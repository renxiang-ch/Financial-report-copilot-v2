"""Check an answer against the tool trace that produced it.

The project's cardinal rule is that numbers come from SQL and `compute`, never
from the model. That rule is only worth as much as its enforcement, and until now
nothing enforced it: a fabricated figure and a fetched one look identical once
they are inside a fluent paragraph.

This module makes the rule checkable. Every number a correct answer states must
appear in something a tool returned, so a number that appears nowhere in the
trace is either invented or computed in the model's head -- both violations. The
check is a pure function over (steps, answer): no LLM, no network, no training,
and deterministic given a trace.

What it catches
    - figures stated in the answer that no tool ever returned
    - large values fed INTO `compute` that no data tool produced, which is how a
      fabricated number gets laundered into a "computed" one
    - accession numbers cited in the answer that no tool returned
    - a supply-edge percentage bound to the wrong company's revenue, or to the
      wrong year's -- correct arithmetic over the wrong inputs

How strong the check is depends on the trace
    A figure quoted out of a retrieved passage is genuinely sourced, so every
    number appearing in every retrieved chunk joins the grounded set. Five
    chunks of a financial statement contribute hundreds of numbers, and against
    a set that large almost any stated figure finds a match. The check is
    therefore strict on answers built from `query_financials` and `graph_query`,
    and permissive on answers built from `retrieve_text`. `passage_values` in
    the record says how many values came from passages, so a caller can see
    which regime an answer was judged under instead of reading a green tick as
    if the two were equivalent.

What it does not catch, and this bound still matters
    Correct arithmetic over the wrong inputs, in general. One sub-case is now
    decidable -- a `revenue_pct` is a share of a named supplier's revenue for a
    named year, so binding it to anyone else's revenue is wrong without needing
    to know what was asked -- but that is a sub-case, not the class. Nothing here
    can tell that the model answered about gross margin when the question was
    about operating margin, or picked the wrong one of two plausible metrics.
    Those need the question's intent, which is exactly the judgement this module
    refuses to hand to a model.

The response to a violation is a label on the answer, not suppression of it. An
answer with four verified figures and one unverified one is more useful to an
analyst with the fifth flagged than withheld entirely, and a red flag the machine
raised is a stronger claim than a disclaimer the author wrote in advance.
"""

import ast
import re

# Scale words that follow a figure in prose. A stated "118,254 million" and a
# stored 118254000000 are the same fact, so both readings are tried before
# anything is called unverified.
_SCALES = {
    "thousand": 1e3, "k": 1e3,
    "million": 1e6, "m": 1e6, "mm": 1e6,
    "billion": 1e9, "b": 1e9, "bn": 1e9,
    "trillion": 1e12, "t": 1e12,
}
_NUM_RE = re.compile(
    r"(-?\$?\s?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\$?\s?\d+(?:\.\d+)?)"
    r"\s*(thousand|million|billion|trillion|mm|bn|[kmbt])?"
    r"\s*(%|percent|percentage points|pp)?",
    re.IGNORECASE,
)
_ACCN_RE = re.compile(r"\d{10}-\d{2}-\d{6}")
# EDGAR writes the same accession two ways: dashed in prose, undashed in the URL
# path. Both are normalised to the undashed form before comparing, so a citation
# given as a link is verified rather than ignored.
_ACCN_URL_RE = re.compile(r"/(\d{18})")
# Numbers inside a URL are path components -- a CIK, an undashed accession, a
# date in a filename -- not figures. Left in the prose they parse as enormous
# dollar amounts that no tool returned, which is how five correct answers came
# back flagged.
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)

# Relative tolerance for "the answer states a rounded form of this value".
# $311,266,860 written as "$311.3 million" is a match; a different figure is not.
_REL_TOL = 0.005
# Below this magnitude a bare number is not a financial figure -- it is a year, a
# count of suppliers, a rank, a multiplier. Percentages are exempt from the floor
# and checked anyway: they are the load-bearing number in every dependency claim.
_MIN_MAGNITUDE = 1000.0


def _stated_numbers(text: str) -> list[tuple[float, list[float], bool]]:
    """(as written, candidate values, is a percentage) for each number in `text`.

    A figure gets more than one candidate when a scale word follows it, because
    prose and the database disagree about units far more often than they disagree
    about facts.
    """
    out: list[tuple[float, list[float], bool]] = []
    for raw, scale, pct in _NUM_RE.findall(text or ""):
        cleaned = raw.replace("$", "").replace(",", "").replace(" ", "")
        try:
            val = float(cleaned)
        except ValueError:
            continue
        cands = [val]
        if scale:
            cands.append(val * _SCALES[scale.lower()])
        out.append((val, cands, bool(pct)))
    return out


def _is_year(val: float) -> bool:
    return float(val).is_integer() and 1990 <= val <= 2100


def _accession_pairs(text: str) -> list[tuple[str, str]]:
    """(as written, normalised) for every accession in `text`.

    Comparison uses the normalised form so a dashed citation in prose and the
    undashed one in its URL are recognised as the same filing. Reporting uses the
    written form: telling a reader that 999999999999999999 is unverified, when
    what they can see on the page is 9999999999-99-999999, is a worse message
    than not flagging it.
    """
    pairs = [(a, a.replace("-", "")) for a in _ACCN_RE.findall(text or "")]
    pairs += [(a, a) for a in _ACCN_URL_RE.findall(text or "")]
    return pairs


def accessions_in(text: str) -> set[str]:
    """Every filing `text` refers to, normalised, in either notation.

    Public and shared. Three modules were each carrying their own copy of the
    accession pattern and they had already drifted: this one learned the
    undashed URL form, the other two did not, so an answer that linked to a
    filing instead of spelling out its accession counted as cited here and
    uncited there. One definition, imported.
    """
    return {norm for _, norm in _accession_pairs(text)}


def dashed_accession(norm: str) -> str:
    """An accession in the form a person reads: NNNNNNNNNN-NN-NNNNNN.

    `accessions_in` normalises to the undashed 18 digits so the two notations
    compare equal. That form is right for comparison and wrong for display --
    telling a reader their answer cites 000032019324000123 when the page says
    0000320193-24-000123 is a worse message than saying nothing.
    """
    d = "".join(ch for ch in (norm or "") if ch.isdigit())
    return f"{d[:10]}-{d[10:12]}-{d[12:18]}" if len(d) == 18 else norm


# Kept for the module's own call sites, which predate the public name.
_accessions_in = accessions_in


def _matches(cands: list[float], grounded: set[float]) -> bool:
    # Both signs are tried. Prose carries direction in words -- "revenue decreased
    # by 2.80%" against a computed -2.8004 is the same fact, and flagging it would
    # mean the check fires on correct answers, which is the one thing a red flag
    # must not do. Whether the sign is RIGHT is the harness's numeric comparison
    # to do; this asks only whether the figure has a source.
    for c in [c for c in cands] + [-c for c in cands]:
        for g in grounded:
            if abs(c - g) <= _REL_TOL * max(abs(g), 1.0):
                return True
            # Prose and storage also disagree by whole SI steps without a scale
            # word ever appearing ("revenue of 391,035" for 391035000000).
            for factor in (1e3, 1e6, 1e9):
                if abs(c * factor - g) <= _REL_TOL * max(abs(g), 1.0):
                    return True
    return False


def value_appears_in(text: str, value: float) -> bool:
    """Is `value` stated somewhere in `text`, allowing for how prose writes it?

    Public because the eval harness needs the same reading of "the answer says
    83,560,000" that this module uses -- scale words, thousands separators,
    rounding, and direction carried by a word rather than a sign. Two different
    notions of "the answer states this number" in one codebase would eventually
    disagree, and the disagreement would show up as a scoring mystery.
    """
    for _, cands, _pct in _stated_numbers(text or ""):
        if _matches(cands, {float(value)}):
            return True
    return False


# ── Refusal detection ────────────────────────────────────────────────────────
#
# Two readings of "this answer refused", kept apart on purpose and kept here so
# the difference is visible rather than spread across two files that quietly
# disagree.
#
# STRICT is first-person only: the system saying it will not answer. It exists
# because the obvious wording matches things that are not refusals -- a bare
# "not disclosed" appears in the entirely correct sentence explaining that a
# threshold disclosure gives no exact figure, and a caveat attached to a real
# answer is the opposite of a refusal. provenance.py uses this, and still does
# not decide on wording alone: an answer that fetched data and computed with it
# did not refuse, however it is phrased.
#
# BROAD accepts third-person absence as well ("no data for that", "not found"),
# because the eval harness scores whether the QUESTION went unanswered, not who
# said so.
#
# What was removed from BROAD, and why it matters: it contained "not in" and
# "outside", which match inside ordinary prose. Measured across every saved
# answer, that made one correct retrieval answer read as a refusal -- "Skyworks
# Solutions serves a variety of end markets..." -- with nothing to catch it,
# because a false refusal detection on a non-refusal question changes no score
# and so is invisible. Replaced with the bounded forms.
REFUSAL_STRICT = (
    "i cannot determine", "i cannot find", "cannot be determined",
    "i am unable to", "unanswerable", "not available in this database",
)

REFUSAL_BROAD = (
    "cannot determine", "can't determine", "unable to determine",
    "cannot be determined", "can't be determined",
    "cannot answer", "unable to answer", "cannot find",
    "do not have", "don't have", "not available", "no data",
    "not found", "not tracked", "not in our database",
    "not in the database", "not covered", "does not support",
)


def looks_like_refusal(text: str, strict: bool = False) -> bool:
    """Does this answer decline to give a figure?

    Wording only. Neither caller treats it as sufficient on its own -- see the
    note above and provenance.build_provenance, which requires the absence of a
    fetched figure as well.
    """
    lower = (text or "").lower()
    return any(p in lower for p in (REFUSAL_STRICT if strict else REFUSAL_BROAD))


# ── One index over the trace ─────────────────────────────────────────────────
#
# Three checks need to know where a number came from, and until now each built
# its own index over `steps`:
#
#     collect_grounded   values a tool produced          (is this figure sourced)
#     _tag_values        value -> ticker, fiscal year    (is it bound to the right subject)
#     authority._fact_index  value -> label, year, ticker (does the formula have a source)
#
# They had already diverged. One indexed an edge percentage as 46.0, another as
# both 46.0 and 0.46, the third as both plus a ticker; tolerances were 1e-6,
# 0.5%-with-SI-steps, and round-to-4-places. Extending any one of them to a new
# numeric form would have left the other two behind, and all three answer
# questions about the same trace.
#
# This is a variant of the defect this project keeps hitting -- not a list
# drifting from its data, but three indexes drifting from each other, which is
# harder to see because all three work and differ only at the edges.

class Fact:
    """One number a tool returned, and what it is a number OF."""

    __slots__ = ("value", "kind", "label", "ticker", "fiscal_year")

    def __init__(self, value: float, kind: str, label: str = "",
                 ticker: str = "", fiscal_year=None):
        self.value = value
        self.kind = kind              # "metric" | "edge_pct" | "passage" | "computed"
        self.label = label            # canonical label, or "" for non-metrics
        self.ticker = ticker          # for an edge this is the SUPPLIER
        self.fiscal_year = fiscal_year

    def __repr__(self) -> str:
        return f"Fact({self.value}, {self.kind}, {self.label or self.ticker})"


def index_trace(steps: list[dict]) -> list[Fact]:
    """Every number the tools produced, in one pass, tagged with what it is.

    An edge percentage is emitted twice, as 46.0 and as 0.46, because the model
    writes it both ways and a value that fails to match is treated by callers as
    unsourced -- missing one reading manufactures a false alarm rather than a
    missed one.
    """
    facts: list[Fact] = []
    for step in steps:
        out = step.get("output")
        if not isinstance(out, dict):
            continue
        tool = step.get("tool")

        if tool == "query_financials" and out.get("found"):
            try:
                facts.append(Fact(float(out["value"]), "metric",
                                  str(out.get("metric") or ""),
                                  str(out.get("ticker") or "").upper(),
                                  out.get("fiscal_year")))
            except (KeyError, TypeError, ValueError):
                pass

        for edge in out.get("edges") or []:
            try:
                pct = float(edge["revenue_pct"])
            except (KeyError, TypeError, ValueError):
                continue
            supplier = str(edge.get("supplier") or "").upper()
            fy = edge.get("fiscal_year")
            facts.append(Fact(pct, "edge_pct", "", supplier, fy))
            facts.append(Fact(pct / 100.0, "edge_pct", "", supplier, fy))
            try:
                facts.append(Fact(float(fy), "edge_year", "", supplier, fy))
            except (TypeError, ValueError):
                pass

        # A figure quoted out of a retrieved passage is sourced, even though the
        # tool returned prose rather than a number. Kept a separate kind so a
        # caller can tell a permissive match from a strict one.
        for res in out.get("results") or []:
            for _, cands, _pct in _stated_numbers(str(res.get("text") or "")):
                for c in cands:
                    facts.append(Fact(c, "passage"))

        for metric in out.get("metrics") or []:
            for year in metric.get("years_available") or []:
                try:
                    facts.append(Fact(float(year), "metric_year"))
                except (TypeError, ValueError):
                    pass

        if tool == "compute" and out.get("ok"):
            try:
                facts.append(Fact(float(out["result"]), "computed"))
            except (KeyError, TypeError, ValueError):
                pass

    return facts


# Near-exact. Compute inputs are copied verbatim out of tool output; when the
# model rescales instead, the value simply finds no fact and the calculation
# goes unchecked. Missing a check is the safe failure, claiming a false one is
# not. Deliberately NOT the same as `_matches`, which is deliberately loose
# because it compares against PROSE.
def _same_value(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-6 * max(abs(a), abs(b), 1.0)


def facts_for(value: float, facts: list[Fact]) -> list[Fact]:
    """Every fact this exact value could be."""
    return [f for f in facts if _same_value(f.value, value)]


def collect_grounded(steps: list[dict]) -> tuple[set[float], set[float], set[str], int]:
    """Return (values from data tools, all tool-produced values, accessions,
    how many of those values came from retrieved passages).

    The first set is deliberately narrower than the second: a `compute` result is
    tool-produced but not independently sourced, so it may ground the ANSWER
    while it may not ground another `compute` call's inputs.

    Reads the shared index rather than walking `steps` itself -- see index_trace
    for why there is only one walk now.
    """
    facts = index_trace(steps)
    from_data = {f.value for f in facts if f.kind != "computed"}
    all_values = set(from_data) | {f.value for f in facts if f.kind == "computed"}
    passage_values = {f.value for f in facts if f.kind == "passage"}

    accns: set[str] = set()
    for step in steps:
        out = step.get("output")
        if not isinstance(out, dict):
            continue
        for edge in out.get("edges") or []:
            accns |= accessions_in(str(edge.get("citation") or ""))
        for res in out.get("results") or []:
            accns |= accessions_in(str(res.get("citation") or ""))
        for blob in (out.get("accn"), out.get("citation")):
            accns |= accessions_in(str(blob or ""))

    return from_data, all_values, accns, len(passage_values)


def _expression_literals(expression) -> list[float]:
    """Every numeric literal written directly into a compute expression.

    Parsed rather than pattern-matched, so `1e9` and a minus sign attached to a
    number are read the way Python reads them and a variable named `x2` is not
    mistaken for one.
    """
    if not isinstance(expression, str) or not expression.strip():
        return []
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        return []
    out: list[float] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float))                 and not isinstance(node.value, bool):
            out.append(float(node.value))
    return out


# ---------------------------------------------------------------------------
# Whose fact is it, and from which year
# ---------------------------------------------------------------------------
# The check above asks whether a figure has a source. It cannot ask whether the
# source was the RIGHT one, and the difference is not academic: asked for the
# impact on Skyworks, the model fetched Apple's revenue, multiplied it by the
# Skyworks->Apple concentration, and produced a figure 94x too large in which
# every single operand came from a tool. The grounding check passed it.
#
# One sub-case of "the wrong source" is mechanically decidable, and it happens
# to be the one this domain turns on. A supply-edge percentage is not a free
# number: `revenue_pct` on a SWKS->AAPL edge means "Apple is 10% of SKYWORKS'
# revenue". It is a share OF THE SUPPLIER'S revenue, for one fiscal year. So
# multiplying it by anything other than that supplier's revenue for that year is
# wrong regardless of what the question was, and no interpretation of intent is
# needed to say so.
#
# Deliberately narrow. A blanket "these inputs came from different years" rule
# would fire on every correct year-over-year question in the frozen set, and a
# red flag that lands on correct answers destroys the only thing a red flag has.
# So the rule is anchored to supply edges, which is where both observed failures
# live -- the entity inversion above, and the multi-turn case where FY2026's 91%
# was carried onto FY2024 revenue.

def _binding_mismatches(steps: list[dict], question: str = "") -> list[str]:
    """Compute calls that bind a supply-edge percentage to the wrong company or year.

    A call is reported only when NO consistent reading of its inputs exists. If
    the same figures can be read as a correct calculation -- because a value is
    ambiguous, or because the right revenue is also present -- that reading wins.
    Ambiguity resolves in the answer's favour.
    """
    facts = index_trace(steps)
    if not facts:
        return []

    from_question: list[float] = []
    for _, cands, _pct in _stated_numbers(question or ""):
        from_question.extend(cands)
        from_question.extend(c / 100.0 for c in cands)

    problems: list[str] = []
    for step in steps:
        if step.get("tool") != "compute":
            continue
        inp = step.get("input") or {}
        values = [v for v in (inp.get("variables") or {}).values()]
        values += _expression_literals(inp.get("expression"))

        edges: list[Fact] = []
        metrics: list[Fact] = []
        for raw in values:
            try:
                val = float(raw)
            except (TypeError, ValueError):
                continue
            hits = facts_for(val, facts)
            # A figure the user put in the question is theirs, not an edge's,
            # even when some edge happens to carry the same percentage.
            if any(_same_value(val, q) for q in from_question):
                hits = [f for f in hits if f.kind != "edge_pct"]
            edges += [f for f in hits if f.kind == "edge_pct"]
            metrics += [f for f in hits if f.kind == "metric"]

        if not edges or not metrics:
            continue      # nothing binds to anything; not this check's business

        # A percentage of the supplier's revenue has to meet the supplier's revenue.
        same_company = [(e, m) for e in edges for m in metrics if e.ticker == m.ticker]
        if not same_company:
            suppliers = sorted({e.ticker for e in edges})
            got = sorted({m.ticker for m in metrics})
            problems.append(
                f"{inp.get('expression', 'compute')}: uses a concentration "
                f"disclosed by {'/'.join(suppliers)}, but the only revenue in the "
                f"calculation belongs to {'/'.join(got)}. That percentage is a "
                f"share of {'/'.join(suppliers)}' revenue, not of {'/'.join(got)}'."
            )
            continue

        if not any(e.fiscal_year == m.fiscal_year for e, m in same_company):
            years_e = sorted({str(e.fiscal_year) for e, _ in same_company})
            years_m = sorted({str(m.fiscal_year) for _, m in same_company})
            problems.append(
                f"{inp.get('expression', 'compute')}: applies an FY"
                f"{'/'.join(years_e)} concentration to FY{'/'.join(years_m)} "
                f"revenue."
            )
    return problems


def _unsourced_formulas(steps: list[dict], answer: str, question: str) -> list[str]:
    """Calculations reaching the answer whose formula has no source.

    The check next door asks whether every figure came from a tool. This one
    asks something a fully-sourced answer can still fail: where did the FORMULA
    come from. Days sales outstanding computed as
    `(current_assets - inventory) / revenue * 365` draws every operand from the
    database and is not days sales outstanding.

    Only calculations whose result reaches the answer are reported. An
    intermediate the model computed and discarded is not a claim about anything.

    See authority.py for how a source is established, and for why the check is
    which stored labels fed the calculation rather than what the expression says.
    """
    from copilot.agent.authority import classify, describe

    qvals: set[float] = set()
    for _, cands, _pct in _stated_numbers(question or ""):
        qvals.update(cands)
        qvals.update(c / 100.0 for c in cands)

    out: list[str] = []
    for step in steps:
        if step.get("tool") != "compute":
            continue
        output = step.get("output")
        if not isinstance(output, dict) or not output.get("ok"):
            continue
        try:
            result = float(output["result"])
        except (KeyError, TypeError, ValueError):
            continue
        if not value_appears_in(answer or "", result):
            continue
        authority, detail = classify(step, steps, question, qvals)
        if authority is None:
            line = describe(detail)
            if line not in out:
                out.append(line)
    return out


def verify_answer(steps: list[dict], answer: str, question: str = "") -> dict:
    """Return the verification record for one answer.

    `question` is not evidence, but it is a source: a figure the user supplied
    ("if Apple cuts orders by 20%") is theirs, and echoing it back is not
    fabrication. Repeating a false premise the user asserted is a real failure,
    but a different one -- it belongs to adversarial evaluation, not to a check
    for numbers that came from nowhere.
    """
    from_data, all_values, accns, passage_values = collect_grounded(steps)
    for _, cands, _pct in _stated_numbers(question or ""):
        all_values.update(cands)

    # Accessions and URLs are digits too, and left in the text they parse as
    # implausible dollar figures -- a CIK, an undashed accession, a date inside a
    # filename. Both are removed here and verified separately, below.
    prose = _URL_RE.sub(" ", _ACCN_RE.sub(" ", answer or ""))

    unverified: list[float] = []
    checked = 0
    for as_written, cands, is_pct in _stated_numbers(prose):
        if not is_pct and (_is_year(as_written) or abs(as_written) < _MIN_MAGNITUDE):
            continue
        if is_pct:
            # "-0.42%" in prose and -0.004184 from compute are the same fact.
            # The model puts the *100 inside the expression about half the time
            # and outside it the rest, so the tool's result is a fraction or a
            # percentage depending on a coin flip -- and this check flagged the
            # correct answer whenever the coin landed the other way. The eval
            # harness already bridged this when v2 was written; the verifier
            # never got the same treatment, which is the same two-readings
            # problem in a second place.
            #
            # Only for figures WRITTEN as percentages: bridging every number by
            # a factor of 100 would let a fabricated 46 pass against a real 0.46.
            cands = list(cands) + [c / 100.0 for c in cands]
        checked += 1
        if not _matches(cands, all_values) and as_written not in unverified:
            unverified.append(as_written)

    # Inputs to `compute` are checked against data tools only. Letting one
    # compute result ground another compute's input would let an invented number
    # wash itself clean in two steps.
    laundered: list[str] = []
    for step in steps:
        if step.get("tool") != "compute":
            continue
        inp = step.get("input") or {}
        candidates = [(str(k), v) for k, v in (inp.get("variables") or {}).items()]
        # Literals written INTO the expression count as inputs too. Checking only
        # the variables dict left the whole test bypassable by writing the number
        # in the string instead -- `compute("9500000000 - 1000000", {})` passed
        # while the identical fabrication passed through `variables` was caught.
        # The agent really does write this form: five expressions in the saved
        # traces inline their operands, e.g.
        # `(4178000000 - 4772400000) / 4772400000 * 100`.
        candidates += [("literal", n) for n in _expression_literals(inp.get("expression"))]
        for name, val in candidates:
            try:
                fval = float(val)
            except (TypeError, ValueError):
                continue
            if abs(fval) < _MIN_MAGNITUDE or _is_year(fval):
                continue          # question-derived constants, not fetched facts
            if not _matches([fval], from_data):
                entry = f"{name}={fval:,.0f}"
                if entry not in laundered:
                    laundered.append(entry)

    uncited = sorted({written for written, norm in _accession_pairs(answer or "")
                      if norm not in accns})

    misbound = _binding_mismatches(steps, question)
    unsourced_formulas = _unsourced_formulas(steps, answer, question)

    return {
        "unverified_numbers":   unverified,
        "unsourced_inputs":     laundered,
        "unverified_citations": uncited,
        # A calculation whose every operand is sourced can still bind the wrong
        # company's revenue to a percentage, or the wrong year's. Those answers
        # pass all three checks above; this one is why they no longer pass.
        "misbound_inputs":      misbound,
        # Deliberately NOT part of `verified`. An unsourced formula is not a
        # detected error -- `total_equity / (total_assets - total_debt)` is
        # arithmetically fine, it just is not a definition of book value per
        # share, and only a reader can say that. Reporting it as a failure would
        # put a red flag on every legitimate novel calculation; reporting it as
        # nothing would leave the reader unable to tell one from the other. So it
        # is recorded, shown, and left to them.
        "unsourced_formulas":   unsourced_formulas,
        "verified":             not (unverified or laundered or uncited or misbound),
        # How many figures in the answer were actually put to the test. Zero means
        # the answer stated no checkable figure, which is not the same as passing,
        # and the caller must not render it as one.
        "numbers_checked":      checked,
        "citations_checked":    len(_accessions_in(answer or "")),
        # How many grounding values came from retrieved prose rather than a
        # structured lookup. High means a permissive check, not a strong pass.
        "passage_values":       passage_values,
    }
