"""
Supply-chain edge extraction from 10-K text chunks.

Method: FinReflectKG single-pass schema-guided extraction (highest faithfulness).
Pipeline:
  1. Regex pre-filter  — narrow 969 chunks to ~30 candidates
  2. LLM single-pass   — gpt-4o-mini extracts structured edges per chunk
  3. Pydantic validate — schema compliance enforced at parse time
  4. DB insert         — deduplicated upsert into supply_edges
  5. Regression check  — verify known ground truth edges

Usage:
    python -m copilot.pipeline.extract_edges
    python -m copilot.pipeline.extract_edges --dry-run
    python -m copilot.pipeline.extract_edges --ticker QRVO
"""

import argparse
import re
import time
from typing import Literal

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, field_validator
from openai import OpenAI

from copilot.config import settings
from copilot.storage.db import get_conn
from copilot.storage.schema import create_tables

EDGAR_HEADERS = {"User-Agent": "financial-copilot research renxiangchao2678@gmail.com"}

# Note titles / table-header phrases that signal customer concentration disclosures.
# Two styles covered:
#   Note-title style (CRUS, QRVO): "Significant Customer", "Concentration of Credit Risk"
#   Table-header style (JBL, others): "Sales to the following customer that accounted for 10%..."
_CONCENTRATION_NOTE_TITLES = [
    "concentration of credit risk",
    "major customer",
    "significant customer",
    "customer concentration",
    "concentrations",
    # Table-header style — direct percentage tables without a preceding note title
    "sales to the following customer",
    "sets forth the respective portion of net revenue",
    "10% or more of our net revenue",
    "10% or more of the company",
    "10 percent or more of our net revenue",
]

# ── Item 8 HTML extraction ────────────────────────────────────────────────────

def _download_html(doc_url: str) -> str:
    resp = httpx.get(doc_url, headers=EDGAR_HEADERS, timeout=60, follow_redirects=True)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "html.parser", from_encoding="utf-8")
    # ix:nonfraction/ix:nonnumeric wrap the number itself in modern EDGAR
    # filings, so decompose() deleted the value along with the wrapper.
    # unwrap() drops only the wrapper. Same defect and same fix as
    # ingest_text.py::download_filing_html, which documents it at length.
    for tag in soup(["script", "style"]):
        tag.decompose()
    for tag in soup(["ix:nonfraction", "ix:nonnumeric"]):
        tag.unwrap()
    text = soup.get_text(separator="\n")
    text = text.encode("ascii", errors="ignore").decode("ascii")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return re.sub(r" {2,}", " ", text).strip()


_MAX_SECTION_CHARS = 4000  # cap per candidate block sent to LLM

_STOP_RE = re.compile(
    r"\b(goodwill|income tax|stock[\-\s]based compensation|debt|leases|"
    r"pension|derivative|fair value|equity|commitments|subsequent events)\b",
    re.IGNORECASE,
)


def _extract_item8_candidates(full_text: str) -> list[str]:
    """
    Return text blocks that contain customer concentration disclosures.

    Strategy: when a concentration note title or table-header phrase is found,
    accumulate ALL consecutive paragraphs as one block and pass it to the LLM —
    rather than relying on Item 8 boundaries (unreliable: Table of Contents
    entries match the same regex as actual section headers) or filtering each
    paragraph independently.

    Two bugs this fixes vs. the original per-paragraph filter:
    1. `len(para) > 40` dropped short HTML table data rows like
       "Apple, Inc.\\n11\\n%\\n17\\n%\\n19\\n%" that carry the customer name
       and percentage — the whole table was visible in full_text but the data
       row itself was silently discarded before ever reaching the LLM.
    2. Filtering candidates with `_is_candidate(para)` per-paragraph meant a
       table row containing only a name or only a number (neither matching
       the regex alone) was never included even while capturing was on.
    """
    # Include short paragraphs — HTML table rows can be < 40 chars
    all_paras = [p.strip() for p in full_text.split("\n\n") if p.strip()]

    candidates: list[str] = []
    buffer: list[str] = []
    capturing = False

    def _flush() -> None:
        if buffer:
            block = "\n\n".join(buffer)
            if _is_candidate(block):
                candidates.append(block)
            buffer.clear()

    for para in all_paras:
        lower = para.lower()

        # Concentration section trigger — flush previous section, start new
        if any(t in lower for t in _CONCENTRATION_NOTE_TITLES):
            _flush()
            buffer.append(para)
            capturing = True
            continue

        if capturing:
            # Stop at clearly unrelated short section headers
            if _STOP_RE.search(lower) and len(para) < 120:
                _flush()
                capturing = False
                continue

            buffer.append(para)  # include even short table rows
            if sum(len(p) for p in buffer) > _MAX_SECTION_CHARS:
                _flush()
                capturing = False
        else:
            # Outside a section: skip short/irrelevant fragments
            if len(para) < 40:
                continue

    _flush()
    return candidates


def run_extraction_from_html(
    ticker_filter: str | None = None,
    dry_run: bool = False,
) -> None:
    """Extract supply-chain edges from Item 8 Financial Notes in 10-K HTML filings."""
    client = OpenAI(api_key=settings.openai_api_key)
    conn   = get_conn()
    _create_edges_table(conn)

    # Load filings from DB (skip AAPL — it's the customer, not a supplier)
    # Change it when we expand dataset
    with conn.cursor() as cur:
        query = "SELECT ticker, accn, fiscal_year, doc_url FROM filings WHERE ticker != 'AAPL'"
        params: list = []
        if ticker_filter:
            query += " AND ticker = %s"
            params.append(ticker_filter.upper())
        query += " ORDER BY ticker, fiscal_year DESC"
        cur.execute(query, params)
        filings = cur.fetchall()

    print(f"Processing {len(filings)} filings for Item 8 extraction\n")
    total_edges = 0
    blocked: list[str] = []

    for filing in filings:
        ticker     = filing["ticker"]
        accn       = filing["accn"]
        fiscal_year = filing["fiscal_year"]
        doc_url    = filing["doc_url"]

        print(f"[{ticker}] FY{fiscal_year}  {accn}")
        try:
            html_text  = _download_html(doc_url)
            candidates = _extract_item8_candidates(html_text)
        except Exception as e:
            print(f"  ERROR downloading: {e}")
            time.sleep(1)
            continue

        if not candidates:
            print("  → no concentration note found in Item 8")
            time.sleep(0.5)
            continue

        print(f"  {len(candidates)} candidate paragraphs")
        for para in candidates:
            edges = _extract_from_chunk(client, para, ticker)
            for edge in edges:
                customer = _normalize_customer(edge.customer_ticker, edge.customer_name)
                print(f"  → {ticker}→{customer}  {edge.revenue_pct}%  FY{edge.fiscal_year}  [{edge.disclosure_status}]")
                if not dry_run:
                    written = _upsert_edge(conn, ticker, edge, accn, chunk_id=None,
                                           source_text=edge.evidence_sentence or None,
                                           filing_fiscal_year=fiscal_year)
                    conn.commit()
                    if written:
                        total_edges += 1
                    else:
                        blocked.append(f"{ticker}→{customer} FY{edge.fiscal_year}")
                else:
                    total_edges += 1

        time.sleep(0.5)  # be polite to EDGAR

    print(f"\nExtracted {total_edges} edges from Item 8.")
    if blocked:
        print(f"\n[GATE SUMMARY] {len(blocked)} candidate edge(s) blocked by the consistency "
              f"gate — extracted by the LLM but not written because source_text didn't ground "
              f"the number. Review manually; these are coverage gaps, not silent bad data:")
        for b in blocked:
            print(f"    {b}")
    if not dry_run:
        _run_regression(conn)
    conn.close()


# ── Known ground truth for regression validation ──────────────────────────────
# Source: WRDS Supply Chain / manual verification against 10-K filings
GOLDEN_EDGES = [
    {"supplier": "QRVO", "customer": "AAPL", "revenue_pct": 46.0, "fiscal_year": 2024},
    # SWKS: 10-K text says "more than ten percent" only — exact % (~59%) is in financial notes
    # (not ingested). Regression checks for threshold disclosure (revenue_pct=10.0).
    {"supplier": "SWKS", "customer": "AAPL", "revenue_pct": 10.0, "fiscal_year": 2024},
    # CRUS: ~85% disclosure is in financial notes (Note 14), not in ingested text_chunks.
    # This is a known data coverage gap — not an extraction failure.
]

# ── Customer name → ticker resolution ────────────────────────────────────────
CUSTOMER_ALIASES: dict[str, str] = {
    "apple": "AAPL",
    "apple inc": "AAPL",
    "apple inc.": "AAPL",
    "apple, inc.": "AAPL",
    "apple, inc": "AAPL",
    "skyworks": "SWKS",
    "skyworks solutions": "SWKS",
    "qorvo": "QRVO",
    "cirrus logic": "CRUS",
    "corning": "GLW",
    "broadcom": "AVGO",
    "samsung": "005930.KS",
    "samsung electronics": "005930.KS",
    "samsung electronics co., ltd.": "005930.KS",
    "samsung electronics co., ltd": "005930.KS",
    "005930": "005930.KS",
    # Huawei is privately held — no public ticker exists. Without an alias entry,
    # the LLM free-associates a different literal string per filing (including at
    # least one hallucinated ticker, "002502.SZ", which is an unrelated Shenzhen
    # listing), fragmenting one real-world relationship into multiple DB rows that
    # the (supplier, customer, fiscal_year) uniqueness constraint can't deduplicate.
    # Canonicalize to a name-based pseudo-ticker instead of inventing a fake one.
    "huawei": "HUAWEI",
    "huawei technology co., ltd.": "HUAWEI",
    "huawei technology co., ltd": "HUAWEI",
    "huawei technologies co., ltd.": "HUAWEI",
    "002502.sz": "HUAWEI",
}

# ── Regex pre-filter patterns ─────────────────────────────────────────────────
_CONCENTRATION_PATTERNS = [
    re.compile(r"\d+\s*%\s+of\s+(our\s+)?(net\s+)?revenue", re.IGNORECASE),
    re.compile(r"accounted\s+for\s+\d+", re.IGNORECASE),
    re.compile(r"constituted\s+\d+\s*%", re.IGNORECASE),
    re.compile(r"represented\s+\d+\s*%\s+of\s+(net\s+)?revenue", re.IGNORECASE),
    re.compile(r"(10|ten)\s*%\s+(or\s+more\s+of\s+)?(our\s+)?(net\s+)?revenue", re.IGNORECASE),
    # SWKS-style: "constituted more than ten percent of our net revenue"
    re.compile(r"constituted\s+more\s+than\s+ten\s+percent", re.IGNORECASE),
    re.compile(r"more\s+than\s+ten\s+percent\s+of\s+(our\s+)?(net\s+)?revenue", re.IGNORECASE),
    # CRUS-style: "represented approximately 87 percent of...sales"
    re.compile(r"\d+\s+percent\s+(of\s+)?(our\s+)?(net\s+)?(revenue|sales)", re.IGNORECASE),
    re.compile(r"approximately\s+\d+\s+percent", re.IGNORECASE),
]

def _is_candidate(text: str) -> bool:
    return any(p.search(text) for p in _CONCENTRATION_PATTERNS)



# ── Pydantic output schema ────────────────────────────────────────────────────

class EdgeCandidate(BaseModel):
    customer_name: str
    customer_ticker: str
    revenue_pct: float
    fiscal_year: int
    disclosure_status: Literal["named", "inferred", "unnamed"]
    threshold_only: bool = False
    evidence_sentence: str = ""

    @field_validator("customer_ticker")
    @classmethod
    def resolve_ticker(cls, v: str) -> str:
        resolved = CUSTOMER_ALIASES.get(v.lower().strip())
        return resolved or v.upper().strip()

    @field_validator("revenue_pct")
    @classmethod
    def pct_range(cls, v: float) -> float:
        if not (0 < v <= 100):
            raise ValueError(f"revenue_pct {v} out of range (0, 100]")
        return v


class ExtractionResult(BaseModel):
    edges: list[EdgeCandidate]


# ── LLM extraction ────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You extract supply-chain customer concentration disclosures from SEC 10-K filings.

A customer concentration disclosure states that a single customer accounts for a meaningful
percentage (typically ≥10%) of a company's revenue. These are required under ASC 280-10-50-42.

Rules:
- Extract ONLY explicit disclosures, do not infer or estimate.
- If text gives an exact percentage (e.g. "accounted for 10% of revenue", "accounted for 10% and 12%"),
  set revenue_pct to that number and threshold_only to false — even if the number happens to be exactly 10.
  IMPORTANT: "accounted for 10%" is an exact figure, not a threshold. threshold_only must be false.
- If text gives only a vague lower bound with no specific number
  (e.g. "more than ten percent", "at least 10%", "over 10%", "constituted more than ten percent"),
  set revenue_pct to 10.0 AND threshold_only to true.
- If no percentage or threshold is stated at all, do not extract that customer.
- Some filings state an AGGREGATE sentence ("our five largest customers accounted for
  approximately 36% of net revenue") immediately followed by a PER-CUSTOMER breakdown table
  (e.g. "Apple, Inc. 11% 17% 19%" across fiscal years). When both are present, extract the
  customer-specific number from the table, and set evidence_sentence to the table row for
  that customer (e.g. "Apple, Inc. 11 % 17 % 19 %") — never the aggregate sentence, even
  though the aggregate sentence reads as a more complete quote. The aggregate percentage
  describes the whole customer group, not this specific customer.
- CRITICAL — grounding requirement: evidence_sentence must be a literal substring (or near-
  verbatim quote) of the input text, and it must itself contain either the exact revenue_pct
  number you are reporting, or the threshold wording that justifies threshold_only=true. If
  you cannot find a sentence or table row that literally contains the number/wording you are
  about to report, DO NOT extract that customer — treat it the same as "no percentage stated
  at all." A number you cannot point to a literal quote for is worse than no edge: do not
  guess, do not infer from context, do not carry a number over from a nearby but unrelated
  clause.

For disclosure_status:
- "named": customer is explicitly named (e.g. "Apple Inc.")
- "inferred": customer is identifiable but not named (e.g. "our largest customer, a smartphone OEM")
- "unnamed": percentage is disclosed but customer cannot be identified

Return valid JSON:
{
  "edges": [
    {
      "customer_name": "<name as in text, or 'unnamed'>",
      "customer_ticker": "<ticker if known, else empty string>",
      "revenue_pct": <float>,
      "fiscal_year": <int>,
      "disclosure_status": "named" | "inferred" | "unnamed",
      "threshold_only": <true | false>,
      "evidence_sentence": "<copy the exact sentence(s) from the text that state this percentage for this customer>"
    }
  ]
}

If no edges found, return {"edges": []}."""


def _extract_from_chunk(
    client: OpenAI,
    chunk_text: str,
    supplier_ticker: str,
) -> list[EdgeCandidate]:
    prompt = (
        f"Supplier company ticker: {supplier_ticker}\n\n"
        f"10-K text chunk:\n{chunk_text}"
    )
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": prompt},
        ],
        temperature=0,
    )
    raw = response.choices[0].message.content or "{}"
    try:
        result = ExtractionResult.model_validate_json(raw)
        return result.edges
    except Exception as e:
        print(f"    [parse error] {e} | raw={raw[:200]}")
        return []


# ── DB helpers ────────────────────────────────────────────────────────────────

def _create_edges_table(conn) -> None:
    """Delegate to the one definition of this schema.

    There were two, differing in exactly the constraints: schema.py declares
    `supplier_ticker REFERENCES companies(ticker)` and `chunk_id REFERENCES
    text_chunks(id)`, this file declared neither, and CREATE TABLE IF NOT EXISTS
    means whichever ran first won. This one ran first, so the live table has no
    foreign keys -- and the chunk_id pointers rotted unnoticed when text_chunks
    was re-ingested and its surrogate ids changed. All 33 of them now point at
    rows in a range text_chunks no longer occupies.

    Nothing dereferences chunk_id at runtime, so no answer was ever wrong because
    of it. What was lost is the pointer from an edge to the chunk it came from,
    which is the audit chain this project is built on.
    """
    create_tables(conn)


def _normalize_customer(ticker: str, name: str) -> str:
    """Resolve to canonical ticker — tries ticker field first, then customer_name."""
    for candidate in (ticker, name):
        resolved = CUSTOMER_ALIASES.get(candidate.lower().strip())
        if resolved:
            return resolved
    return (ticker or name).strip()


# ── Source-text consistency gate ──────────────────────────────────────────────
#
# Prevents the JBL FY2020-2024 failure mode from ever reaching the DB again:
# revenue_pct was correct, but source_text quoted an unrelated aggregate
# sentence ("our five largest customers accounted for 36%...") instead of the
# per-customer table row that actually grounds the number. A number with an
# unverifiable citation is exactly as untrustworthy as a wrong number for a
# system whose entire value proposition is "every number traces to a quote in
# the filing" — so this gate treats the two failures identically: block the write.

_THRESHOLD_PHRASES = re.compile(
    r"more\s+than\s+(ten\s+percent|10\s*%)|at\s+least\s+(10\s*%|ten\s+percent)|"
    r"over\s+10\s*%|in\s+excess\s+of\s+10\s*%|"
    r"(10\s*%|ten\s+percent)\s+or\s+more",
    re.IGNORECASE,
)

# Reverse check: catches the SWKS→Huawei FY2017 pattern — text states an EXACT
# figure ("accounted for 10%") but was classified threshold_only=True anyway.
# A verb of disclosure directly followed by a bare number+% (no "more than" /
# "at least" / "or more" qualifier immediately before OR after) is exact
# language, not a floor. Checked both sides: "accounted for more than 10%"
# (qualifier before) and "accounted for 10% or more" (qualifier after) are
# both legitimate threshold phrasing and must NOT match this pattern.
_EXACT_DISCLOSURE_RE = re.compile(
    r"\b(accounted for|constituted|represented)\s+"
    r"(?!more than|at least|over|in excess of)"
    r"\d+(?:\.\d+)?\s*(?:%|percent)"
    r"(?!\s*(or more|or greater))",
    re.IGNORECASE,
)


def verify_source_text_consistency(
    revenue_pct: float, threshold_only: bool, source_text: str | None,
) -> tuple[bool, str]:
    """
    Gate: source_text must actually ground revenue_pct (or the threshold wording).

    threshold_only=True  → source_text must contain threshold-indicating language,
                            AND must not also contain exact-disclosure phrasing
                            (that would mean this should have been threshold_only=False
                            with the real number, not floored to 10%).
    threshold_only=False → the revenue_pct number itself (± 0.6, covers rounding)
                            must appear somewhere among the numbers in source_text.

    This is a lightweight textual check, not semantic grounding — a source_text
    that happens to contain an unrelated number within tolerance would pass.
    That's an accepted limitation of a regex-based gate; it still catches the
    JBL-style failure (wrong sentence entirely, no matching number in it at all).
    """
    if not source_text:
        return False, "source_text is empty"

    if threshold_only:
        if not _THRESHOLD_PHRASES.search(source_text):
            return False, "threshold_only=True but source_text has no threshold wording"
        if _EXACT_DISCLOSURE_RE.search(source_text):
            return False, ("threshold_only=True but source_text also contains exact-disclosure "
                           "phrasing (e.g. 'accounted for X%') — likely misclassified, should be "
                           "threshold_only=False with the real figure")
        return True, ""

    numbers_in_text = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", source_text)]
    if any(abs(n - revenue_pct) < 0.6 for n in numbers_in_text):
        return True, ""
    return False, f"revenue_pct={revenue_pct} not found among numbers in source_text {numbers_in_text}"


def _upsert_edge(conn, supplier_ticker: str, edge: EdgeCandidate,
                 accn: str, chunk_id: int | None,
                 source_text: str | None = None,
                 filing_fiscal_year: int | None = None) -> bool:
    """
    filing_fiscal_year: the fiscal year of the filing being processed.
    When edge.fiscal_year == filing_fiscal_year, this is the primary filing for that
    relationship — accn, source_text, revenue_pct, disclosure_status, and threshold_only
    are all authoritative and always written.
    When a later filing references prior-year data (edge.fiscal_year < filing_fiscal_year),
    none of those five columns are overwritten unless no value exists yet (NULL guard) —
    a secondary filing's incidental mention of an older year must never clobber the
    original primary filing's numbers.

    Quality gate: for the primary filing, source_text must pass
    verify_source_text_consistency() or the write is blocked entirely (returns
    False, nothing touches the DB). Secondary (comparison-year) writes are not
    gated — their source_text is only used as a NULL-guard fallback, and gating
    them risks blocking a perfectly fine primary write that happened first.
    """
    customer = _normalize_customer(edge.customer_ticker, edge.customer_name)
    is_primary = (filing_fiscal_year is None or edge.fiscal_year == filing_fiscal_year)

    if is_primary:
        ok, reason = verify_source_text_consistency(edge.revenue_pct, edge.threshold_only, source_text)
        if not ok:
            print(f"    [GATE BLOCKED] {supplier_ticker}→{customer} FY{edge.fiscal_year}: {reason}")
            return False

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO supply_edges
                (supplier_ticker, customer_ticker, revenue_pct, fiscal_year,
                 disclosure_status, threshold_only, accn, chunk_id, source_text)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (supplier_ticker, customer_ticker, fiscal_year)
            DO UPDATE SET
                revenue_pct       = CASE WHEN %s OR supply_edges.revenue_pct IS NULL
                                   THEN EXCLUDED.revenue_pct ELSE supply_edges.revenue_pct END,
                disclosure_status = CASE WHEN %s OR supply_edges.disclosure_status IS NULL
                                   THEN EXCLUDED.disclosure_status ELSE supply_edges.disclosure_status END,
                threshold_only    = CASE WHEN %s OR supply_edges.threshold_only IS NULL
                                   THEN EXCLUDED.threshold_only ELSE supply_edges.threshold_only END,
                accn        = CASE WHEN %s OR supply_edges.accn IS NULL
                                   THEN EXCLUDED.accn ELSE supply_edges.accn END,
                chunk_id    = CASE WHEN %s OR supply_edges.chunk_id IS NULL
                                   THEN EXCLUDED.chunk_id ELSE supply_edges.chunk_id END,
                source_text = CASE WHEN %s OR supply_edges.source_text IS NULL
                                   THEN EXCLUDED.source_text ELSE supply_edges.source_text END,
                extracted_at = NOW()
            RETURNING id
        """, (
            supplier_ticker, customer, edge.revenue_pct, edge.fiscal_year,
            edge.disclosure_status, edge.threshold_only, accn, chunk_id, source_text,
            is_primary, is_primary, is_primary, is_primary, is_primary, is_primary,
        ))
        return cur.fetchone() is not None
    conn.commit()


# ── Regression validation ─────────────────────────────────────────────────────

def _run_regression(conn, tol: float = 5.0) -> None:
    print("\n── Regression check (known ground truth) ──")
    with conn.cursor() as cur:
        for g in GOLDEN_EDGES:
            cur.execute("""
                SELECT revenue_pct FROM supply_edges
                WHERE supplier_ticker = %s
                  AND customer_ticker = %s
                  AND fiscal_year     = %s
                ORDER BY ABS(revenue_pct - %s) ASC
                LIMIT 1
            """, (g["supplier"], g["customer"], g["fiscal_year"], g["revenue_pct"]))
            row = cur.fetchone()
            if row:
                diff = abs(row["revenue_pct"] - g["revenue_pct"])
                status = "PASS" if diff <= tol else "WARN"
                print(f"  {status} {g['supplier']}→{g['customer']} FY{g['fiscal_year']}: "
                      f"expected {g['revenue_pct']}%  got {row['revenue_pct']}%")
            else:
                print(f"  MISS {g['supplier']}→{g['customer']} FY{g['fiscal_year']}: "
                      f"edge not found in DB")


# ── Full-table audit ──────────────────────────────────────────────────────────

def audit_existing_edges(conn, include_unnamed: bool = False) -> dict:
    """
    Run verify_source_text_consistency() over every row already in supply_edges.
    Standalone gate scan of existing data — the pipeline-integrated gate in
    _upsert_edge only protects NEW writes; this is what catches rows written
    before the gate existed (e.g. the original JBL FY2020-2024 rows).

    include_unnamed=False (default) skips disclosure_status != 'named' rows.
    Every downstream consumer — graph_query in tools.py, every dashboard.py
    query — already hard-filters WHERE disclosure_status='named', so an
    unnamed row can never reach a user-facing view regardless of what's in
    revenue_pct/source_text. Flagging them as "suspicious" here is pure audit
    noise unless you're specifically reviewing unnamed/segment-level rows
    (e.g. GLW's 4 segment-scoped disclosures, where the customer is never
    named and the percentage is scoped to one business segment rather than
    total revenue — audited and left in place deliberately, since named
    downstream consumers never see them). Pass include_unnamed=True to see
    them.

    Returns {"clean": [...], "suspicious": [...]} — each entry is a dict with
    id/supplier/customer/fiscal_year/revenue_pct/reason (reason empty if clean).
    """
    with conn.cursor() as cur:
        query = """
            SELECT id, supplier_ticker, customer_ticker, fiscal_year,
                   revenue_pct, threshold_only, source_text
            FROM supply_edges
        """
        if not include_unnamed:
            query += " WHERE disclosure_status = 'named'"
        query += " ORDER BY supplier_ticker, fiscal_year DESC"
        cur.execute(query)
        rows = cur.fetchall()

    clean, suspicious = [], []
    for r in rows:
        ok, reason = verify_source_text_consistency(r["revenue_pct"], r["threshold_only"], r["source_text"])
        entry = {
            "id": r["id"], "supplier": r["supplier_ticker"], "customer": r["customer_ticker"],
            "fiscal_year": r["fiscal_year"], "revenue_pct": r["revenue_pct"], "reason": reason,
        }
        (clean if ok else suspicious).append(entry)

    scope = "all rows" if include_unnamed else "named rows only (use --include-unnamed to see all)"
    print(f"\n── Full-table audit: {len(rows)} edges [{scope}] ──")
    print(f"  clean:      {len(clean)}")
    print(f"  suspicious: {len(suspicious)}")
    for e in suspicious:
        print(f"    [{e['id']}] {e['supplier']}→{e['customer']} FY{e['fiscal_year']} "
              f"({e['revenue_pct']}%): {e['reason']}")

    return {"clean": clean, "suspicious": suspicious}


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run_extraction(
    ticker_filter: str | None = None,
    dry_run: bool = False,
) -> None:
    client = OpenAI(api_key=settings.openai_api_key)
    conn   = get_conn()
    _create_edges_table(conn)

    # 1. Load candidate chunks (join filing fiscal_year for primary-filing detection)
    with conn.cursor() as cur:
        query = """
            SELECT tc.id, tc.ticker, tc.section, tc.text, tc.accn,
                   fi.fiscal_year as filing_fiscal_year
            FROM text_chunks tc
            JOIN filings fi ON fi.accn = tc.accn
            WHERE tc.ticker != 'AAPL'
        """
        params: list = []
        if ticker_filter:
            query += " AND tc.ticker = %s"
            params.append(ticker_filter.upper())
        query += " ORDER BY tc.ticker, tc.id"
        cur.execute(query, params)
        all_chunks = cur.fetchall()

    candidates = [c for c in all_chunks if _is_candidate(c["text"])]
    print(f"Pre-filter: {len(all_chunks)} chunks → {len(candidates)} candidates")

    # 2. Extract edges per candidate chunk
    total_edges = 0
    blocked: list[str] = []
    for chunk in candidates:
        chunk_id           = chunk["id"]
        ticker             = chunk["ticker"]
        section            = chunk["section"]
        accn               = chunk["accn"]
        text               = chunk["text"]
        filing_fiscal_year = chunk["filing_fiscal_year"]

        print(f"\n[{ticker}] chunk {chunk_id} ({section})")
        edges = _extract_from_chunk(client, text, ticker)

        if not edges:
            print("  → no edges found")
            continue

        for edge in edges:
            customer = edge.customer_ticker or edge.customer_name
            print(f"  → {ticker}→{customer}  {edge.revenue_pct}%  "
                  f"FY{edge.fiscal_year}  [{edge.disclosure_status}]")
            if not dry_run:
                written = _upsert_edge(conn, ticker, edge, accn, chunk_id,
                                       source_text=edge.evidence_sentence or None,
                                       filing_fiscal_year=filing_fiscal_year)
                conn.commit()
                if written:
                    total_edges += 1
                else:
                    blocked.append(f"{ticker}→{customer} FY{edge.fiscal_year}")
            else:
                total_edges += 1

    print(f"\nExtracted {total_edges} edges total.")
    if blocked:
        print(f"\n[GATE SUMMARY] {len(blocked)} candidate edge(s) blocked by the consistency "
              f"gate — extracted by the LLM but not written because source_text didn't ground "
              f"the number. Review manually; these are coverage gaps, not silent bad data:")
        for b in blocked:
            print(f"    {b}")

    # 3. Regression check
    if not dry_run:
        _run_regression(conn)

    conn.close()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker",  default=None, help="Extract only for this ticker")
    parser.add_argument("--dry-run", action="store_true", help="Print without writing to DB")
    parser.add_argument("--source",  default="chunks", choices=["chunks", "html", "all"],
                        help="chunks=text_chunks table (default), html=Item 8 HTML, all=both")
    parser.add_argument("--audit", action="store_true",
                        help="Run the source_text/revenue_pct consistency gate over all "
                             "existing rows (no extraction, no DB writes) and exit")
    parser.add_argument("--include-unnamed", action="store_true",
                        help="With --audit: also check disclosure_status != 'named' rows "
                             "(unnamed/segment-level disclosures that no query path ever "
                             "surfaces to users — off by default to avoid audit noise)")
    args = parser.parse_args()

    if args.audit:
        conn = get_conn()
        audit_existing_edges(conn, include_unnamed=args.include_unnamed)
        conn.close()
        return

    if args.source in ("chunks", "all"):
        run_extraction(ticker_filter=args.ticker, dry_run=args.dry_run)
    if args.source in ("html", "all"):
        run_extraction_from_html(ticker_filter=args.ticker, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
