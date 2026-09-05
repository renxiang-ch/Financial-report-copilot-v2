"""Derive an answer's provenance record from the agent's tool trace.

Nothing here asks the model how confident it is. Every field is read off what
the tools actually did, so the record cannot drift from the answer it describes
and cannot be talked up by a model that is wrong but fluent. That also makes it
testable: given a trace, the output is fixed.

The limitations list is the point of the whole module. A number can be perfectly
correct and still mislead -- a floor treated as an estimate, an absence in the
top-5 passages read as an absence in the filing -- and those are exactly the
readings a confident-sounding paragraph invites. Each entry below is emitted
only when the trace shows the corresponding condition really applies.
"""

from copilot.agent.grounding import REFUSAL_STRICT as _REFUSAL_MARKERS


def _accessions(steps: list[dict]) -> list[str]:
    """Every distinct SEC accession the tools attached to this answer.

    Reads through grounding.accessions_in rather than matching here. This
    function had its own copy of the pattern and it had already drifted: it
    matched only the dashed form written in prose, while grounding also reads
    the undashed form EDGAR puts in a URL path. An answer citing a filing by
    link therefore counted as cited in one record and uncited in the other, and
    both records are returned from the same call.

    That is the fourth copy of this pattern. Three were unified on 2026-08-24
    and this one was missed, which is why it is now an import and not a regex.
    """
    from copilot.agent.grounding import accessions_in, dashed_accession

    found: list[str] = []
    for step in steps:
        out = step.get("output")
        if not isinstance(out, dict):
            continue
        blobs = [str(out.get("accn") or ""), str(out.get("citation") or "")]
        for edge in out.get("edges", []) or []:
            blobs.append(str(edge.get("citation") or ""))
        for res in out.get("results", []) or []:
            blobs.append(str(res.get("citation") or ""))
        for blob in blobs:
            for accn in accessions_in(blob):
                written = dashed_accession(accn)
                if written not in found:
                    found.append(written)
    return found


def build_provenance(steps: list[dict], answer: str) -> dict:
    """Return a provenance record for one answered question."""
    used = {s.get("tool") for s in steps}

    fetched = [
        s for s in steps
        if s.get("tool") == "query_financials"
        and isinstance(s.get("output"), dict) and s["output"].get("found")
    ]
    edges = [
        e for s in steps
        if s.get("tool") == "graph_query" and isinstance(s.get("output"), dict)
        for e in (s["output"].get("edges") or [])
    ]
    computes = [
        s["input"].get("expression", "")
        for s in steps
        if s.get("tool") == "compute"
        and isinstance(s.get("output"), dict) and s["output"].get("ok")
    ]
    accns = _accessions(steps)
    floors = [e for e in edges if e.get("pct_is_floor") or e.get("threshold_only")]

    # A refusal is the absence of a delivered figure, not the presence of hedging
    # language, so wording alone never decides it: an answer that fetched data and
    # computed with it did not refuse, however many caveats it carries.
    refused = (
        not fetched and not edges
        and any(m in (answer or "").lower() for m in _REFUSAL_MARKERS)
    )

    if fetched:
        numeric_source = "XBRL structured data (SEC filings), read by SQL"
    elif edges:
        numeric_source = "Percentages quoted from 10-K disclosure text"
    else:
        numeric_source = "No figure was fetched for this answer"

    if not edges:
        relationship_source = None
    elif floors and len(floors) == len(edges):
        relationship_source = "Supplier 10-K disclosure — threshold only (lower bound)"
    elif floors:
        relationship_source = (
            f"Supplier 10-K disclosure — {len(edges) - len(floors)} exact, "
            f"{len(floors)} threshold only (lower bound)"
        )
    else:
        relationship_source = "Supplier 10-K disclosure — exact percentage"

    computation = (
        f"Derived by the compute tool: {'; '.join(computes)}" if computes
        else "Direct lookup, no arithmetic" if fetched or edges
        else "No computation"
    )

    corroboration = (
        "No filing cited" if not accns
        else "Single source — one filing" if len(accns) == 1
        else f"{len(accns)} filings"
    )

    limitations: list[str] = []
    if floors:
        names = ", ".join(sorted({f"{e.get('supplier')}→{e.get('customer')}" for e in floors}))
        limitations.append(
            f"{names}: the 10-K discloses only \">10%\", not an exact figure. Any number "
            f"derived from it is a floor, not an estimate — the true share may be far higher."
        )
    if edges:
        limitations.append(
            "Disclosure is supplier-side: it states what share of the SUPPLIER's revenue came "
            "from that customer. No filing discloses the customer's share of procurement."
        )
    if "retrieve_text" in used:
        limitations.append(
            "Passage retrieval is ranked, not exhaustive. Absence from the retrieved passages "
            "is not evidence of absence from the filing."
        )
    if not accns and not refused:
        limitations.append("No SEC accession is attached to this answer — the claim is uncited.")
    if computes and not fetched and not edges:
        limitations.append("Arithmetic ran on values that did not come from a data tool.")

    return {
        "numeric_source":      numeric_source,
        "relationship_source": relationship_source,
        "computation":         computation,
        "corroboration":       corroboration,
        "accessions":          accns,
        "tools_used":          sorted(t for t in used if t),
        "refused":             refused,
        "limitations":         limitations,
    }
