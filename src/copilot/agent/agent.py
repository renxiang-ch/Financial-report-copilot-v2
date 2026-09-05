"""
Financial QA agent over any OpenAI-compatible endpoint.

The agent receives a question, calls tools to fetch numbers from SQL, and returns
a cited answer. The LLM never computes numbers itself -- and since a rule nobody
checks is a rule in name only, grounding.py checks it: every figure in the answer
is traced back to something a tool returned, and what cannot be traced is
labelled.

Which model answers is model_router.select_model's decision, not this module's.
"""

import json
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor, as_completed

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

from copilot.agent.model_router import select_model
from copilot.agent.conversation import (
    active_context_block,
    append_turn,
    carried_slots,
    to_openai_messages,
    trim_history,
)
from copilot.agent.clarify import as_text, clarification_for
from copilot.agent.grounding import accessions_in, verify_answer
from copilot.agent.provenance import build_provenance
from copilot.agent.slots import extract_slots
from copilot.agent.tools import compute, graph_query, list_metrics, query_financials, retrieve_text
from copilot.config import settings


# ── Tool router ────────────────────────────────────────────────────────────────
#
# Problem this fixes: tool selection was a soft LLM decision (system-prompt
# examples only). Eval regressions showed real instances of "tool displacement"
# — the model reaching for retrieve_text (unstructured 10-K text search) on
# questions where graph_query (structured supply_edges lookup) is the correct,
# verifiable source, and vice versa. See docs/case-study-tool-router.md.
#
# This router is a pre-classification step, not a rewrite of the agent loop:
# it pattern-matches the question and either (a) forces the FIRST tool call
# for one clear-cut category, or (b) short-circuits to a deterministic refusal
# before any LLM call at all for a category that is structurally unanswerable.
# Everything else falls through to the existing "auto" tool-choice behavior
# unchanged — the router only intervenes where the failure mode was observed.

ROUTER_ENABLED = True  # toggled off by eval harness for before/after ablation

_PROCUREMENT_REFUSAL_TEXT = (
    "I cannot determine this from available data. 10-K customer-concentration "
    "disclosures are supplier-reported: they state what percentage of the "
    "SUPPLIER's revenue comes from a named customer, not what percentage of the "
    "CUSTOMER's procurement, purchasing, or spending comes from that supplier. "
    "That figure is not disclosed in any filing in this database."
)


def route_question(question: str, carry: dict | None = None) -> dict:
    """Classify a question into a tool-routing category. Pure function, no I/O.

    Routes on the relationship the question is about, not on the words it uses.

    The previous version matched patterns against a hardcoded list of ten company
    names and a handful of phrasings. Measured on eighteen phrasings drawn from
    real failures rather than from its own patterns, it scored 38.9% -- while the
    harness written against those patterns reported 100%, because its questions
    were composed by reading the regex. Four answerable questions were refused
    outright and four structurally undisclosed ones were let through:

        "Which of Apple's suppliers is most at risk if procurement spending goes
        to competitors?"        -- answerable, refused, because it says
                                   "procurement" and "spending"
        "How much does Apple buy from Qorvo?"
                                -- undisclosed, allowed, because it contains none
                                   of the words the patterns were written around

    Both are the same mistake: counting words instead of asking which side of the
    relationship is being asked about. A 10-K concentration disclosure is
    supplier-reported, so a share of a SUPPLIER's revenue is answerable and a
    share of a CUSTOMER's spend is not, whatever vocabulary the question uses.
    `slots.relation` decides that from whose denominator the question names --
    see there for the rules and their evidence.

    Refusal is the dangerous action here, not tool forcing. A wrong `force_tool`
    costs one tool call the model can recover from; a wrong `refuse` returns
    nothing, spends no tokens, and gives the user no way to tell a missing fact
    from a misfiring classifier. So refusal is the branch that has to be sure of
    itself, and it declines to fire when the parse did not account for the whole
    question.
    """
    if not ROUTER_ENABLED:
        return {"category": "default", "action": "auto"}

    slots = extract_slots(question, carry=carry)

    # Something in the question was noticed and could not be represented -- a
    # region, a segment, a period qualifier. Acting confidently on a reading
    # that is known to be partial is how a reduced question gets a definite
    # answer, so nothing irreversible happens here.
    if slots["unaccounted"]:
        return {"category": "default", "action": "auto",
                "why": f"parse did not account for {slots['unaccounted']}"}

    side = slots["relation_side"]

    # Both parties must be named IN THIS QUESTION. "What share of Skyworks'
    # revenue comes from purchasing agreements?" is not a question about Apple's
    # budget, and refusing it would be the false refusal this rewrite exists to
    # remove.
    #
    # `resolved_question` means a party was supplied by an earlier turn rather
    # than by this one, and that is allowed to force a tool and never to refuse.
    # The asymmetry is the same one this docstring opens with: a wrong forced
    # call costs a round the model recovers from, while a refusal justified by a
    # company the question never mentions is unanswerable to the person holding
    # it -- they would have to guess which earlier turn the system is still
    # thinking about.
    if (side == "customer" and len(slots["companies"]) >= 2
            and not slots.get("resolved_question")):
        return {"category": "procurement_share", "action": "refuse",
                "why": slots["relation_basis"]}

    if side == "supplier":
        route = {"category": "dependency", "action": "force_tool",
                 "tool": "graph_query", "why": slots["relation_basis"]}
        if slots.get("resolved_question"):
            # Carried into the trace and the provenance: a pronoun this system
            # resolved on the reader's behalf is a reading, not a fact.
            route["resolved_question"] = slots["resolved_question"]
        return route

    return {"category": "default", "action": "auto"}


# OpenAI function-calling format.
# The metric names the model is told about, read from the same table
# query_financials serves from.
#
# This was a written-down list of ten while the database held twenty-four, and
# it was not harmless. Asked for Corning's total equity INCLUDING
# non-controlling interests, the model could not see that TotalEquityInclNCI
# exists, queried TotalEquity instead, and reported 10,686,000,000 with the
# qualifier attached -- the real figure for that label and year is between
# 11,070 and 12,545 million. Same shape asked for interest expense on debt.
# That is the substitution failure, reached through the lookup path rather than
# the arithmetic, where none of the answer checks can see it: the value really
# was fetched, no compute ran, and the company and year are right.
#
# The system prompt already called list_metrics "the authority on which metrics
# exist", which conceded the schema list was not. Now there is one authority.
#
# Read once per process and cached. A database that cannot be reached falls
# back to the ten names that were hardcoded here, so importing this module never
# requires a live connection -- the fallback is worse than the truth and better
# than a crash, and it is marked so a reader can tell which one they got.
_FALLBACK_METRICS = ("Revenue", "GrossProfit", "NetIncome", "OperatingIncome",
                     "EPS_Basic", "EPS_Diluted", "TotalAssets", "LongTermDebt",
                     "R&D", "COGS")


@lru_cache(maxsize=1)
def advertised_metrics() -> tuple[str, ...]:
    """Labels query_financials can actually serve, for the tool description."""
    try:
        from copilot.storage.db import get_conn
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT label FROM financial_facts "
                            "WHERE form = '10-K' ORDER BY label")
                labels = tuple(r["label"] for r in cur.fetchall())
        finally:
            conn.close()
        return labels or _FALLBACK_METRICS
    except Exception:
        return _FALLBACK_METRICS


def _metric_description() -> str:
    labels = advertised_metrics()
    note = "" if labels is not _FALLBACK_METRICS else (
        " (database unreachable at import; this list may be incomplete -- call "
        "list_metrics to confirm)")
    return ("The exact stored label. One of: " + ", ".join(labels) +
            ". Names are exact and case-sensitive; a near-miss is not a "
            "substitute -- if the metric you want is not on this list, say the "
            "data does not support the question rather than querying a "
            "similar-sounding one." + note)


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_metrics",
            "description": (
                "List all available financial metrics and fiscal years for a company. "
                "Call this when you are unsure what data exists, or to confirm year coverage "
                "before making a multi-step query."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "Stock ticker e.g. AAPL"},
                },
                "required": ["ticker"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_financials",
            "description": (
                "Fetch one financial metric for one company for one fiscal year from the database. "
                "IMPORTANT: call this once per data point — for multi-datapoint questions "
                "(ratios, YoY comparisons, cross-company) you MUST call it multiple times. "
                "Never guess or reuse a number across questions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "Stock ticker e.g. AAPL"},
                    "metric": {"type": "string", "description": _metric_description()},
                    "fiscal_year": {"type": "integer", "description": "Fiscal year e.g. 2024. Omit for latest."},
                    # `form` was here, offering 10-Q. The stored quarterly rows
                    # are year-to-date cumulative with no way to select a
                    # quarter, so the parameter advertised a capability that
                    # returned a wrong number rather than no number. Annual only
                    # until the ingestion layer separates the two.
                },
                "required": ["ticker", "metric"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compute",
            "description": (
                "Evaluate an arithmetic expression with named variables. "
                "Call this AFTER all needed query_financials calls are done. "
                "Never do arithmetic in your head — always use this tool."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": (
                            "Python arithmetic expression. Examples: "
                            "'gross_profit / revenue * 100' for margin, "
                            "'(new - old) / old * 100' for YoY growth."
                        ),
                    },
                    "variables": {
                        "type": "object",
                        "description": "Dict mapping variable names to numeric values from query_financials.",
                    },
                },
                "required": ["expression", "variables"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "retrieve_text",
            "description": (
                "Search 10-K filing text for qualitative questions: risk factors, MD&A commentary, "
                "business description, competitive position, strategy. "
                "Use this for 'why', 'how', 'what does the company say about' questions. "
                "Never use for numeric data — use query_financials for numbers. "
                "The search text is the user's own question, sent verbatim -- there is no "
                "query parameter to fill in. A compressed keyword rewrite measurably hurts "
                "recall here (6/6 vs 3/6 on a held-out probe: the words dropped in "
                "compression, usually the company name, are often the only terms that "
                "overlap the disclosure's actual wording). Just pass ticker and fiscal_year."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "Optional: restrict to one company e.g. AAPL"},
                    "k":      {"type": "integer", "description": "Number of passages to return (default 5)"},
                    "fiscal_year": {
                        "type": "integer",
                        "description": "Optional: restrict the search to one fiscal year's "
                                       "filing, e.g. 2024. Pass it whenever the question is "
                                       "about a specific year -- every company here has about "
                                       "ten filings and the same boilerplate repeats across "
                                       "them. Omit it for questions about change over time. "
                                       "If omitted, the most recent filing is searched and the "
                                       "result says which one.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "graph_query",
            "description": (
                "Query the supply-chain graph built from 10-K customer concentration disclosures "
                "(ASC 280: customers ≥10% of revenue must be disclosed). "
                "customer: return edges where this ticker is the customer. "
                "supplier: return edges where this ticker is the supplier. "
                "Pass both to query a specific supplier→customer pair. "
                "fiscal_year: 'latest' = most recent year for this company, "
                "'trend' = all available years, an integer year, or 'YYYY-YYYY' range. "
                "depth: 1 = direct relationships (default), 2 = suppliers of suppliers. "
                "Each edge includes revenue_pct (always numeric; the disclosed floor "
                "when threshold_only), pct_display (the string to print), "
                "threshold_only (true = text says '>10%' only, "
                "exact % not disclosed), citation (accession number), and source_text "
                "(verbatim sentence from the 10-K). Always surface traversal_trace and "
                "source_text in your final answer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "customer":    {"type": "string", "description": "Find suppliers of this customer e.g. AAPL"},
                    "supplier":    {"type": "string", "description": "Find customers of this supplier e.g. QRVO"},
                    "fiscal_year": {"type": "string", "description": (
                        "'latest' = most recent year for this company (default for snapshot questions). "
                        "'trend' = all available years (use when asked about a relationship or evolution over time). "
                        "'2024' = specific year. '2022-2025' = year range."
                    )},
                    "depth":       {"type": "integer", "description": "Hops to traverse (1=direct, 2=suppliers of suppliers)"},
                },
            },
        },
    },
]


SYSTEM = """You are a financial research assistant that answers analyst questions over SEC 10-K filings.
You have tools to fetch exact numbers from a database and search filing text. Never state numbers from memory.

## Core rules
1. Every number must come from query_financials — never recall or guess figures.
2. Every calculation must use compute — never do arithmetic in your head.
3. Cite every number with its accession number from the tool result.
4. If query_financials returns found=false, say you cannot determine that value.
5. list_metrics(ticker) is the authority on which metrics exist -- it returns the
   exact labels and fiscal years this database holds for that company. Do not decide
   from memory that a metric is missing; if you are unsure, call list_metrics first.
   If query_financials returns found=false, say you cannot determine that value.
   Deriving a figure from metrics that ARE present is correct and expected, provided
   every input came from a tool and compute did the arithmetic -- free cash flow from
   OperatingCashFlow and CapEx, or a debt-to-equity ratio from TotalDebt and
   TotalEquity, are real answers, not proxies. State which inputs you used.
   NEVER approximate a metric you could not fetch: do not stand in a similar-sounding
   label for the one that was asked for, and do not estimate a figure the database
   cannot support. Anything requiring data this database does not hold at all --
   share price and everything derived from it (dividend yield, market cap), geographic
   or segment revenue splits, headcount -- is unanswerable; say so.
6. NEVER substitute a different company's data when the asked-about company has no results.
   If the user asks about company X and the tool returns no data for X, say
   "I cannot find supply-chain data for X in the database." — do not pivot to
   other companies' data as a substitute answer. Answer only what was asked.
7. When the question specifies a fiscal year (e.g. "FY2024", "fiscal year 2024"), pass that
   EXACT year to BOTH query_financials AND graph_query. Never use fiscal_year="latest" when
   a specific year is given.
8. graph_query returns supplier-perspective data only: revenue_pct = what % of the SUPPLIER's
   revenue comes from that customer. It does NOT tell you what % of the CUSTOMER's procurement
   or spending comes from that supplier. Questions like "what % of Apple's procurement comes
   from QRVO?" or "what share of Apple's spending is QRVO?" are UNANSWERABLE — say so explicitly.

## Multi-step questions — follow this pattern exactly

Before calling any tool, identify ALL data points needed:

| Question type              | Required calls                                                     |
|----------------------------|--------------------------------------------------------------------|
| Margin (gross/op/net)      | query_financials ×2 (numerator + denominator) → compute            |
| YoY growth                 | query_financials ×2 (year N and year N-1) → compute                |
| Cross-company compare      | query_financials ×N (one per company per metric) → compute         |
| Trend (3 years)            | query_financials ×3 → present each with citation                   |
| Supplier exposure (Tier-3) | graph_query → query_financials ×N → compute ×N → rank              |

Example for gross margin:
  Step 1: query_financials(AAPL, GrossProfit, 2024)
  Step 2: query_financials(AAPL, Revenue, 2024)
  Step 3: compute("gross_profit / revenue * 100", {gross_profit: <val1>, revenue: <val2>})

Example for supplier exposure / order-cut impact analysis:
  Step 1: graph_query(customer="AAPL", fiscal_year=year) → get all suppliers + revenue_pct
  Step 2: query_financials(supplier, Revenue, year) × N → each supplier's total revenue
  Step 3: compute("revenue * pct / 100 * cut", {revenue: <val>, pct: <val>, cut: 0.20}) × N
          → dollar IMPACT per supplier (e.g. 20% cut = multiply by 0.20, NOT 1.0)
          IMPORTANT: "impact of a 20% cut" = total_apple_revenue × 0.20, not total_apple_revenue
  Step 4: rank by dollar impact, cite accession number per edge

Example for specific relationship question ("relationship between AAPL and SWKS"):
  Step 1: graph_query(customer="AAPL", supplier="SWKS", fiscal_year="trend")
  Step 2: State the relationship facts per fiscal year, cite accession number per edge

Example for supplier trend question ("CRUS dependency on Apple" / "CRUS revenue from Apple over time"):
  Step 1: graph_query(supplier="CRUS", fiscal_year="trend")
  — CRUS is the supplier (files the 10-K that discloses the %). Apple is its customer.
  — "Company X's dependency on Y" means X sells to Y → X=supplier param, Y=customer param.
  — NEVER pass the filing company (the one whose revenue_pct is disclosed) as customer.

## Qualitative questions
Use retrieve_text for risk factors, MD&A commentary, business descriptions, competitive position.

## Output format
State the result clearly with: value, fiscal year/period, and SEC citation (accession number).
For graph queries:
- Keep the answer concise — state facts and cite one accession number per edge.
- Do NOT quote source_text inline. The UI displays source_text in a separate Citations panel.
- threshold_only=true means the 10-K disclosed only a threshold ("more than ten
  percent"), not an exact figure. Reporting it and computing with it are separate:
  write edge.pct_display (">10%") in the answer text, and pass edge.revenue_pct
  (the disclosed floor, always a number) to compute when the question needs
  arithmetic. Never drop the percentage term because the exact figure is unknown —
  that silently treats the supplier as 100% dependent. Label any figure derived
  from a floor as a lower bound.
A figure that came out of `compute` is not something a filing reported. Give the
derivation with it — the inputs and what was done to them — and never write "as
reported in" or "according to the filing" about a computed figure; cite the
filings the INPUTS came from instead. A reader who can see
`(current assets - inventory) / revenue * 365` next to "days sales outstanding"
can tell it is not days sales outstanding; a reader given only the number cannot.
This applies to correct calculations too: 46.21% is a gross margin this system
worked out, not a number Apple published.
If a value cannot be determined, say so explicitly — never fabricate."""


_TOOLS = {
    "query_financials": query_financials,
    "list_metrics":     list_metrics,
    "compute":          compute,
    "retrieve_text":    retrieve_text,
    "graph_query":      graph_query,
}


def _collect_citations(steps: list[dict], answer: str) -> list[str]:
    """The sources this answer rests on -- not everything the tools touched.

    Derived after the fact, like provenance and verification, because the test
    for one of the two kinds needs the answer text and cannot be applied while
    the loop is still running.

    The two kinds are not the same and were being treated as if they were:

    * query_financials and graph_query return a fact the model ASKED FOR by name.
      If it came back, the answer is built on it, so its filing is a source.
    * retrieve_text returns the five passages nearest the query. They are
      CANDIDATES. The model reads them and may use one, or none.

    Listing all five as sources was measured across 121 saved answers: 98% of
    them presented at least one passage the answer never cited, many of them all
    five. A "Sources (5)" label above an answer that used none of them overstates
    its grounding, which is the opposite of what this system is for. Retrieved
    passages therefore count only once the answer points at them; every passage
    stays visible in the reasoning trace regardless.
    """
    cited = accessions_in(answer or "")
    out: list[str] = []

    def _add(text: str, *, require_cited: bool) -> None:
        text = str(text or "")
        if not text:
            return
        if require_cited:
            accns = accessions_in(text)
            if not accns or not (accns & cited):
                return
        if text not in out:      # one filing can back several facts; say it once
            out.append(text)

    for step in steps:
        result = step.get("output")
        if not isinstance(result, dict):
            continue
        requested = step.get("tool") in ("query_financials", "graph_query")
        _add(result.get("citation"), require_cited=not requested)
        for edge in result.get("edges") or []:
            _add(edge.get("citation"), require_cited=not requested)
        for passage in result.get("results") or []:
            _add(passage.get("citation"), require_cited=True)
    return out


def _run_tool(name: str, inputs: dict) -> str:
    """Execute one tool call and return its result as JSON.

    Never raises. The arguments come from the model, so a wrong argument name or
    a missing one is an ordinary event, not an exceptional one -- and until this
    caught them, `compute` called without `variables` (a mistake models really
    make) raised a TypeError that travelled out of the thread pool, out of the
    agent loop, and out of the API as a 500. The model controlled whether the
    request survived.

    A failure is fed back as a tool result instead, which is what every agent
    framework does with a bad call: LangChain retries the tool, Pydantic AI hands
    the validation error to the model via ModelRetry. Seeing "unexpected keyword
    argument 'year'" alongside the schema, the model can fix the call on the next
    round. The two kinds are labelled differently because only one is worth
    retrying -- a wrong argument is; a database that is down is not.
    """
    fn = _TOOLS.get(name)
    if fn is None:
        return json.dumps({"error": f"Unknown tool: {name}",
                           "available": sorted(_TOOLS)})
    try:
        return json.dumps(fn(**inputs))
    except TypeError as e:
        return json.dumps({
            "error": f"{name} was called with arguments it does not accept: {e}",
            "recoverable": True,
            "hint": "Check the tool schema and call it again with the correct "
                    "argument names. Do not answer from memory instead.",
        })
    except Exception as e:                       # noqa: BLE001 -- deliberate catch-all
        return json.dumps({
            "error": f"{name} failed: {type(e).__name__}: {e}",
            "recoverable": False,
            "hint": "This is an infrastructure failure, not a bad call. Do not "
                    "retry it and do not substitute a remembered value; say the "
                    "data could not be retrieved.",
        })


MAX_ROUNDS = 10
# Generous enough for a slow cold start on a free tier, short enough that a hung
# upstream surfaces as an error rather than as an apparently-still-working UI.
LLM_TIMEOUT_S = 90


def _ask_openai(question: str, model: str, route: dict,
                history: list[dict] | None = None,
                active_context: str | None = None) -> dict:
    # base_url empty -> api.openai.com. Set it to route every model through an
    # OpenAI-compatible aggregator without touching this loop.
    # A timeout, because every other outbound call in this codebase has one and
    # this one did not: a hung upstream held the request until the caller gave up.
    client = OpenAI(api_key=settings.openai_api_key,
                    base_url=settings.openai_base_url or None,
                    timeout=LLM_TIMEOUT_S, max_retries=2)

    # System prompt and tool schemas stay first and byte-identical across turns:
    # OpenAI caches on an exact prefix, so anything varying above the history
    # would forfeit the ~2.2k-token static block on every call.
    messages = [{"role": "system", "content": SYSTEM}]
    messages += to_openai_messages(history or [])
    # Below the history, so the prefix that was cacheable stays cacheable and
    # only the part of the prompt that is new anyway grows. None whenever the
    # question inherited nothing, which is every single-shot question.
    if active_context:
        messages.append({"role": "system", "content": active_context})
    messages.append({"role": "user", "content": question})

    steps = []
    total_input_tokens  = 0
    total_output_tokens = 0
    total_cached_tokens = 0

    for round_idx in range(MAX_ROUNDS):
        if round_idx == 0 and route["action"] == "force_tool":
            tool_choice = {"type": "function", "function": {"name": route["tool"]}}
        else:
            tool_choice = "auto"

        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=TOOL_SCHEMAS,
            tool_choice=tool_choice,
        )

        total_input_tokens  += response.usage.prompt_tokens
        total_output_tokens += response.usage.completion_tokens
        # Prefix caching is automatic on this endpoint, so the only thing to do
        # is read how much of it actually landed. Reported rather than assumed:
        # the discount is invisible in prompt_tokens, which counts cached and
        # fresh input alike.
        _details = getattr(response.usage, "prompt_tokens_details", None)
        total_cached_tokens += getattr(_details, "cached_tokens", 0) or 0

        msg = response.choices[0].message

        # No tool calls — model is done
        if not msg.tool_calls:
            answer_text = msg.content or ""
            break

        # Append assistant turn (with tool_calls)
        messages.append(msg)

        # Execute tool calls — parallel when multiple in one round
        def _exec(tc):
            name  = tc.function.name
            inp   = json.loads(tc.function.arguments)
            if name == "retrieve_text":
                # The model no longer controls the search text (see TOOL_SCHEMAS):
                # a compressed keyword rewrite measurably hurts recall (6/6 vs 3/6
                # on a held-out probe), almost always because the words it drops
                # -- typically the company name -- are the only ones that overlap
                # the disclosure's actual wording. `query` isn't in the schema
                # any more, but overwrite it unconditionally rather than only
                # filling it in when absent: a model that includes an unlisted
                # field anyway must not win out over the question it was actually
                # asked.
                inp["query"] = question
            out   = _run_tool(name, inp)
            return tc, name, inp, out

        if len(msg.tool_calls) == 1:
            ordered = [_exec(msg.tool_calls[0])]
        else:
            with ThreadPoolExecutor() as pool:
                futures = {pool.submit(_exec, tc): tc for tc in msg.tool_calls}
                id_order = {tc.id: i for i, tc in enumerate(msg.tool_calls)}
                ordered = [None] * len(msg.tool_calls)
                for future in as_completed(futures):
                    tc, name, inp, out = future.result()
                    ordered[id_order[tc.id]] = (tc, name, inp, out)

        for tc, tool_name, tool_input, result_str in ordered:
            result_data = json.loads(result_str)
            steps.append({"tool": tool_name, "input": tool_input, "output": result_data})


            messages.append({
                "role":         "tool",
                "tool_call_id": tc.id,
                "content":      result_str,
            })

    else:
        answer_text = (
            f"[Circuit breaker] Could not produce a final answer within {MAX_ROUNDS} "
            "tool-call rounds. Partial steps are recorded."
        )

    return {
        "answer":    answer_text,
        "steps":     steps,
        "citations": _collect_citations(steps, answer_text),
        "usage": {
            "input_tokens":  total_input_tokens,
            "output_tokens": total_output_tokens,
            "cached_input_tokens": total_cached_tokens,
            "cache_hit_rate": (round(total_cached_tokens / total_input_tokens, 4)
                               if total_input_tokens else 0.0),
        },
        "route": route,
    }


_UPSTREAM_FAILURE_TEXT = (
    "This query did not complete: {reason}. **This is a system failure, not a "
    "finding** -- nothing was looked up, so it says nothing about whether the "
    "data exists. Please try again."
)


def _upstream_failure(exc: Exception, route: dict) -> dict:
    """Turn a dead model endpoint into an answer, not a 500.

    Wording matters more than the mechanism here. "Cannot determine" is this
    system's phrase for a fact it went and checked for and did not find, and a
    user who reads that concludes the data is absent. A timeout checked nothing.
    Letting the two look alike would turn an outage into a false finding about
    the filings, which is a worse failure than the outage.

    provenance and verification still run over the empty trace below, so
    `refused` stays false: this answer is not a refusal, and nothing downstream
    should count it as one.
    """
    reason = {
        "APITimeoutError":    "the model did not respond in time",
        "APIConnectionError": "the model endpoint could not be reached",
    }.get(type(exc).__name__, f"the model endpoint returned an error ({type(exc).__name__})")
    return {
        "answer":    _UPSTREAM_FAILURE_TEXT.format(reason=reason),
        "steps":     [],
        "citations": [],
        "usage":     {"input_tokens": 0, "output_tokens": 0,
                      "cached_input_tokens": 0, "cache_hit_rate": 0.0},
        "route":     route,
        "upstream_error": type(exc).__name__,
    }


def ask(question: str, model: str | None = None,
        history: list[dict] | None = None) -> dict:
    """
    Run the agent loop for one turn of a conversation.

    model:   any model id the configured OpenAI-compatible endpoint serves.
    history: prior turns as [{"question", "answer"}, ...]. Omit for a single-shot
             question. See conversation.py for what is kept, what a follow-up
             inherits from it, and why. `context["inherited"]` names the
             constraints this turn took from earlier ones, so a caller can show
             the reader what was assumed on their behalf.

    Returns: {answer, steps, citations, usage, route, provenance, verification,
              history, context}
    where `history` already includes this turn, so a caller can hand it straight
    back on the next request.
    """
    kept_history, context_note = trim_history(history)
    carry = carried_slots(kept_history)
    route = route_question(question, carry=carry)

    # What this turn leaves unstated and the thread already established. The same
    # parse the router just made, made once more because the router discards
    # everything except its decision -- cheap, and the alternative is threading a
    # second return value through a function whose whole value is being a pure
    # classifier.
    #
    # Routing does see the carry, and is fenced rather than blinded: a company
    # supplied by an earlier turn can force a tool and can never cause a refusal.
    # See route_question for why those two are not the same risk.
    slots = extract_slots(question, carry=carry)
    active_context = active_context_block(slots)
    context_note["inherited"] = slots["inherited"]
    context_note["active_context"] = active_context
    # `route` is dropped at the API boundary (AnswerResponse does not declare
    # it), and a rewrite the reader cannot see is a rewrite the reader cannot
    # object to. This is the field that survives the trip.
    context_note["resolved_question"] = slots.get("resolved_question")

    # Handing the question back, before anything is spent on answering the wrong
    # reading of it. After the refusal check on purpose: a question that cannot
    # be answered under ANY reading is answered by saying so, not by offering a
    # menu of readings that all fail.
    clarification = (None if route["action"] == "refuse"
                     else clarification_for(question, slots, carry))
    if clarification:
        result = {
            "answer":    as_text(clarification),
            "needs_clarification": True,
            "clarification": clarification,
            "steps":     [],
            "citations": [],
            "usage":     {"input_tokens": 0, "output_tokens": 0,
                          "cached_input_tokens": 0, "cache_hit_rate": 0.0},
            "route":     route,
            "model_route": {"selected": None, "reason": "no model was called"},
        }
    elif route["action"] == "refuse":
        result = {
            "answer":    _PROCUREMENT_REFUSAL_TEXT,
            "steps":     [],
            "citations": [],
            "usage":     {"input_tokens": 0, "output_tokens": 0,
                          "cached_input_tokens": 0, "cache_hit_rate": 0.0},
            "route":     route,
        }
    else:
        chosen, why = select_model(model)
        try:
            result = _ask_openai(question, chosen, route, kept_history,
                                 active_context=active_context)
        except (APITimeoutError, APIConnectionError, APIStatusError) as e:
            result = _upstream_failure(e, route)
        result["model_route"] = {"selected": chosen, "reason": why}

    # Attached here rather than inside each provider path so every answer carries
    # one -- including the router's zero-tool refusal, whose provenance is the
    # honest "nothing was fetched, and here is why nothing could be".
    steps = result.get("steps") or []
    answer = result.get("answer") or ""
    result["provenance"] = build_provenance(steps, answer)
    # The verification is deliberately separate from the provenance record.
    # Provenance describes where an answer came from; this says whether the
    # answer's own figures survive being checked against that. One is a
    # description, the other is a verdict, and merging them would let a
    # reassuring description stand in for a failed check.
    result["verification"] = verify_answer(steps, answer, question)
    result["context"] = context_note
    # A question that was handed back was not answered, so it is not a turn. The
    # rewrite the reader picks arrives as a fresh question against this same
    # history -- which is why the options carry sentences rather than codes.
    result["history"] = (kept_history if result.get("needs_clarification")
                         else append_turn(kept_history, question, result["answer"]))
    return result
