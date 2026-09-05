"""
Download and chunk 10-K body text from EDGAR.

Pipeline:
  EDGAR submissions API → find recent 10-K filings
  → download HTML filing → extract text sections
  → chunk into ~500-token pieces → store in text_chunks table

Usage:
    python -m copilot.pipeline.ingest_text --ticker AAPL --years 3
    python -m copilot.pipeline.ingest_text                     # v1 cluster, last 3 years
    python -m copilot.pipeline.ingest_text --cluster research --years 10
"""

import argparse
import re
import sys
import time

# Force UTF-8 output on Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx
import tiktoken
from bs4 import BeautifulSoup

from copilot.pipeline.companies import CIK_OVERRIDES, CLUSTER_RESEARCH, CLUSTER_V1
from copilot.storage.db import get_conn
from copilot.storage.schema import create_tables

EDGAR_HEADERS = {"User-Agent": "financial-copilot research renxiangchao2678@gmail.com"}
CHUNK_TOKENS = 500
CHUNK_OVERLAP = 50

# Sections we care about in 10-K filings
TARGET_SECTIONS = {
    "business":       "Business",
    "risk factors":   "Risk Factors",
    "management":     "MD&A",
    "mda":            "MD&A",
}

enc = tiktoken.get_encoding("cl100k_base")


def get_cik(ticker: str) -> str:
    if ticker.upper() in CIK_OVERRIDES:
        return CIK_OVERRIDES[ticker.upper()]
    resp = httpx.get(
        "https://www.sec.gov/files/company_tickers.json",
        headers=EDGAR_HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    for entry in resp.json().values():
        if entry["ticker"].upper() == ticker.upper():
            return str(entry["cik_str"]).zfill(10)
    raise ValueError(f"Ticker {ticker} not found")


def _parse_filing_batch(cik: str, batch: dict) -> list[dict]:
    """Extract 10-K entries from a filings batch dict (recent or archived page).

    fiscal_year is derived from EDGAR's own `reportDate` (the period-of-report
    end date), NOT from `filingDate`. Found 2026-08-07: the previous
    `int(filed[:4])` is wrong for any December/calendar-fiscal-year-end filer,
    since the 10-K is filed 5-8 weeks into the FOLLOWING calendar year (e.g.
    TXN's FY2025 10-K: filingDate=2026-02-06, reportDate=2025-12-31 --
    filed[:4] gives 2026, off by one). Confirmed via cross-check against
    financial_facts' XBRL-derived fiscal_year: 4/15 companies in this
    project's cluster (APH, GLW, ON, TXN -- all Dec/calendar FYE) had 100% of
    their `filings` rows mislabeled by exactly one year under the old logic.
    `reportDate` is present on every 10-K entry in both the "recent" batch and
    the archived pages (verified for both before this fix).
    """
    results = []
    for i, form in enumerate(batch["form"]):
        if form != "10-K":
            continue
        accn = batch["accessionNumber"][i]
        filed = batch["filingDate"][i]
        report_date = batch["reportDate"][i]
        doc = batch["primaryDocument"][i]
        accn_no_dash = accn.replace("-", "")
        doc_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn_no_dash}/{doc}"
        results.append({
            "accn": accn,
            "filed_date": filed,
            "fiscal_year": int(report_date[:4]) if report_date else int(filed[:4]),
            "doc_url": doc_url,
        })
    return results


def get_recent_10k_filings(cik: str, years: int = 3) -> list[dict]:
    """Return metadata for the most recent N 10-K filings.

    Handles EDGAR pagination: companies with long histories store older filings
    in separate archived submission pages (submissions-001.json, etc.).
    """
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    resp = httpx.get(url, headers=EDGAR_HEADERS, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    all_results = _parse_filing_batch(cik, data["filings"]["recent"])

    # Fetch archived pages if we still need more
    for page in data["filings"].get("files", []):
        if len(all_results) >= years:
            break
        arch_url = f"https://data.sec.gov/submissions/{page['name']}"
        arch_resp = httpx.get(arch_url, headers=EDGAR_HEADERS, timeout=20)
        arch_resp.raise_for_status()
        arch_data = arch_resp.json()
        all_results.extend(_parse_filing_batch(cik, arch_data))
        time.sleep(0.3)

    # Return the N most recent, sorted newest-first
    all_results.sort(key=lambda x: x["filed_date"], reverse=True)
    results = all_results[:years]

    return results


def download_filing_text(doc_url: str) -> str:
    """Download 10-K HTML and return cleaned plain text."""
    resp = httpx.get(doc_url, headers=EDGAR_HEADERS, timeout=60, follow_redirects=True)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "html.parser", from_encoding="utf-8")

    # Remove script/style (no visible content, safe to fully delete). Inline
    # XBRL wrapper tags (ix:nonfraction/ix:nonnumeric) are NOT pure metadata --
    # in modern EDGAR filings the number/text IS the tag's content (e.g.
    # <ix:nonfraction>1,959</ix:nonfraction>), so decompose() was deleting the
    # number along with the wrapper. unwrap() removes only the wrapper and
    # keeps the inner text in place. Found 2026-07-22: this silently deleted
    # every dollar figure in Item 8 financial-statement tables/prose across
    # the whole corpus (confirmed on TXN's R&D/revenue line, among others).
    for tag in soup(["script", "style"]):
        tag.decompose()
    for tag in soup(["ix:nonfraction", "ix:nonnumeric"]):
        tag.unwrap()

    text = soup.get_text(separator="\n")
    # Normalize unicode, collapse whitespace
    text = text.encode("ascii", errors="ignore").decode("ascii")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


def detect_section(heading: str) -> str | None:
    """Map a heading string to a known section label."""
    h = heading.lower().strip()
    for key, label in TARGET_SECTIONS.items():
        if key in h:
            return label
    return None


def split_into_sections(text: str) -> list[tuple[str, str]]:
    """
    Split filing text into (section_label, section_text) pairs.
    Uses Item headings as section boundaries.

    Handles two heading formats:
    - "Item 1A. Title text on same line" (most filers)
    - "Item1.\nTitle on next line" (Lam Research iXBRL format, no space before digit)
    """
    # Matches inline: "Item 1A. Title..." or "Item1A: Title..." (colon separator)
    # Also matches next-line: "Item1.\nTitle..."
    pattern = re.compile(
        r"(Item\s*\d+[A-Z]?[.:]?\s+[^\n]{3,60}"    # title on same line
        r"|Item\s*\d+[A-Z]?[.:]?\s*\n[^\n]{3,60})", # title on next line
        re.IGNORECASE,
    )
    parts = pattern.split(text)

    sections = []
    current_label = "General"
    current_text = []

    for part in parts:
        if pattern.match(part):
            # Save previous section
            if current_text:
                sections.append((current_label, "\n".join(current_text)))
            label = detect_section(part) or "General"
            current_label = label
            current_text = [part]
        else:
            current_text.append(part)

    if current_text:
        sections.append((current_label, "\n".join(current_text)))

    # Only keep target sections (drop General noise)
    return [(lbl, txt) for lbl, txt in sections if lbl != "General"]


def chunk_text(text: str, chunk_tokens: int = CHUNK_TOKENS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping token-based chunks."""
    tokens = enc.encode(text)
    chunks = []
    start = 0
    while start < len(tokens):
        end = min(start + chunk_tokens, len(tokens))
        chunk_tokens_slice = tokens[start:end]
        chunks.append(enc.decode(chunk_tokens_slice))
        if end == len(tokens):
            break
        start += chunk_tokens - overlap
    return chunks


def upsert_filing(conn, ticker: str, filing: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO filings (accn, ticker, form, filed_date, fiscal_year, doc_url)
            VALUES (%s, %s, '10-K', %s, %s, %s)
            ON CONFLICT (accn) DO NOTHING
            """,
            (filing["accn"], ticker, filing["filed_date"], filing["fiscal_year"], filing["doc_url"]),
        )
    conn.commit()


def upsert_chunks(conn, ticker: str, accn: str, sections: list[tuple[str, str]]) -> int:
    total = 0
    chunk_index = 0
    with conn.cursor() as cur:
        for section_label, section_text in sections:
            for chunk in chunk_text(section_text):
                token_count = len(enc.encode(chunk))
                cur.execute(
                    """
                    INSERT INTO text_chunks (accn, ticker, section, chunk_index, text, token_count)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (accn, chunk_index) DO NOTHING
                    """,
                    (accn, ticker, section_label, chunk_index, chunk, token_count),
                )
                chunk_index += 1
                total += 1
    conn.commit()
    return total


def ingest_ticker_text(ticker: str, conn, years: int = 3) -> None:
    print(f"[{ticker}] resolving CIK...")
    cik = get_cik(ticker)

    print(f"[{ticker}] fetching filing list...")
    filings = get_recent_10k_filings(cik, years)
    print(f"[{ticker}] found {len(filings)} 10-K filings")

    for filing in filings:
        accn = filing["accn"]
        print(f"[{ticker}] downloading {accn} ({filing['filed_date']})...")
        try:
            upsert_filing(conn, ticker, filing)
            text = download_filing_text(filing["doc_url"])
            sections = split_into_sections(text)
            n = upsert_chunks(conn, ticker, accn, sections)
            print(f"[{ticker}] {accn} → {len(sections)} sections, {n} chunks")
        except Exception as e:
            print(f"[{ticker}] ERROR on {accn}: {e}")
        time.sleep(0.5)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", default=None)
    parser.add_argument("--years", type=int, default=3)
    parser.add_argument(
        "--cluster",
        default="v1",
        choices=["v1", "research"],
        help="v1=original 6 companies, research=15-company SC-DisclosureQA cluster",
    )
    args = parser.parse_args()

    cluster = CLUSTER_RESEARCH if args.cluster == "research" else CLUSTER_V1
    tickers = [args.ticker.upper()] if args.ticker else list(cluster.keys())

    conn = get_conn()
    create_tables(conn)
    print(f"Ingesting {len(tickers)} companies (cluster={args.cluster}, years={args.years}).")

    for i, ticker in enumerate(tickers):
        if i > 0:
            time.sleep(1)
        try:
            ingest_ticker_text(ticker, conn, years=args.years)
        except Exception as e:
            print(f"[{ticker}] FATAL: {e}")

    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
