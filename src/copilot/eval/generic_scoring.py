"""Scoring for the generic-agent baseline (docs/generic-agent-baseline.md).

Reuses harness.py's / harness_tier3.py's tolerance, refusal-detection and
LLM-judge primitives so a generic_agent score is comparable to v1_loop's --
same tolerance bands, same refusal detector, same judge rubric.

The one thing that can't be reused as-is: harness_tier3.score_item_t3 reads
tool-call ``steps`` (graph_query's returned edges) to check traversal traces.
A plain-text CLI answer has no steps, so graph_lookup/graph_trend items here
check the same expected_edges against the answer TEXT instead -- including
`threshold_only` semantics (a disclosed ">10%" floor is satisfied by any value
>= the floor, not by matching it exactly; a plain numeric answer that is more
precise than the floor is not a miss).
"""

from __future__ import annotations

import re
from typing import Any

from copilot.eval.harness import (
    _extract_number,
    _is_refusal,
    _llm_judge,
    _within_tolerance,
    _within_tolerance_abs,
)
from copilot.eval.harness_tier3 import _llm_judge_graph


def _safe_judge(fn, *args) -> dict[str, Any]:
    """Run an LLM judge, but never crash the sweep on a bad/placeholder key,
    a network error, or a rate limit -- those become `needs_review`, to be
    re-scored later (`scripts/rescore_generic_baseline.py`) once the key works.
    harness.py's judges only guard an empty key, not a wrong one."""
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001 -- deliberate: score offline later
        return {"score": -1, "reason": f"judge unavailable: {type(e).__name__}"}

_PCT_RE = re.compile(r"(-?\d+\.?\d*)\s*%")

# Tier-3 revenue_pct is read off a percentage the answer states in prose, not a
# `compute` step -- allow a couple of points of rounding/paraphrase slack.
_TIER3_PCT_TOLERANCE_ABS = 2.0

# The number right after "=" in a shown calculation -- Codex reliably writes out
# its arithmetic (e.g. "(383.285 - 394.328) / 394.328 * 100 = -2.8%"). Preferring
# this over the first number in the prose is the plain-text equivalent of v1's
# harness preferring the `compute` step's signed output over text extraction.
# The `[*_~≈$ ]*` skips markdown emphasis / currency so "= **$311.3 million**"
# still yields 311.3; a "(" (an intermediate sub-expression) is not skipped.
_CALC_RE = re.compile(r"=\s*[*_~≈$ ]*(-?\d[\d,]*\.?\d*)\s*%?")

# Markdown link target -- `](/abs/path/... Copilot v2/...)` would otherwise feed
# path digits (e.g. "v2" -> 2) to `_extract_number`.
_LINK_RE = re.compile(r"\]\([^)]*\)")

# Form types ("10-K", "10-Q", "8-K", "20-F") whose leading digits `_extract_number`
# would otherwise grab as the answer.
_FORM_RE = re.compile(r"\b(?:10-K|10-Q|8-K|20-F|6-K|S-1)\b", re.I)


def _extract_answer_number(text: str) -> float | None:
    """Number an answer is actually asserting, robust to Codex's prose style.

    Prefers the value after "=" in a shown calculation (Codex always writes its
    arithmetic out) -- keeping whatever sign that carries. Otherwise falls back
    to the first non-year number, sign-agnostic; `score_numeric` tries both
    signs against `expected_value`, so "net loss of $70M" and "decrease of
    $347M" both resolve correctly without guessing from prose.
    """
    text = _LINK_RE.sub("]", text or "")
    calc = _CALC_RE.findall(text)
    if calc:
        try:
            return float(calc[-1].replace(",", ""))
        except ValueError:
            pass
    return _extract_number(_FORM_RE.sub(" ", text))

# `eval_set.json` marks these `answerable: false`, but for three different
# reasons -- only the first is a fair "the agent should have refused" test.
#   undisclosed    : no filing discloses this from any angle (the real trap)
#   v1_schema_gap  : the fact IS in the filing; v1's SQL schema just never
#                    ingested it, so v1_loop refuses. A raw-document reader
#                    answering it correctly is not a failure.
#   out_of_scope   : the company isn't in the provided corpus at all; any
#                    answer comes from the model's own knowledge.
_UNANSWERABLE_REASON = {
    "t3_unans_apple_supplier_share": "undisclosed",
    "unans_aapl_china_rev_2023": "v1_schema_gap",
    "unans_aapl_div_yield_2024": "v1_schema_gap",
    "unans_tsmc_rev_2024": "out_of_scope",
}


def score_numeric(item: dict, answer_text: str) -> dict[str, Any]:
    got_raw = _extract_answer_number(answer_text)
    correct = False
    if got_raw is not None and item.get("expected_value") is not None:
        # Try both signs: a magnitude from prose ("net loss of $70M", "decrease
        # of $347M") is unsigned; `expected_value` + tolerance disambiguate.
        candidates = [got_raw, -got_raw]
        if item.get("expected_unit") in ("%", "pp"):
            candidates += [got_raw * 100, -got_raw * 100]
        if item.get("tolerance_abs") is not None:
            correct = any(_within_tolerance_abs(c, item["expected_value"], item["tolerance_abs"])
                          for c in candidates)
        elif item.get("tolerance_pct") is not None:
            correct = any(_within_tolerance(c, item["expected_value"], item["tolerance_pct"])
                          for c in candidates)
    return {
        "type": "numeric",
        "correct": correct,
        "expected": item.get("expected_value"),
        "got_raw": got_raw,
        "refusal_detected": _is_refusal(answer_text),
    }


def score_retrieval(item: dict, answer_text: str) -> dict[str, Any]:
    lower = answer_text.lower()
    hit = False
    for gc in item.get("golden_citations") or []:
        kp = gc.get("key_phrase", "")
        if kp and kp.lower() in lower:
            hit = True
            break

    judge = _safe_judge(_llm_judge, item["question"], item.get("golden_answer", ""), answer_text)
    judge_score = judge.get("score", -1)
    judge_available = judge_score != -1
    # The judge is the authority: it has the golden answer and the rubric
    # ("all key facts present, no fabrications"). `passage_hit` (the verbatim
    # golden SEC phrase appearing in the answer) is reported but NOT a gate --
    # v1's harness checks it against RETRIEVED CHUNKS, but a generic agent has
    # no retrieval step, and demanding a verbatim quote in a free-form analyst
    # answer tests quotation, not comprehension (6/7 judge=3 answers failed on
    # this alone, devlog 002). Without a judge -> needs_review, re-score later.
    if judge_available:
        correct = judge_score >= 2
        needs_review = False
    else:
        correct = None
        needs_review = True
    return {
        "type": "retrieval",
        "correct": correct,
        "needs_review": needs_review,
        "passage_hit": hit,
        "judge_available": judge_available,
        "judge_score": judge_score,
        "judge_reason": judge.get("reason", ""),
    }


def _edges_ok_text(answer_text: str, expected_edges: list[dict]) -> bool:
    lower = answer_text.lower()
    found_pcts = [float(m) for m in _PCT_RE.findall(answer_text)]

    def edge_ok(edge: dict) -> bool:
        pct = edge.get("revenue_pct")
        if pct is None:
            return True
        if edge.get("threshold_only"):
            # ">= floor" disclosed -- a more precise number the answer found
            # elsewhere in the filing is not a miss, it's more precise than
            # what v1's own extraction captured for this edge.
            return any(p >= pct - _TIER3_PCT_TOLERANCE_ABS for p in found_pcts)
        return any(abs(p - pct) <= _TIER3_PCT_TOLERANCE_ABS for p in found_pcts)

    # Supplier must at least be named for its edge to count.
    def supplier_named(edge: dict) -> bool:
        s = edge.get("supplier")
        return not s or s.lower() in lower

    return all(supplier_named(e) and edge_ok(e) for e in expected_edges)


def score_tier3(item: dict, answer_text: str) -> dict[str, Any]:
    """Type-driven, mirroring harness_tier3.score_item_t3's branches."""
    q_type = item.get("type")
    scoring_mode = item.get("scoring")

    if q_type in ("graph_lookup", "graph_trend"):
        expected_edges = item.get("expected_edges") or []
        edges_ok = _edges_ok_text(answer_text, expected_edges)
        expected_suppliers = item.get("expected_suppliers") or []
        lower = answer_text.lower()
        suppliers_ok = (all(s.lower() in lower for s in expected_suppliers)
                        if expected_suppliers else True)
        return {"type": q_type, "correct": suppliers_ok and edges_ok,
                "suppliers_ok": suppliers_ok, "edges_ok": edges_ok}

    if q_type == "graph_fact":
        return {"type": q_type, **score_numeric(item, answer_text)}

    if q_type == "graph_comparison" or (q_type == "graph_compute" and scoring_mode == "llm_judge"):
        judge = _safe_judge(_llm_judge_graph, item["question"],
                            item.get("scoring_notes", ""), answer_text)
        judge_score = judge.get("score", -1)
        judge_available = judge_score != -1
        correct = (judge_score >= 2) if judge_available else None
        return {"type": q_type, "correct": correct, "needs_review": not judge_available,
                "judge_available": judge_available, "judge_score": judge_score,
                "judge_reason": judge.get("reason", "")}

    if q_type == "graph_compute":  # scoring_mode == "numeric"
        return {"type": q_type, **score_numeric(item, answer_text)}

    return {"type": q_type or "tier3_unknown", "correct": None,
            "needs_review": True, "reason": f"no scorer for type={q_type!r}"}


def score_item(item: dict, answer_text: str) -> dict[str, Any]:
    """Route one eval item to the right scorer and tag it with id/tier/question."""
    base = {"id": item.get("id"), "tier": item.get("tier"), "question": item.get("question")}

    if not item.get("answerable", True):
        correct = _is_refusal(answer_text)
        return {**base, "type": "unanswerable", "correct": correct,
                "refusal_detected": correct,
                "unanswerable_reason": _UNANSWERABLE_REASON.get(item.get("id"), "undisclosed")}

    if item.get("_dataset") == "eval_set_tier3" or "expected_ranking" in item:
        return {**base, **score_tier3(item, answer_text)}

    if item.get("type") == "retrieval":
        return {**base, **score_retrieval(item, answer_text)}

    return {**base, **score_numeric(item, answer_text)}
