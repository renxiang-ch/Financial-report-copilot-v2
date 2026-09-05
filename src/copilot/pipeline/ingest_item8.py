"""
Ingest Item 8 (Financial Statements & Supplementary Data) from 10-K HTML.

For each filing already in the filings table:
  1. Download the 10-K HTML
  2. Locate Item 8 section via text/HTML boundary detection
  3. Extract top-level tables → Markdown text (chunk_type='table')
  4. Extract prose (tables removed) → plain text  (chunk_type='text')
  5. Chunk and store in text_chunks, continuing chunk_index from max existing

Requires schema migration (run once):
  ALTER TABLE text_chunks ADD COLUMN IF NOT EXISTS chunk_type TEXT DEFAULT 'text';

Usage:
    python -m copilot.pipeline.ingest_item8
    python -m copilot.pipeline.ingest_item8 --ticker AAPL
    python -m copilot.pipeline.ingest_item8 --cluster research --years 10
"""

import argparse
import re
import sys
import time

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx
import tiktoken
from bs4 import BeautifulSoup, Tag

from copilot.storage.db import get_conn
from copilot.storage.schema import migrate_add_chunk_type

EDGAR_HEADERS = {"User-Agent": "financial-copilot research renxiangchao2678@gmail.com"}
CHUNK_TOKENS = 500
CHUNK_OVERLAP = 50
TABLE_MAX_TOKENS = 1500  # tables get their own larger budget

enc = tiktoken.get_encoding("cl100k_base")

# Non-breaking space variants in HTML: &nbsp; &#160; \xa0 (literal after decode)
_NBSP = r"(?:&nbsp;|&#160;|\xa0|\s)"

# Item 8 heading — matches both "Item 8." and "ITEM&#160;8." forms
_ITEM8_RE = re.compile(
    rf"ITEM{_NBSP}+8{_NBSP}*[.\-]",
    re.IGNORECASE,
)
# Item 9 end boundary (9, 9A, 9B …)
_ITEM9_RE = re.compile(
    rf"ITEM{_NBSP}+9[A-Z]?{_NBSP}*[.\-]",
    re.IGNORECASE,
)

# Some filers (found 2026-07-22: GLW, JBL, ON, and MCHP in some fiscal years)
# write Item 8 as a one-paragraph cross-reference -- "the response to this
# Item 8 is included in ... Part IV, Item 15" -- rather than putting the
# financial statements directly between the Item 8 and Item 9 headings. The
# real statements end up physically located elsewhere: sometimes between
# Item 15 and Item 16, sometimes in unlabeled "Part IV" content after Item
# 16. Rather than chase each filer's specific redirect target, search
# forward from Item 8's own position for the first REAL occurrence of a
# primary statement's own title (confirmed by a table with digits nearby,
# distinguishing it from a table-of-contents/index mention of the same
# phrase) and use that as the anchor instead.
_STATEMENT_HEADER_RE = re.compile(
    r"CONSOLIDATED\s+(BALANCE\s+SHEETS?|STATEMENTS?\s+OF\s+"
    r"(INCOME|OPERATIONS|EARNINGS|CASH\s+FLOWS))",
    re.IGNORECASE,
)
_REDIRECT_SPAN_THRESHOLD = 3000   # chars; real Item 8 sections are much longer
_REDIRECT_WINDOW = 300_000        # generous cap on how far to read past the anchor


def _has_real_table(html_slice: str) -> bool:
    """
    A 'real' financial-statement table, not a Table-of-Contents/Index entry.
    An index row ("Consolidated Balance Sheets ... 60") also has a <table>
    and digits (the page number), so digits alone false-positived on ON's
    "Index to Financial Statements" table. The real statement always carries
    a scale/period marker ("(in millions", "Year Ended...") that a bare page
    reference never does -- require one in addition to the digit check.
    """
    if "<table" not in html_slice.lower():
        return False
    text = BeautifulSoup(html_slice, "html.parser").get_text()
    if not re.search(r"\d{2,}", text):
        return False
    return bool(re.search(
        r"\$|\(in\s+(thousands|millions)|year\s+ended|fiscal\s+year\s+ended",
        text, re.IGNORECASE,
    ))


def _find_redirected_statements(html_str: str, search_from: int) -> int | None:
    """Find the first real (not TOC) occurrence of a primary statement title
    at or after search_from. Returns its char position, or None."""
    for m in _STATEMENT_HEADER_RE.finditer(html_str, search_from):
        if _has_real_table(html_str[m.start():m.start() + 3000]):
            return m.start()
    return None


def _download_html(url: str) -> bytes:
    resp = httpx.get(url, headers=EDGAR_HEADERS, timeout=60, follow_redirects=True)
    resp.raise_for_status()
    return resp.content


def _table_to_markdown(table: Tag) -> str:
    """Convert a BeautifulSoup <table> element to a Markdown table string."""
    rows = []
    for tr in table.find_all("tr"):
        cells = []
        for cell in tr.find_all(["td", "th"]):
            text = cell.get_text(separator=" ", strip=True)
            text = re.sub(r"\s+", " ", text).strip()
            # Escape pipe characters inside cells
            text = text.replace("|", "/")
            cells.append(text)
        if any(c for c in cells):
            rows.append("| " + " | ".join(cells) + " |")

    if not rows:
        return ""
    # Insert Markdown separator after header row
    if len(rows) > 1:
        col_count = rows[0].count("|") - 1
        sep = "| " + " | ".join(["---"] * max(col_count, 1)) + " |"
        rows.insert(1, sep)

    return "\n".join(rows)


def _find_item8_boundaries(html_str: str) -> tuple[int, int] | None:
    """
    Return (start, end) char positions of the Item 8 section in the raw HTML string.

    EDGAR 10-Ks often have two occurrences of "Item 8":
    1. Table-of-contents entry (short, followed immediately by Item 9 TOC entry)
    2. Actual section heading (followed by thousands of chars of financial data)

    We select the match with the largest span to the next Item 9 heading.
    Returns None if Item 8 not found at all.

    If the selected span is short (a cross-reference to Item 15/Part IV
    rather than the statements themselves), falls back to searching forward
    for the real statement content wherever it physically lives -- see
    _find_redirected_statements.
    """
    best_start = best_end = None
    best_span = 0

    for m8 in _ITEM8_RE.finditer(html_str):
        m9 = _ITEM9_RE.search(html_str, m8.end() + 500)
        if not m9:
            continue
        span = m9.start() - m8.start()
        if span > best_span:
            best_span = span
            best_start = m8.start()
            best_end = m9.start()

    if best_start is None:
        return None

    if best_span < _REDIRECT_SPAN_THRESHOLD or not _has_real_table(html_str[best_start:best_end]):
        anchor = _find_redirected_statements(html_str, best_start)
        if anchor is not None:
            return anchor, min(anchor + _REDIRECT_WINDOW, len(html_str))

    return best_start, best_end


def _extract_item8_blocks(html_content: bytes) -> list[tuple[str, str]]:
    """
    Return ordered list of (chunk_type, text) from Item 8 section.
    chunk_type: 'table' | 'text'

    Strategy:
      - Find Item 8 section in raw HTML by string position
      - Parse the slice with BeautifulSoup
      - Two passes: (1) convert top-level tables to Markdown,
                    (2) remove all tables and extract prose text
    """
    # Decode HTML — try UTF-8, fall back to latin-1
    try:
        html_str = html_content.decode("utf-8")
    except UnicodeDecodeError:
        html_str = html_content.decode("latin-1")

    bounds = _find_item8_boundaries(html_str)
    if not bounds:
        return []

    start, end = bounds
    item8_html = html_str[start:end]

    # ── Pass 1: extract top-level tables ──────────────────────────────────────
    # script/style are pure metadata, safe to fully delete. ix:nonfraction/
    # ix:nonnumeric are NOT -- in modern EDGAR filings the tagged number/text
    # IS the tag's content (e.g. <ix:nonfraction>1,959</ix:nonfraction>), so
    # decompose() was deleting every dollar figure in these tables along with
    # the wrapper. unwrap() keeps the inner text, removes only the wrapper.
    # Found 2026-07-22 on TXN's Item 8 income statement (every value blanked).
    soup_tables = BeautifulSoup(item8_html, "html.parser")
    for tag in soup_tables(["script", "style"]):
        tag.decompose()
    for tag in soup_tables(["ix:nonfraction", "ix:nonnumeric"]):
        tag.unwrap()

    table_blocks: list[str] = []
    for table in soup_tables.find_all("table"):
        if table.find_parent("table"):
            continue  # skip nested tables
        md = _table_to_markdown(table)
        # Only keep tables with real data (> 2 rows and some numeric content)
        if md and len(md.split("\n")) >= 3 and re.search(r"\d", md):
            table_blocks.append(md)

    # ── Pass 2: prose text (tables removed) ───────────────────────────────────
    soup_prose = BeautifulSoup(item8_html, "html.parser")
    for tag in soup_prose(["script", "style"]):
        tag.decompose()
    for tag in soup_prose(["ix:nonfraction", "ix:nonnumeric"]):
        tag.unwrap()
    for table in soup_prose.find_all("table"):
        table.decompose()

    prose = soup_prose.get_text(separator="\n")
    prose = prose.encode("ascii", errors="ignore").decode("ascii")
    prose = re.sub(r"\n{3,}", "\n\n", prose)
    prose = re.sub(r" {2,}", " ", prose).strip()

    # ── Combine: prose first (context), then tables ───────────────────────────
    blocks: list[tuple[str, str]] = []
    if prose.strip():
        blocks.append(("text", prose))
    for md in table_blocks:
        blocks.append(("table", md))

    return blocks


def _chunk_text(text: str) -> list[str]:
    """Split text into overlapping token-based chunks."""
    tokens = enc.encode(text)
    chunks: list[str] = []
    start = 0
    while start < len(tokens):
        end = min(start + CHUNK_TOKENS, len(tokens))
        chunks.append(enc.decode(tokens[start:end]))
        if end == len(tokens):
            break
        start += CHUNK_TOKENS - CHUNK_OVERLAP
    return chunks


def _chunk_table(md: str) -> list[str]:
    """Split a Markdown table into chunks at TABLE_MAX_TOKENS boundary."""
    tokens = enc.encode(md)
    if len(tokens) <= TABLE_MAX_TOKENS:
        return [md]
    # Split by rows to avoid cutting mid-row
    rows = md.split("\n")
    chunks: list[str] = []
    current: list[str] = []
    current_toks = 0
    for row in rows:
        row_toks = len(enc.encode(row))
        if current_toks + row_toks > TABLE_MAX_TOKENS and current:
            chunks.append("\n".join(current))
            current = []
            current_toks = 0
        current.append(row)
        current_toks += row_toks
    if current:
        chunks.append("\n".join(current))
    return chunks


def _get_max_chunk_index(conn, accn: str) -> int:
    """Return the highest existing chunk_index for this accession, or -1."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT MAX(chunk_index) AS max_idx FROM text_chunks WHERE accn = %s",
            (accn,),
        )
        row = cur.fetchone()
        val = row["max_idx"] if row else None
        return int(val) if val is not None else -1


def _upsert_item8_chunks(
    conn,
    ticker: str,
    accn: str,
    blocks: list[tuple[str, str]],
) -> int:
    """Insert Item 8 chunks, continuing chunk_index from max existing."""
    max_idx = _get_max_chunk_index(conn, accn)
    chunk_index = max_idx + 1
    total = 0

    with conn.cursor() as cur:
        for chunk_type, content in blocks:
            if chunk_type == "table":
                raw_chunks = _chunk_table(content)
            else:
                raw_chunks = _chunk_text(content)

            for chunk in raw_chunks:
                chunk_clean = chunk.strip()
                if not chunk_clean:
                    continue
                token_count = len(enc.encode(chunk_clean))
                cur.execute(
                    """
                    INSERT INTO text_chunks
                        (accn, ticker, section, chunk_index, text, token_count, chunk_type)
                    VALUES (%s, %s, 'Item 8', %s, %s, %s, %s)
                    ON CONFLICT (accn, chunk_index) DO NOTHING
                    """,
                    (accn, ticker, chunk_index, chunk_clean, token_count, chunk_type),
                )
                chunk_index += 1
                total += 1

    conn.commit()
    return total


def get_filings(conn, ticker: str | None, years: int | None) -> list[dict]:
    """Fetch filings from DB, optionally filtered by ticker and year count."""
    with conn.cursor() as cur:
        if ticker:
            cur.execute(
                """
                SELECT accn, ticker, doc_url, fiscal_year
                FROM filings
                WHERE ticker = %s AND form = '10-K'
                ORDER BY fiscal_year DESC
                """,
                (ticker.upper(),),
            )
        else:
            cur.execute(
                """
                SELECT accn, ticker, doc_url, fiscal_year
                FROM filings
                WHERE form = '10-K'
                ORDER BY ticker, fiscal_year DESC
                """
            )
        rows = [dict(row) for row in cur.fetchall()]

    if years and ticker:
        rows = rows[:years]
    return rows


def already_has_item8(conn, accn: str) -> bool:
    """Return True if this accession already has Item 8 chunks."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM text_chunks WHERE accn = %s AND section = 'Item 8' LIMIT 1",
            (accn,),
        )
        return cur.fetchone() is not None


def ingest_item8_for_filing(conn, filing: dict, skip_existing: bool = True) -> int:
    accn = filing["accn"]
    ticker = filing["ticker"]
    doc_url = filing["doc_url"]
    fy = filing["fiscal_year"]

    if skip_existing and already_has_item8(conn, accn):
        print(f"[{ticker}] {accn} FY{fy} — already has Item 8, skipping")
        return 0

    try:
        html = _download_html(doc_url)
    except Exception as e:
        print(f"[{ticker}] {accn} FY{fy} — download error: {e}")
        return 0

    blocks = _extract_item8_blocks(html)
    if not blocks:
        print(f"[{ticker}] {accn} FY{fy} — Item 8 not found in HTML")
        return 0

    n = _upsert_item8_chunks(conn, ticker, accn, blocks)
    table_count = sum(1 for t, _ in blocks if t == "table")
    print(f"[{ticker}] {accn} FY{fy} — {n} chunks ({table_count} tables, {len(blocks)-table_count} text)")
    return n


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest Item 8 Financial Statements from 10-K HTML")
    parser.add_argument("--ticker", default=None, help="Single ticker to process")
    parser.add_argument("--years", type=int, default=None, help="Max filings per ticker")
    parser.add_argument(
        "--cluster",
        default="research",
        choices=["v1", "research"],
        help="v1=6 companies, research=15-company cluster",
    )
    parser.add_argument(
        "--rerun", action="store_true",
        help="Re-process filings that already have Item 8 chunks",
    )
    args = parser.parse_args()

    conn = get_conn()
    migrate_add_chunk_type(conn)

    filings = get_filings(conn, args.ticker, args.years)
    print(f"Found {len(filings)} filings to process.")

    total_chunks = 0
    for i, filing in enumerate(filings):
        n = ingest_item8_for_filing(conn, filing, skip_existing=not args.rerun)
        total_chunks += n
        if i > 0 and i % 5 == 0:
            time.sleep(0.5)  # polite EDGAR rate limit

    conn.close()
    print(f"\nDone. {total_chunks} Item 8 chunks added across {len(filings)} filings.")


if __name__ == "__main__":
    main()
