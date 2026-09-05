"""Assert that what the code writes down still matches what the database holds.

This project's most persistent defect is a written-down list drifting from the
data it describes. Seven instances so far: a supplier dropdown built from a
colour table, RRF using a text prefix as identity, a model-tiering table no
category fell through, agent.MODEL duplicating DEFAULT_MODEL, the accession
pattern copied into four modules, two cluster definitions disagreeing on a
company's name, and a tool schema advertising ten metrics while the database
held twenty-four.

The last one was not cosmetic. Asked for Corning's total equity INCLUDING
non-controlling interests, the model could not see that TotalEquityInclNCI
exists, queried TotalEquity, and answered 10,686,000,000 with the qualifier
attached -- the real figure is 11,070,000,000. Every answer check was blind to
it: the value was fetched, no formula ran, the company and year were right.

Each drift was found by hand, late, after it had done something. These
assertions are the same checks run automatically. They are unusual for a unit
test in that they compare code against DATA rather than against expected
behaviour -- which is exactly the axis this project keeps failing on, and the
axis a reproducible pipeline depends on: re-running ingestion has to reproduce
the database the eval sets were verified against.

Every assertion here failed at least once before it was written.
"""

import pytest

from copilot.storage.db import get_conn


@pytest.fixture(scope="module")
def db_labels() -> frozenset[str]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT label FROM financial_facts WHERE form = '10-K'")
            return frozenset(r["label"] for r in cur.fetchall())
    finally:
        conn.close()


@pytest.fixture(scope="module")
def db_tickers() -> frozenset[str]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT ticker FROM companies")
            return frozenset(r["ticker"].upper() for r in cur.fetchall())
    finally:
        conn.close()


# ── What the model is told exists ────────────────────────────────────────────

def test_the_tool_schema_advertises_exactly_what_the_database_holds(db_labels):
    """The seventh drift, and the only one that produced a wrong number.

    Not "is a subset": advertising fewer than exist makes the model substitute a
    similar-sounding label, and advertising more makes it query things that come
    back empty. Both have happened here.
    """
    from copilot.agent.agent import TOOL_SCHEMAS, advertised_metrics

    assert set(advertised_metrics()) == set(db_labels)

    schema = next(t for t in TOOL_SCHEMAS if t["function"]["name"] == "query_financials")
    described = schema["function"]["parameters"]["properties"]["metric"]["description"]
    for label in db_labels:
        assert label in described, f"{label} is queryable and not advertised"


# ── What the checkers assume exists ──────────────────────────────────────────

def test_every_registry_label_is_a_real_label(db_labels):
    """authority.REGISTRY vouches for combinations of stored labels. A renamed
    label would silently stop matching and report correct formulas as unsourced
    -- noisy rather than dangerous, but invisible without this."""
    from copilot.agent.authority import REGISTRY

    for labels, name in REGISTRY:
        for label in labels:
            assert label in db_labels, f"{name!r} needs {label!r}, which no longer exists"


def test_every_clarification_option_names_a_real_label(db_labels):
    """clarify._METRIC_FAMILIES offers the reader a menu of figures. An option
    naming a label the database no longer holds would send them to a question
    that cannot be answered -- a worse outcome than the ambiguity it replaced,
    because they chose it."""
    from copilot.agent.clarify import _METRIC_FAMILIES

    for head, options in _METRIC_FAMILIES.items():
        for label, _phrase, stored in options:
            assert stored in db_labels, f"{head!r} offers {label!r} -> missing {stored!r}"


def test_every_clarification_rewrite_resolves_to_a_metric():
    """Each option's phrase has to be one find_metric recognises, or the rewrite
    comes back ambiguous and the reader is asked the same question twice."""
    from copilot.agent.clarify import _METRIC_FAMILIES
    from copilot.agent.slots import find_metric

    for head, options in _METRIC_FAMILIES.items():
        for label, phrase, stored in options:
            assert find_metric(f"Apple's {phrase} in 2024") == stored, (
                f"{label!r} rewrites to a phrase that does not resolve"
            )


def test_every_metric_term_maps_to_a_real_label(db_labels):
    from copilot.agent.slots import _METRIC_TERMS

    for pattern, label in _METRIC_TERMS:
        assert label in db_labels, f"pattern {pattern!r} maps to missing label {label!r}"


def test_every_hardcoded_ticker_is_a_real_company(db_tickers):
    from copilot.agent.slots import _EXTRA_ALIASES, _WORDLIKE_TICKERS

    for alias, ticker in _EXTRA_ALIASES.items():
        assert ticker in db_tickers, f"alias {alias!r} points at missing {ticker!r}"
    for ticker in _WORDLIKE_TICKERS:
        assert ticker in db_tickers, f"{ticker!r} is excluded from matching but does not exist"


# ── What the pipelines would reproduce ───────────────────────────────────────

def test_the_cluster_is_defined_once():
    """Two ingestion pipelines once held drifting copies of the cluster dict
    (they disagreed on whether SWKS is "Skyworks Solutions" or "Skyworks
    Solutions Inc.", and the string becomes companies.name, which slots
    derives its company index from). The pipelines were merged into
    ingest_financial_facts (2026-08-26); this guards against a local copy
    ever replacing the import from the canonical source."""
    from copilot.pipeline import companies, ingest_financial_facts

    assert ingest_financial_facts.CLUSTER_RESEARCH is companies.CLUSTER_RESEARCH


def test_the_stored_tag_mapping_matches_the_code(db_labels):
    """What ingestion would write must be what ingestion did write. Checks the
    direction that matters: every label the code can produce exists, so a
    re-run adds nothing the eval sets were not verified against.

    (A second guard — that two pipelines share one TAG_LABELS object — retired
    with the merge: TAG_LABELS now has exactly one definition site.)"""
    from copilot.pipeline import ingest_financial_facts

    producible = set(ingest_financial_facts.TAG_LABELS.values())
    unexpected = producible - set(db_labels)
    assert not unexpected, f"re-running ingestion would introduce {sorted(unexpected)}"


# ── What two modules must agree about ────────────────────────────────────────

def test_provenance_and_grounding_read_the_same_accessions():
    """Four copies of this pattern existed; three were unified and the fourth
    was missed. provenance matched only the dashed form, so an answer citing a
    filing by link counted as cited in one record and uncited in the other --
    and both are returned from the same call."""
    from copilot.agent.grounding import accessions_in
    from copilot.agent.provenance import _accessions

    url = ("https://www.sec.gov/Archives/edgar/data/320193/"
           "000032019324000123/aapl-20240928.htm")
    steps = [{"tool": "query_financials", "output": {"found": True,
                                                     "citation": f"SEC 10-K ({url})"}}]
    assert accessions_in(url) == {"000032019324000123"}
    assert _accessions(steps) == ["0000320193-24-000123"]   # normalised, then made readable


def test_refusal_detection_has_one_home():
    """Two lists with deliberately different semantics, which is fine, and no
    test pinning the difference, which was not: the next person to "unify" them
    would have changed a scoring rule without noticing."""
    from copilot.agent import grounding, provenance
    from copilot.eval.harness import _is_refusal

    assert provenance._REFUSAL_MARKERS is grounding.REFUSAL_STRICT
    assert _is_refusal("I cannot determine this.")
    # Strict is a real subset: third-person absence is a refusal for scoring and
    # not for provenance.
    assert grounding.looks_like_refusal("No data found for that ticker.")
    assert not grounding.looks_like_refusal("No data found for that ticker.", strict=True)


# ── A boundary rather than a bug, pinned so it stays one ─────────────────────

def test_the_fiscal_year_label_matches_the_period_for_every_answerable_year():
    """Every (ticker, label, fiscal_year) holds several rows -- a filing reports
    comparative periods -- and query_financials takes period_end DESC LIMIT 1.
    Nothing defined which row is "the year's figure", so this measures it.

    136 of 4,351 combinations pick a period ending in a different calendar year,
    all of them MCHP FY2009-2013 and CRUS FY2011-2013, where fiscal_year appears
    to record the year the period STARTED. Zero occur in FY2016 or later, which
    is where every eval question lives. The assertion is on that boundary: if a
    future ingestion pushes the problem into the answerable range, this fails.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                WITH picked AS (
                    SELECT DISTINCT ON (ticker, label, fiscal_year)
                           ticker, label, fiscal_year, period_end
                    FROM financial_facts WHERE form = '10-K'
                    ORDER BY ticker, label, fiscal_year, period_end DESC)
                SELECT ticker, label, fiscal_year, period_end FROM picked
                WHERE fiscal_year >= 2016
                  AND EXTRACT(YEAR FROM period_end)::int <> fiscal_year
                LIMIT 5""")
            offenders = cur.fetchall()
    finally:
        conn.close()
    assert not offenders, f"fiscal_year no longer matches its period: {offenders}"
