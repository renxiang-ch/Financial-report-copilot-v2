"""
Eval harness: run the agent against eval_set.json and score results.

Metrics:
  - tier1_accuracy     : % of Tier-1 numeric questions within tolerance
  - tier2_accuracy     : % of Tier-2 numeric questions within tolerance
  - input_value_hit    : % of Tier-2 questions where agent fetched correct raw values
  - passage_hit        : % of retrieval questions where key_phrase found in retrieved chunks
  - avg_judge_score    : mean LLM-judge score (0–3) for retrieval questions
  - retrieval_accuracy : % of retrieval questions where passage_hit AND judge>=2
  - refusal_accuracy   : % of unanswerable questions correctly refused
  - overall_accuracy   : across all questions
  - avg_latency_s      : mean wall-clock seconds per question
  - estimated_cost_usd : gpt-4o-mini API cost

Usage:
    python -m copilot.eval.harness
    python -m copilot.eval.harness --dataset data/datasets/eval_set.json --tier 1
    python -m copilot.eval.harness --out data/results/eval_results_latest.json
    python -m copilot.eval.harness --limit 5
"""

import argparse
import json
import re
import time
from pathlib import Path

from openai import OpenAI

# Shared with the agent on purpose: two different readings of "the answer states
# this number" in one codebase would eventually disagree, and the disagreement
# would surface as a scoring mystery rather than as a bug.
from copilot.agent.grounding import accessions_in, looks_like_refusal, value_appears_in
# Same reason: the string 'default (gpt-4o-mini)' was written down here while
# the default lived in model_router, which is this project's signature defect in
# miniature -- a result file would have named the wrong model the day the default
# changed, and nothing would have failed.
from copilot.agent.model_router import DEFAULT_MODEL

# USD per 1M tokens, keyed by agent model. A cross-model comparison whose cost
# column is computed at one model's rates is worse than having no cost column --
# it looks quantitative and is wrong by the price ratio, which here is ~17x.
# Rates are first-party OpenAI list prices; an aggregator (AIHubMix and similar)
# bills differently, so `cost_basis` records which case applied.
_MODEL_RATES = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o":      (2.50, 10.00),
}
_DEFAULT_RATES = _MODEL_RATES["gpt-4o-mini"]


def _rates_for(model: str | None) -> tuple[tuple[float, float], str]:
    """Return ((input, output) $/1M, a note on how trustworthy that is)."""
    key = (model or "gpt-4o-mini").strip()
    if key in _MODEL_RATES:
        return _MODEL_RATES[key], f"OpenAI list price for {key}"
    return _DEFAULT_RATES, (
        f"no rate on file for {key} -- costed at gpt-4o-mini rates, "
        f"treat the dollar figure as a placeholder and the token counts as real"
    )


# ── number utilities ──────────────────────────────────────────────────────────

def _extract_number(text: str) -> float | None:
    """
    Pull the first recognisable number out of an answer string.
    Handles: $391.0B, 46.2%, $93,736M, -0.72, 2.02%
    """
    text = text.replace(",", "").replace("$", "").replace("%", "")
    matches = re.findall(r"-?\d+\.?\d*", text)
    if not matches:
        return None
    for m in matches:
        val = float(m)
        if 1990 <= val <= 2030:  # skip years
            continue
        return val
    return None


def _within_tolerance(got: float, expected: float, tol_pct: float) -> bool:
    """True if got is within tol_pct% of expected, accounting for SI magnitude differences."""
    if expected == 0:
        return abs(got) < 1e-9
    ratio = got / expected
    if abs(ratio - 1.0) * 100 <= tol_pct:
        return True
    for scale in (1e9, 1e6, 1e3):
        if abs((got * scale) / expected - 1.0) * 100 <= tol_pct:
            return True
        if abs((got / scale) / expected - 1.0) * 100 <= tol_pct:
            return True
    return False


def _within_tolerance_abs(got: float, expected: float, tol_abs: float) -> bool:
    """
    Absolute tolerance, NO SI-scale hopping. Two deliberate uses:
    - near-zero percentages (e.g. CAGR -0.42%) where a relative tolerance band
      collapses to nothing
    - unit-trap questions ("expressed in millions") where the SI-scale forgiveness
      of _within_tolerance would defeat the entire point of the question
    """
    return abs(got - expected) <= tol_abs


# ── refusal detection ─────────────────────────────────────────────────────────

def _is_refusal(text: str) -> bool:
    """Did this answer leave the question unanswered? See
    grounding.REFUSAL_BROAD for the two readings and why they differ."""
    return looks_like_refusal(text)


# ── retrieval scoring helpers ─────────────────────────────────────────────────

def _check_key_phrase(steps: list[dict], golden_citations: list[dict]) -> bool:
    """
    Scan every tool step's own text evidence for a golden key_phrase.

    Originally scanned retrieve_text chunks only. That missed a real case:
    a supply-chain dependency question the agent correctly answers via
    graph_query instead, whose edges carry the same disclosure sentence in
    `source_text` -- e.g. ret_swks_apple_concentration_2024's golden phrase
    ("constituted more than ten percent of our net revenue") is present
    verbatim in the graph_query edge's source_text, not in any retrieve_text
    chunk, because the agent correctly reached for the more precise tool.
    Scoring that as a miss punished the better tool choice. This does not
    relax what counts as evidence -- source_text is the same SEC-filing
    sentence a retrieve_text chunk would have been -- it only widens where
    the check looks for it.
    """
    retrieved_texts: list[str] = []
    for step in steps:
        if step.get("tool") == "retrieve_text":
            for r in step.get("output", {}).get("results", []):
                retrieved_texts.append(r.get("text", ""))
        elif step.get("tool") == "graph_query":
            for edge in step.get("output", {}).get("edges", []):
                st = edge.get("source_text")
                if st:
                    retrieved_texts.append(st)

    for gc in golden_citations:
        key_phrase = gc.get("key_phrase", "")
        if not key_phrase:
            continue
        for text in retrieved_texts:
            if key_phrase.lower() in text.lower():
                return True
    return False


# ── grounded-answer scoring (outcome-scored, tool-path-agnostic) ─────────────

def _score_grounded(item: dict, answer_text: str) -> dict:
    """
    Deterministic outcome scoring: judges WHAT the answer says, not WHICH tool
    produced it. Fixes the documented false negative where the harness only
    credited retrieve_text chunk hits and scored the agent's (better) graph_query
    path as a miss.

    Three independent checks, all must pass:
      facts_ok    — every golden_fact_keywords group has at least one alternative
                    present in the answer (case-insensitive; groups are AND,
                    alternatives within a group are OR)
      citation_ok — answer contains an SEC accession number (require_citation)
      pct_ok      — if allowed_pct_values is set, every percentage stated in the
                    answer must be in that set (catches fabricated precise figures
                    like "69%" on a threshold-only disclosure)
      lead_ok     — if leading_pct is set, the FIRST percentage in the answer must
                    be that one. Separates "answered for FY2024 and added context"
                    from "answered for FY2026 and mentioned FY2024", which every
                    other check here reads identically.
    """
    lower = answer_text.lower()

    fact_groups = item.get("golden_fact_keywords", [])
    facts_ok = all(
        any(alt.lower() in lower for alt in group) for group in fact_groups
    )

    citation_ok = True
    if item.get("require_citation", True):
        citation_ok = bool(accessions_in(answer_text))
    # Which filing, not merely that there was one. A question about FY2024 that
    # is answered from the FY2023 filing cites a real accession and passes an
    # "is there a citation" check, which is how one frozen retrieval question
    # scored correct for years while answering the wrong year -- its key_phrase
    # does not appear in the FY2024 filing at all.
    wrong_filing: list[str] = []
    _cited = accessions_in(answer_text)
    for accn in item.get("required_accessions") or []:
        if accn.replace("-", "") not in _cited:
            wrong_filing.append(accn)
    citation_ok = citation_ok and not wrong_filing

    pct_ok = True
    fabricated: list[float] = []
    stated_pcts = [float(m.group(1))
                   for m in re.finditer(r"(-?\d+(?:\.\d+)?)\s*%", answer_text)]
    allowed = item.get("allowed_pct_values")
    if allowed:
        for v in stated_pcts:
            if not any(abs(v - a) < 0.01 for a in allowed):
                fabricated.append(v)
        pct_ok = not fabricated

    # Which figure the answer LEADS with, when the question has one right answer
    # and neighbouring years are legitimate context.
    #
    # `allowed_pct_values` cannot carry this. It exists to catch invented
    # figures, so its list has to contain every real one -- and an answer that
    # opens with the wrong year's percentage and mentions the right one further
    # down then passes it, and passes a keyword check, and passes a citation
    # check. That answer is wrong in the way this whole set is about. The first
    # percentage stated is the one a reader takes as the answer.
    lead_ok = True
    lead_value = stated_pcts[0] if stated_pcts else None
    if item.get("leading_pct") is not None:
        lead_ok = (lead_value is not None
                   and abs(lead_value - float(item["leading_pct"])) < 0.01)

    # Values the answer must state, and values it must not. `forbidden_values`
    # encodes a defect directly rather than its absence: the threshold_only bug
    # produced a figure exactly ten times too large, and naming that figure makes
    # the question fail loudly if the bug returns, whichever phrasing it wears.
    values_ok = True
    missing: list[float] = []
    forbidden_seen: list[float] = []
    for v in item.get("required_values") or []:
        if not value_appears_in(answer_text, v):
            missing.append(v)
    for v in item.get("forbidden_values") or []:
        if value_appears_in(answer_text, v):
            forbidden_seen.append(v)
    values_ok = not missing and not forbidden_seen

    return {
        "id":              item["id"],
        "tier":            item["tier"],
        "type":            "grounded",
        "answerable":      True,
        "correct":         facts_ok and citation_ok and pct_ok and values_ok and lead_ok,
        "facts_ok":        facts_ok,
        "citation_ok":     citation_ok,
        "missing_accns":   wrong_filing,
        "pct_ok":          pct_ok,
        "lead_ok":         lead_ok,
        "lead_pct":        lead_value,
        "values_ok":       values_ok,
        "missing_values":  missing,
        "forbidden_seen":  forbidden_seen,
        "fabricated_pcts": fabricated,
        # Stored whole, not truncated. A 200-character excerpt cuts an
        # accession in half, so a result file could not be re-scored
        # offline -- re-running the grounding check over saved traces read
        # "0000320193-24-0001" as a dollar figure and flagged a correct
        # answer. An artefact you cannot re-examine without paying for the
        # run again is a weak artefact.
        "got_text":        answer_text,
    }


# ── DB fingerprint ────────────────────────────────────────────────────────────

def _db_fingerprint() -> dict:
    """
    Snapshot of the data the eval ran against. Two results files are only
    comparable if their fingerprints match — without this, a DB-side change
    (new filings ingested, edges fixed) silently masquerades as an agent-side
    regression or improvement. Comparison is still manual for now (automated
    gate = Roadmap task 3.2); the fingerprint makes the mismatch visible.
    """
    try:
        from copilot.storage.db import get_conn
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                  (SELECT COUNT(*) FROM companies)                                        AS companies,
                  (SELECT COUNT(*) FROM supply_edges WHERE disclosure_status = 'named')   AS named_edges,
                  (SELECT COUNT(*) FROM financial_facts)                                  AS financial_facts,
                  (SELECT COUNT(*) FROM filings)                                          AS filings,
                  (SELECT MAX(fiscal_year) FROM supply_edges)                             AS max_fy_edges
            """)
            row = cur.fetchone()
        return dict(row)
    except Exception as e:
        return {"error": str(e)}


# ── Tier-2 input value verification ──────────────────────────────────────────

def _check_input_values(steps: list[dict], input_values: dict,
                        tol_pct: float = 0.5) -> dict[str, bool]:
    """
    For Tier-2 questions: verify that query_financials calls returned the
    correct raw values (the ones needed for the formula).

    Returns a dict: {variable_name: hit (bool)}
    """
    qf_values: list[float] = []
    for step in steps:
        if step.get("tool") == "query_financials":
            out = step.get("output", {})
            if out.get("found") and out.get("value") is not None:
                try:
                    qf_values.append(float(out["value"]))
                except (TypeError, ValueError):
                    pass

    hits: dict[str, bool] = {}
    for var_name, expected_val in input_values.items():
        hits[var_name] = any(
            _within_tolerance(v, float(expected_val), tol_pct)
            for v in qf_values
        )
    return hits


# ── LLM judge ─────────────────────────────────────────────────────────────────

_judge_client: OpenAI | None = None

def _llm_judge(question: str, golden_answer: str, agent_answer: str) -> dict:
    """
    Use gpt-4o-mini to score agent_answer vs golden_answer.
    Returns {"score": 0–3, "reason": str}
    """
    global _judge_client
    from copilot.config import settings
    if not settings.openai_api_key:
        return {"score": -1, "reason": "no API key"}
    if _judge_client is None:
        _judge_client = OpenAI(api_key=settings.openai_api_key)

    prompt = f"""You are evaluating a financial research assistant's answer.

Question: "{question}"

Reference answer (ground truth from the 10-K filing):
{golden_answer}

Generated answer:
{agent_answer}

Score 0–3:
3 = Fully correct — all key facts from reference present, no fabrications
2 = Mostly correct — main point captured, minor omissions or slight inaccuracies
1 = Partially correct — misses important facts or has notable errors
0 = Incorrect or fabricated — contradicts reference or invents information

Respond with ONLY valid JSON:
{{"score": <0|1|2|3>, "reason": "<one concise sentence>"}}"""

    response = _judge_client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=128,
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        return json.loads(response.choices[0].message.content.strip())
    except Exception:
        return {"score": -1, "reason": "parse error"}


# ── tool trace ───────────────────────────────────────────────────────────────

def _build_tool_trace(steps: list[dict]) -> list[str]:
    """
    Build a compact human-readable trace of all tool calls for one question.
    Each entry is one line: "tool_name(args) → result_summary"
    """
    trace = []
    for step in steps:
        tool = step.get("tool", "?")
        inp  = step.get("input", {})
        out  = step.get("output", {})

        if tool == "query_financials":
            ticker = inp.get("ticker", "?")
            metric = inp.get("metric", "?")
            fy     = inp.get("fiscal_year", "latest")
            if out.get("found"):
                val  = out.get("value")
                cite = out.get("citation", "")
                trace.append(f"query_financials({ticker}, {metric}, FY{fy}) → {val}  [{cite}]")
            else:
                trace.append(f"query_financials({ticker}, {metric}, FY{fy}) → NOT FOUND")

        elif tool == "compute":
            expr   = inp.get("expression", "?")
            result = out.get("result")
            trace.append(f"compute({expr}) → {result}")

        elif tool == "list_metrics":
            ticker  = inp.get("ticker", "?")
            metrics = out.get("metrics", [])
            trace.append(f"list_metrics({ticker}) → {len(metrics)} metrics available")

        elif tool == "retrieve_text":
            query   = inp.get("query", "?")
            ticker  = inp.get("ticker", "all")
            results = out.get("results", [])
            chunk_summaries = [
                f"{r.get('ticker')}:{r.get('section')} score={r.get('score', 0):.3f}"
                for r in results[:5]
            ]
            trace.append(
                f"retrieve_text('{query}', ticker={ticker}) → "
                f"{len(results)} chunks: [{', '.join(chunk_summaries)}]"
            )

        else:
            trace.append(f"{tool}({inp}) → {out}")

    return trace


# ── item scorer ───────────────────────────────────────────────────────────────

def score_item(item: dict, agent_result: dict) -> dict:
    answer_text = agent_result.get("answer", "")
    steps       = agent_result.get("steps", [])

    # ── unanswerable ─────────────────────────────────────────────────────────
    if not item["answerable"]:
        correct = _is_refusal(answer_text)
        return {
            "id":               item["id"],
            "tier":             item["tier"],
            "type":             "unanswerable",
            "answerable":       False,
            "correct":          correct,
            "refusal_detected": correct,
            "got_text":         answer_text,
        }

    # ── grounded (outcome-scored, tool-path-agnostic) ────────────────────────
    if item.get("type") == "grounded":
        return _score_grounded(item, answer_text)

    # ── retrieval ─────────────────────────────────────────────────────────────
    if item.get("type") == "retrieval":
        passage_hit = _check_key_phrase(steps, item.get("golden_citations", []))
        judge       = _llm_judge(item["question"], item.get("golden_answer", ""), answer_text)
        judge_score = judge.get("score", -1)
        correct     = passage_hit and judge_score >= 2
        return {
            "id":           item["id"],
            "tier":         item["tier"],
            "type":         "retrieval",
            "answerable":   True,
            "correct":      correct,
            "passage_hit":  passage_hit,
            "judge_score":  judge_score,
            "judge_reason": judge.get("reason", ""),
            "got_text":     answer_text,
        }

    # ── numeric (Tier-1 and Tier-2) ───────────────────────────────────────────
    # Prefer compute step output (exact, preserves sign) over text extraction
    # (text extraction loses negatives: "declined by 2.8%" → +2.8)
    got_raw = None
    for step in reversed(steps):
        if step.get("tool") == "compute":
            raw = step.get("output", {}).get("result")
            if raw is not None:
                try:
                    got_raw = float(raw)
                    break
                except (TypeError, ValueError):
                    pass
    if got_raw is None:
        got_raw = _extract_number(answer_text)

    correct = False
    if got_raw is not None:
        # Percent is a display unit: a compute step that returns the decimal form
        # (-0.004184) and an answer that states "-0.42%" are the same quantity.
        # For %/pp items also try got*100 (decimal→percent) — but never got/100:
        # an agent that multiplied by 100 twice is genuinely wrong.
        # (Found by v2_aapl_rev_cagr_2022_2024 on its first run, 2026-07-16: the
        # "prefer compute output" heuristic grabbed the decimal form and failed a
        # correct answer.)
        candidates = [got_raw]
        if item.get("expected_unit") in ("%", "pp"):
            candidates.append(got_raw * 100)
        if item.get("tolerance_abs") is not None:
            correct = any(
                _within_tolerance_abs(c, item["expected_value"], item["tolerance_abs"])
                for c in candidates
            )
        else:
            correct = any(
                _within_tolerance(c, item["expected_value"], item["tolerance_pct"])
                for c in candidates
            )

    scored: dict = {
        "id":        item["id"],
        "tier":      item["tier"],
        "type":      "numeric",
        "answerable": True,
        "correct":   correct,
        "expected":  item["expected_value"],
        "got_raw":   got_raw,
        "got_text":  answer_text,
        "unit":      item["expected_unit"],
        # over-refusal analysis: a refusal on an answerable question is a distinct
        # failure mode from a wrong number — record which one happened
        "refusal_detected": _is_refusal(answer_text),
    }

    # Tier-2 extra: verify agent fetched the correct raw input values
    if item["tier"] == 2 and item.get("input_values"):
        iv_hits = _check_input_values(steps, item["input_values"])
        scored["input_values_hit"]    = iv_hits
        scored["all_inputs_fetched"]  = all(iv_hits.values())

    return scored


# ── main harness ──────────────────────────────────────────────────────────────

def run_eval(dataset_path: Path, tier_filter: int | None = None,
             limit: int | None = None, include_held_out: bool = False,
             agent_model: str | None = None) -> dict:
    from copilot.agent.agent import ask

    with open(dataset_path, encoding="utf-8") as f:
        data = json.load(f)

    items = data["items"]

    # Retired items stay in the file with their reason so the change is on the
    # record, but they no longer score. A frozen set protects against tuning to
    # the test set; it cannot make a question stay valid after the database
    # changes underneath it. Scoring a question whose premise is now false
    # measures obedience to a stale premise, not capability.
    n_retired = sum(1 for i in items if i.get("retired"))
    items = [i for i in items if not i.get("retired")]

    n_held_out = sum(1 for i in items if i.get("held_out"))
    if not include_held_out:
        # held-out items never inform day-to-day fixes — they only run at
        # milestones (pre-deploy, README number updates) via --include-held-out,
        # so fixes can't quietly overfit to them
        items = [i for i in items if not i.get("held_out")]
    if tier_filter:
        items = [i for i in items if i["tier"] == tier_filter]
    if limit:
        items = items[:limit]

    results = []
    total_input_tokens  = 0
    total_output_tokens = 0
    total_latency       = 0.0

    print(f"Running {len(items)} questions  (dataset v{data.get('version','?')})\n")

    for idx, item in enumerate(items, 1):
        print(f"[{idx:02d}/{len(items)}] {item['id']}")
        print(f"       Q: {item['question']}")

        t0 = time.time()
        try:
            result = ask(item["question"], model=agent_model)
        except Exception as e:
            result = {"answer": f"ERROR: {e}", "steps": [], "citations": [], "usage": {}}
        elapsed = time.time() - t0
        total_latency += elapsed

        total_input_tokens  += result.get("usage", {}).get("input_tokens",  0)
        total_output_tokens += result.get("usage", {}).get("output_tokens", 0)

        scored               = score_item(item, result)
        scored["latency_s"]  = round(elapsed, 2)
        scored["steps"]      = result.get("steps", [])
        scored["citations"]  = result.get("citations", [])
        scored["tool_trace"] = _build_tool_trace(result.get("steps", []))
        # Recorded but never scored. Whether a figure is TRACEABLE and whether it
        # is RIGHT are different properties, and folding the first into the pass
        # mark would let a well-sourced wrong answer earn credit. Reported on its
        # own line below instead.
        scored["verification"] = result.get("verification") or {}
        if item.get("category"):
            scored["category"] = item["category"]
        results.append(scored)

        status = "PASS" if scored["correct"] else "FAIL"
        if not item["answerable"]:
            print(f"       {status} refusal={'yes' if scored.get('refusal_detected') else 'NO'} ({elapsed:.1f}s)")
        elif item.get("type") == "retrieval":
            print(f"       {status} passage={'hit' if scored.get('passage_hit') else 'MISS'}  judge={scored.get('judge_score')}/3 ({elapsed:.1f}s)")
        elif item.get("type") == "grounded":
            print(f"       {status} facts={'ok' if scored.get('facts_ok') else 'MISS'}  "
                  f"citation={'ok' if scored.get('citation_ok') else 'MISS'}  "
                  f"pct={'ok' if scored.get('pct_ok') else 'FABRICATED ' + str(scored.get('fabricated_pcts'))} ({elapsed:.1f}s)")
        else:
            iv_ok = scored.get("all_inputs_fetched")
            iv_str = f"  inputs={'ok' if iv_ok else 'MISSING'}" if iv_ok is not None else ""
            print(f"       {status} expected={item['expected_value']}  got={scored.get('got_raw')}{iv_str} ({elapsed:.1f}s)")

        # always print tool trace so you can see exactly what the agent did
        for line in scored["tool_trace"]:
            print(f"         > {line}")
        print()

    # ── aggregate metrics ─────────────────────────────────────────────────────

    unanswerable = [r for r in results if not r["answerable"]]
    numeric      = [r for r in results if r.get("type") == "numeric"]
    retrieval    = [r for r in results if r.get("type") == "retrieval"]
    tier1        = [r for r in numeric if r["tier"] == 1]
    tier2        = [r for r in numeric if r["tier"] == 2]
    tier2_iv     = [r for r in tier2 if "all_inputs_fetched" in r]

    def acc(lst: list) -> float | None:
        return round(sum(r["correct"] for r in lst) / len(lst) * 100, 1) if lst else None

    (_rate_in, _rate_out), _cost_basis = _rates_for(agent_model)
    cost_usd = (
        total_input_tokens  / 1e6 * _rate_in +
        total_output_tokens / 1e6 * _rate_out
    )

    passage_hit_acc = (
        round(sum(r.get("passage_hit", False) for r in retrieval) / len(retrieval) * 100, 1)
        if retrieval else None
    )
    judge_scores = [r["judge_score"] for r in retrieval if r.get("judge_score", -1) >= 0]
    avg_judge    = round(sum(judge_scores) / len(judge_scores), 2) if judge_scores else None

    input_fetch_acc = (
        round(sum(r["all_inputs_fetched"] for r in tier2_iv) / len(tier2_iv) * 100, 1)
        if tier2_iv else None
    )

    grounded = [r for r in results if r.get("type") == "grounded"]

    category_accuracy = {}
    for r in results:
        c = r.get("category")
        if c:
            category_accuracy.setdefault(c, []).append(r)
    category_accuracy = {c: acc(lst) for c, lst in sorted(category_accuracy.items())}

    flagged = [r for r in results
               if r.get("verification") and not r["verification"].get("verified", True)]
    _unsourced = [r["id"] for r in results
                  if (r.get("verification") or {}).get("unsourced_formulas")]
    checked = sum(r.get("verification", {}).get("numbers_checked", 0) for r in results)

    summary = {
        "dataset":              str(dataset_path),
        "dataset_version":      data.get("version"),
        "db_fingerprint":       _db_fingerprint(),
        "n_total":              len(results),
        "n_held_out_excluded":  0 if include_held_out else n_held_out,
        "n_retired_excluded":   n_retired,
        # Which model answered. The judge is deliberately NOT switched with it:
        # varying both would confound "stronger agent" with "less self-evaluation
        # bias". Only the retrieval metric uses the judge, so the deterministic
        # tiers stay directly comparable across models.
        "agent_model":          agent_model or DEFAULT_MODEL,
        "cost_basis":           _cost_basis,
        "tier1_accuracy":       acc(tier1),
        "tier2_accuracy":       acc(tier2),
        "tier2_input_fetch":    input_fetch_acc,
        "retrieval_accuracy":   acc(retrieval),
        "passage_hit_accuracy": passage_hit_acc,
        "avg_judge_score":      avg_judge,
        "refusal_accuracy":     acc(unanswerable),
        "numeric_accuracy":     acc(numeric),
        "grounded_accuracy":    acc(grounded),
        "category_accuracy":    category_accuracy or None,
        "overall_accuracy":     acc(results),
        # Answers carrying at least one figure, computation input, or accession
        # that could not be traced back to a tool result. Independent of accuracy:
        # a flagged answer may still be correct, and a clean one may still be wrong.
        "unverified_answers":   len(flagged),
        "unsourced_formula_ids": _unsourced,
        "unverified_ids":       [r["id"] for r in flagged],
        "figures_checked":      checked,
        "avg_latency_s":        round(total_latency / len(results), 2) if results else 0,
        "total_latency_s":      round(total_latency, 1),
        "input_tokens":         total_input_tokens,
        "output_tokens":        total_output_tokens,
        "estimated_cost_usd":   round(cost_usd, 4),
        "results":              results,
    }

    return summary


# ── multi-turn ────────────────────────────────────────────────────────────────
#
# Here rather than in a harness of its own. There are already three harness
# shells whose only shared code is the scorer, and a fourth would be the same
# duplication one layer down -- everything below reuses score_item, the
# fingerprint, the cost basis and the trace builder unchanged.
#
# What a conversation needs that a question does not is a second, independent
# check. Reading the answer is not enough when the constraint under test is
# which year the TOOLS were asked for: an answer can print 87% while the tool
# that ran fetched 91%, and every check that reads text would call that correct.


def _check_tool_years(steps: list[dict], allowed: list[int]) -> dict:
    """Which fiscal year the number-fetching tools were actually asked for.

    Only query_financials and graph_query. They are where a year becomes a
    number, and where getting it wrong produces a real figure from the wrong
    period rather than a visible error. retrieve_text is excluded: its year is
    optional by design, it reports the scope it used, and scoping it is a
    retrieval-quality question rather than a binding one.

    Omitting the year counts as a violation, not as a neutral default. "Latest"
    for Cirrus Logic is FY2026, so a dropped year in a FY2024 thread silently
    returns a different year's figure -- which is the defect, in the form it
    actually takes. Reading `latest` as a violation is only sound because every
    conversation in this set is about a year that is NOT the newest one on file,
    which is deliberate: a thread about the newest year cannot show this defect
    at all, since dropping the year lands on the right answer by accident.

    A call that asks for ALL years is not a violation. It returns the required
    year along with the others, so nothing is bound to the wrong period by
    making it -- and this harness has already been burned once by scoring a
    tool path instead of an outcome, when the retrieval scorer failed answers
    for using the better tool. It is recorded separately rather than counted as
    evidence: a superset query leaves which year the ANSWER used undetermined
    here, which is what `leading_pct` is for.
    """
    allowed_set = {int(y) for y in allowed}
    used: set[int] = set()
    supersets: list[str] = []
    violations: list[str] = []

    for step in steps:
        tool = step.get("tool")
        if tool not in ("query_financials", "graph_query"):
            continue
        raw = (step.get("input") or {}).get("fiscal_year")
        if raw is None:
            violations.append(f"{tool}(year omitted -> latest)")
            continue
        text = str(raw).strip().lower()
        years = {int(m) for m in re.findall(r"(?:19|20)\d{2}", text)}
        if text == "trend" or (len(years) > 1 and min(years) <= min(allowed_set)
                               and max(years) >= max(allowed_set)):
            supersets.append(f"{tool}({text})")
            continue
        if not years:
            violations.append(f"{tool}(fiscal_year={raw!r})")
            continue
        outside = years - allowed_set
        if outside:
            violations.append(f"{tool}({sorted(outside)})")
        used |= years & allowed_set

    return {
        "ok":         bool(used or supersets) and not violations,
        "years_used": sorted(used),
        "supersets":  supersets,
        "violations": violations,
    }


def run_multiturn(dataset_path: Path, agent_model: str | None = None,
                  disable_inheritance: bool = False) -> dict:
    """Run each conversation in order, feeding the returned history forward.

    `disable_inheritance` is the ablation: the assumption block is suppressed and
    nothing else changes -- same history, same trimming, same prompt shape. It
    exists because "the fix works" is not a measurement until the same set has
    been run with the fix switched off, and this project has twice reported an
    improvement that turned out to be the instrument moving.
    """
    from copilot.agent import agent as agent_mod
    from copilot.agent.agent import ask

    with open(dataset_path, encoding="utf-8") as f:
        data = json.load(f)

    original_block = agent_mod.active_context_block
    if disable_inheritance:
        agent_mod.active_context_block = lambda slots: None

    conversations = []
    total_input_tokens = total_output_tokens = 0
    total_latency = 0.0
    n_turns_scored = n_turns_correct = 0

    try:
        header = f"Running {len(data['conversations'])} conversations (dataset v{data.get('version','?')}"
        header += ", inheritance DISABLED)" if disable_inheritance else ")"
        print(header + "\n")

        for conv in data["conversations"]:
            print(f"=== {conv['id']}")
            history: list[dict] = []
            turn_records = []
            conv_correct = True

            for idx, turn in enumerate(conv["turns"], 1):
                print(f"  [t{idx}] {turn['question']}")
                t0 = time.time()
                try:
                    result = ask(turn["question"], model=agent_model, history=history)
                except Exception as e:
                    result = {"answer": f"ERROR: {e}", "steps": [], "citations": [],
                              "usage": {}, "context": {}, "history": history}
                elapsed = time.time() - t0
                total_latency += elapsed
                total_input_tokens += result.get("usage", {}).get("input_tokens", 0)
                total_output_tokens += result.get("usage", {}).get("output_tokens", 0)
                history = result.get("history") or history

                context = result.get("context") or {}
                record = {
                    "turn":       idx,
                    "question":   turn["question"],
                    "answer":     result.get("answer", ""),
                    "inherited":  context.get("inherited") or [],
                    "latency_s":  round(elapsed, 2),
                    "tool_trace": _build_tool_trace(result.get("steps", [])),
                }

                if turn.get("score") is False:
                    record["scored"] = False
                    print(f"        (setup turn, not scored) inherited={record['inherited']}")
                    turn_records.append(record)
                    continue

                item = dict(turn)
                item["id"] = f"{conv['id']}#t{idx}"
                scored = score_item(item, result)

                years = None
                if turn.get("tool_years"):
                    years = _check_tool_years(result.get("steps", []), turn["tool_years"])
                    scored["tool_years"] = years
                    scored["correct"] = bool(scored["correct"]) and years["ok"]

                record["scored"] = True
                record["result"] = scored
                record["verification"] = result.get("verification") or {}
                n_turns_scored += 1
                n_turns_correct += bool(scored["correct"])
                conv_correct = conv_correct and bool(scored["correct"])

                status = "PASS" if scored["correct"] else "FAIL"
                extra = ""
                if years is not None:
                    extra = "  years=" + ("ok " + str(years["years_used"]) if years["ok"]
                                          else "WRONG " + str(years["violations"]))
                print(f"        {status} inherited={record['inherited']}{extra} ({elapsed:.1f}s)")
                for line in record["tool_trace"]:
                    print(f"          > {line}")
                turn_records.append(record)

            conversations.append({
                "id":            conv["id"],
                "correct":       conv_correct,
                "design_intent": conv.get("design_intent", ""),
                "fails_means":   conv.get("fails_means", ""),
                "turns":         turn_records,
            })
            print(f"    -> {'PASS' if conv_correct else 'FAIL'}\n")
    finally:
        agent_mod.active_context_block = original_block

    (_rate_in, _rate_out), _cost_basis = _rates_for(agent_model)
    cost_usd = (total_input_tokens / 1e6 * _rate_in +
                total_output_tokens / 1e6 * _rate_out)

    # Turns whose number-fetching tools used only the years the conversation
    # established. Reported on its own line: it is the property under test, and
    # folding it into one accuracy number would hide which half moved.
    year_checked = [t["result"]["tool_years"]
                    for c in conversations for t in c["turns"]
                    if t.get("scored") and "tool_years" in (t.get("result") or {})]

    return {
        "dataset":               str(dataset_path),
        "dataset_version":       data.get("version"),
        "inheritance_enabled":   not disable_inheritance,
        "n_conversations":       len(conversations),
        "conversation_accuracy": (round(sum(c["correct"] for c in conversations)
                                        / len(conversations) * 100, 1)
                                  if conversations else None),
        "n_turns_scored":        n_turns_scored,
        "turn_accuracy":         (round(n_turns_correct / n_turns_scored * 100, 1)
                                  if n_turns_scored else None),
        "year_binding_accuracy": (round(sum(y["ok"] for y in year_checked)
                                        / len(year_checked) * 100, 1)
                                  if year_checked else None),
        "n_year_checked":        len(year_checked),
        "db_fingerprint":        _db_fingerprint(),
        "agent_model":           agent_model or DEFAULT_MODEL,
        "cost_basis":            _cost_basis,
        "avg_latency_s":         (round(total_latency / n_turns_scored, 2)
                                  if n_turns_scored else 0),
        "total_latency_s":       round(total_latency, 1),
        "input_tokens":          total_input_tokens,
        "output_tokens":         total_output_tokens,
        "estimated_cost_usd":    round(cost_usd, 4),
        "conversations":         conversations,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def run_multiturn_repeated(dataset_path: Path, agent_model: str | None = None,
                           disable_inheritance: bool = False,
                           repeat: int = 1) -> dict:
    """Run the whole set several times and report the spread, not one number.

    This project's frozen retrieval metric has a 25-62.5% band across runs of
    identical code, and it has twice reported a change that turned out to be the
    instrument moving. A conversation set is worse, not better: nine scored turns
    means one flip is eleven points. A single run of this is an anecdote.

    What comes out is the per-turn pass count across runs. The aggregate is there
    for the summary line, but the flip list is the part worth reading -- a turn
    that passes three times in five is a different finding from one that passes
    every time, and averaging hides which is which.
    """
    runs = [run_multiturn(dataset_path, agent_model=agent_model,
                          disable_inheritance=disable_inheritance)
            for _ in range(repeat)]

    per_turn: dict[str, dict] = {}
    for run in runs:
        for conv in run["conversations"]:
            for turn in conv["turns"]:
                if not turn.get("scored"):
                    continue
                key = f"{conv['id']}#t{turn['turn']}"
                slot = per_turn.setdefault(key, {"passes": 0, "runs": 0, "notes": []})
                slot["runs"] += 1
                slot["passes"] += bool(turn["result"].get("correct"))
                years = turn["result"].get("tool_years") or {}
                if years.get("violations"):
                    slot["notes"].append(years["violations"][0])

    accs = [r["turn_accuracy"] for r in runs if r["turn_accuracy"] is not None]
    binds = [r["year_binding_accuracy"] for r in runs
             if r.get("year_binding_accuracy") is not None]

    def spread(values: list[float]) -> dict:
        if not values:
            return {"mean": None, "min": None, "max": None}
        return {"mean": round(sum(values) / len(values), 1),
                "min": min(values), "max": max(values)}

    return {
        "dataset":             str(dataset_path),
        "dataset_version":     runs[0]["dataset_version"] if runs else None,
        "inheritance_enabled": not disable_inheritance,
        "repeat":              repeat,
        "turn_accuracy":       spread(accs),
        "year_binding":        spread(binds),
        # Sorted least-stable first: the turns that flip are the ones that decide
        # whether a difference between two arms means anything.
        "per_turn":            dict(sorted(
            per_turn.items(),
            key=lambda kv: abs(kv[1]["passes"] / kv[1]["runs"] - 0.5))),
        "agent_model":         runs[0]["agent_model"] if runs else None,
        "db_fingerprint":      runs[0]["db_fingerprint"] if runs else {},
        "estimated_cost_usd":  round(sum(r["estimated_cost_usd"] for r in runs), 4),
        "total_latency_s":     round(sum(r["total_latency_s"] for r in runs), 1),
        "runs":                runs,
    }


def _print_repeated(summary: dict) -> None:
    print("=" * 60)
    print(f"MULTI-TURN, {summary['repeat']} RUNS")
    print("=" * 60)
    print(f"  Dataset version : {summary['dataset_version']}")
    print(f"  Inheritance     : {'ON' if summary['inheritance_enabled'] else 'OFF (ablation)'}")
    ta, yb = summary["turn_accuracy"], summary["year_binding"]
    print(f"  Turn accuracy   : {ta['mean']}%  (range {ta['min']}-{ta['max']})")
    print(f"  Year binding    : {yb['mean']}%  (range {yb['min']}-{yb['max']})")
    print("  Per turn, least stable first:")
    for key, slot in summary["per_turn"].items():
        note = f"   {slot['notes'][0]}" if slot["notes"] else ""
        print(f"    {slot['passes']}/{slot['runs']}  {key}{note}")
    print(f"  Agent model     : {summary.get('agent_model')}")
    print(f"  DB fingerprint  : {summary.get('db_fingerprint')}")
    print(f"  Total latency   : {summary['total_latency_s']}s")
    print(f"  Estimated cost  : ${summary['estimated_cost_usd']}")


def _print_multiturn(summary: dict) -> None:
    print("=" * 60)
    print("MULTI-TURN SUMMARY")
    print("=" * 60)
    print(f"  Dataset version      : {summary['dataset_version']}")
    print(f"  Inheritance          : {'ON' if summary['inheritance_enabled'] else 'OFF (ablation)'}")
    print(f"  Conversations        : {summary['n_conversations']}")
    print(f"  Conversation accuracy: {summary['conversation_accuracy']}%  (every scored turn correct)")
    print(f"  Turn accuracy        : {summary['turn_accuracy']}%  ({summary['n_turns_scored']} scored turns)")
    # Reported separately because it is the property under test. An answer can
    # state the right figure while the tool that produced it fetched another
    # year, and one blended number would hide which of the two moved.
    print(f"  Year binding         : {summary['year_binding_accuracy']}%  "
          f"({summary['n_year_checked']} turns where the tools' fiscal year was checked)")
    for conv in summary["conversations"]:
        mark = "PASS" if conv["correct"] else "FAIL"
        print(f"    {mark}  {conv['id']}")
        for turn in conv["turns"]:
            if not turn.get("scored"):
                continue
            res = turn.get("result", {})
            years = res.get("tool_years")
            note = ""
            if years and not years["ok"]:
                note = f"  years -> {years['violations']}"
            print(f"          t{turn['turn']} {'ok ' if res.get('correct') else 'BAD'} "
                  f"inherited={turn['inherited']}{note}")
    fp = summary.get("db_fingerprint", {})
    if fp and "error" not in fp:
        print(f"  DB fingerprint       : {fp}")
    print(f"  Agent model          : {summary.get('agent_model')}")
    print(f"  Avg latency          : {summary['avg_latency_s']}s / scored turn")
    print(f"  Tokens (in/out)      : {summary['input_tokens']} / {summary['output_tokens']}")
    print(f"  Estimated cost       : ${summary['estimated_cost_usd']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="data/datasets/eval_set.json")
    parser.add_argument("--tier",  type=int, default=None, help="Filter to tier 1 or 2")
    parser.add_argument("--limit", type=int, default=None, help="Run only first N questions")
    parser.add_argument("--out",   default=None, help="Save full results JSON")
    parser.add_argument("--model", default=None,
                        help="Agent model id. Any model the configured endpoint serves. "
                             "The LLM judge stays on gpt-4o-mini regardless.")
    parser.add_argument("--include-held-out", action="store_true",
                        help="Also run held_out items (milestone evals only)")
    parser.add_argument("--repeat", type=int, default=1,
                        help="Conversation datasets only: run the set N times and "
                             "report the spread and which turns flipped. One run of "
                             "nine turns cannot separate an effect from noise.")
    parser.add_argument("--no-inherit", action="store_true",
                        help="Conversation datasets only: suppress the carried-context "
                             "block, leaving history and everything else unchanged. "
                             "The before side of the before/after.")
    args = parser.parse_args()

    # Conversation sets are recognised by their shape rather than by a separate
    # command. One entry point, one scorer, one fingerprint -- the alternative was
    # a fourth harness whose only original content is the loop over turns.
    with open(args.dataset, encoding="utf-8") as f:
        _shape = json.load(f)
    if "conversations" in _shape:
        if args.repeat > 1:
            summary = run_multiturn_repeated(Path(args.dataset), agent_model=args.model,
                                             disable_inheritance=args.no_inherit,
                                             repeat=args.repeat)
            _print_repeated(summary)
        else:
            summary = run_multiturn(Path(args.dataset), agent_model=args.model,
                                    disable_inheritance=args.no_inherit)
            _print_multiturn(summary)
        if args.out:
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(summary, f, indent=2)
            print(chr(10) + f"Saved: {out_path}")
        return

    summary = run_eval(Path(args.dataset), tier_filter=args.tier, limit=args.limit,
                       include_held_out=args.include_held_out,
                       agent_model=args.model)

    print("=" * 60)
    print("EVAL SUMMARY")
    print("=" * 60)
    print(f"  Dataset version   : {summary['dataset_version']}")
    print(f"  Total questions   : {summary['n_total']}")
    print("  ── Numeric ─────────────────────────────")
    print(f"  Tier-1 accuracy   : {summary['tier1_accuracy']}%")
    print(f"  Tier-2 accuracy   : {summary['tier2_accuracy']}%")
    print(f"  Tier-2 input fetch: {summary['tier2_input_fetch']}%  (agent got correct raw values)")
    print("  ── Retrieval ───────────────────────────")
    print(f"  Passage hit       : {summary['passage_hit_accuracy']}%  (key_phrase in chunks)")
    print(f"  Avg judge score   : {summary['avg_judge_score']} / 3")
    print(f"  Retrieval accuracy: {summary['retrieval_accuracy']}%  (hit ∩ judge≥2)")
    print("  ── Other ───────────────────────────────")
    print(f"  Refusal accuracy  : {summary['refusal_accuracy']}%")
    if summary.get("grounded_accuracy") is not None:
        print(f"  Grounded accuracy : {summary['grounded_accuracy']}%  (facts + citation + no fabricated %)")
    if summary.get("category_accuracy"):
        print("  ── By category ─────────────────────────")
        for c, a in summary["category_accuracy"].items():
            print(f"  {c:<26}: {a}%")
    print(f"  Overall accuracy  : {summary['overall_accuracy']}%")
    print(f"  Grounding check   : {summary['figures_checked']} figures checked, "
          f"{summary['unverified_answers']} answer(s) flagged"
          + (f" -> {', '.join(summary['unverified_ids'])}" if summary["unverified_ids"] else ""))
    fp = summary.get("db_fingerprint", {})
    if fp and "error" not in fp:
        print(f"  DB fingerprint    : {fp}")
    print(f"  Agent model       : {summary.get('agent_model', '—')}")
    print(f"  Cost basis        : {summary.get('cost_basis', '—')}")
    if summary.get("n_retired_excluded"):
        print(f"  Retired excluded  : {summary['n_retired_excluded']} items (premise no longer true)")
    if summary.get("n_held_out_excluded"):
        print(f"  Held-out excluded : {summary['n_held_out_excluded']} items (--include-held-out to run)")
    print("  ── Cost & Latency ──────────────────────")
    print(f"  Avg latency       : {summary['avg_latency_s']}s / question")
    print(f"  Total latency     : {summary['total_latency_s']}s")
    print(f"  Tokens (in/out)   : {summary['input_tokens']} / {summary['output_tokens']}")
    print(f"  Estimated cost    : ${summary['estimated_cost_usd']}")

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(summary, f, indent=2)
        print(f"\nFull results saved to {out_path}")


if __name__ == "__main__":
    main()
