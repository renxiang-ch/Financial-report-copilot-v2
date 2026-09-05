"""
Ingest XBRL financial facts from EDGAR into Postgres — the one and only
`financial_facts` ingestion path.

History: this file merges two predecessors (2026-08-26). `fix_financial_facts.py`
carried the corrected extraction logic but assumed `companies` already existed;
`ingest_xbrl.py` could bootstrap an empty database but only knew the original
6-company/10-label scope and applied none of the corrections below — re-running
it would have re-inserted rows the corrected logic deliberately rejects. Keeping
both meant every re-run was one wrong module name away from silently corrupting
the data, so they are now one script: corrected logic, plus `companies` upsert
and empty-database bootstrap. Both old files are gone; there is no other
ingestion path to reach for.

The corrected logic guards against two structural bugs found 2026-07-30 while
auditing data reliability:

Bug 1 — duration mismatch: SEC's companyfacts API sometimes reports a
quarterly-duration fact under the same tag used for the annual figure (e.g.
ON Semiconductor's `Revenues` tag carries both quarterly and annual entries,
some with a period_end that ties with the true annual figure's period_end).
Fix: reject 10-K duration-type facts whose (end-start) span isn't 355-380
days before they're candidates for the annual figure.

Bug 2 — tag collision: multiple XBRL tags map to the same label, and for
7/20 labels this produces genuinely different values, not just formatting
noise (confirmed against official FASB/XBRL element definitions from
calcbench.com). Fix, per label:
  - 4 labels SPLIT into two distinct concepts (the collision was a real
    difference in financial meaning, not a synonym):
      LongTermDebt (kept name, now sourced from LongTermDebtNoncurrent —
        true long-term excl. current) / TotalDebt (new, from LongTermDebt tag
        — includes current portion)
      TotalEquity (kept name, from StockholdersEquity — parent-only) /
        TotalEquityInclNCI (new, includes noncontrolling interest)
      InterestExpense (kept name, from InterestExpense tag — total) /
        InterestExpenseOnDebt (new, from InterestExpenseDebt — debt-only)
      D&A (kept name, from DepreciationDepletionAndAmortization — cash-flow-
        statement comprehensive figure) / D&A_Component (new, from
        DepreciationAndAmortization — income-statement/note figure)
  - 3 labels KEPT as a single label with an explicit tag priority order
    (the collision was legacy/era tag transition or negligible-magnitude
    noise, not two concepts a company reports simultaneously by design):
      COGS: CostOfGoodsAndServicesSold > CostOfRevenue > CostOfGoodsSold
        (the last is officially FASB-deprecated as of 2018-01-31)
      Revenue: RevenueFromContractWithCustomerExcludingAssessedTax > Revenues
        > SalesRevenueNet > RevenueFromContractWithCustomerIncludingAssessedTax
      CapEx: PaymentsToAcquirePropertyPlantAndEquipment >
        PaymentsForCapitalImprovements (never actually collide in this data,
        priority exists for completeness/future-proofing only)

Known residual, out of scope: the SAME tag can still report two different
values for the same (ticker, fiscal_year, period_end) when a later filing
restates a prior comparative period — this is a pre-existing, separate
ambiguity class (not the tag-collision or duration bug above) and is left to
`query_financials()`'s existing `ORDER BY period_end DESC LIMIT 1` behavior.

Note for reproduction: the published `data/seed/` snapshot, not this script,
is what every published result was measured against — load that first. This
script is the "rebuild from EDGAR" path, and EDGAR data moves (new filings,
restatements), so a fresh run is not guaranteed to reproduce the snapshot
byte-for-byte.

Usage:
    python -m copilot.pipeline.ingest_financial_facts                 # all 15 companies, writes
    python -m copilot.pipeline.ingest_financial_facts --ticker AAPL   # single company
    python -m copilot.pipeline.ingest_financial_facts --dry-run       # report only, no writes
"""

import argparse
import time
from collections import defaultdict
from datetime import date

import httpx

from copilot.pipeline.companies import CIK_OVERRIDES, CLUSTER_RESEARCH
from copilot.storage.db import get_conn
from copilot.storage.schema import create_tables, migrate_add_period_start

EDGAR_HEADERS = {"User-Agent": "financial-copilot research renxiangchao2678@gmail.com"}

# tag -> label. Where a label still has >1 tag mapped, LABEL_TAG_PRIORITY
# below resolves ties; where a label was split, each half has exactly one
# tag mapped and no priority list is needed.
TAG_LABELS: dict[str, str] = {
    # Income statement
    "RevenueFromContractWithCustomerExcludingAssessedTax": "Revenue",
    "RevenueFromContractWithCustomerIncludingAssessedTax": "Revenue",
    "Revenues": "Revenue",
    "SalesRevenueNet": "Revenue",
    "GrossProfit": "GrossProfit",
    "CostOfGoodsAndServicesSold": "COGS",
    "CostOfRevenue": "COGS",
    "CostOfGoodsSold": "COGS",
    "OperatingIncomeLoss": "OperatingIncome",
    "NetIncomeLoss": "NetIncome",
    "EarningsPerShareBasic": "EPS_Basic",
    "EarningsPerShareDiluted": "EPS_Diluted",
    "ResearchAndDevelopmentExpense": "R&D",
    "IncomeTaxExpenseBenefit": "IncomeTaxExpense",
    "InterestExpense": "InterestExpense",
    "InterestExpenseDebt": "InterestExpenseOnDebt",
    "DepreciationDepletionAndAmortization": "D&A",
    "DepreciationAndAmortization": "D&A_Component",
    # Balance sheet
    "Assets": "TotalAssets",
    "AssetsCurrent": "CurrentAssets",
    "LiabilitiesCurrent": "CurrentLiabilities",
    "LongTermDebtNoncurrent": "LongTermDebt",
    "LongTermDebt": "TotalDebt",
    "StockholdersEquity": "TotalEquity",
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest": "TotalEquityInclNCI",
    "PropertyPlantAndEquipmentNet": "PP&E",
    "InventoryNet": "Inventory",
    # Cash flow
    "NetCashProvidedByUsedInOperatingActivities": "OperatingCashFlow",
    "PaymentsToAcquirePropertyPlantAndEquipment": "CapEx",
    "PaymentsForCapitalImprovements": "CapEx",
}

LABEL_TAG_PRIORITY: dict[str, list[str]] = {
    "COGS": ["CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold"],
    "Revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
    ],
    "CapEx": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsForCapitalImprovements"],
}

# Duration-type (flow) labels need a start date and a ~365-day span to count
# as "the annual figure". Instant-type (stock) labels are point-in-time and
# have no duration to validate.
DURATION_LABELS = {
    "Revenue", "GrossProfit", "COGS", "OperatingIncome", "NetIncome",
    "EPS_Basic", "EPS_Diluted", "R&D", "IncomeTaxExpense",
    "InterestExpense", "InterestExpenseOnDebt", "D&A", "D&A_Component",
    "OperatingCashFlow", "CapEx",
}
MIN_ANNUAL_DAYS = 355
MAX_ANNUAL_DAYS = 380


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


def get_company_facts(cik: str) -> dict:
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    resp = httpx.get(url, headers=EDGAR_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def upsert_company(conn, ticker: str, cik: str) -> None:
    """`financial_facts.ticker` is only meaningful against a `companies` row,
    and slots._company_index derives its surface forms from companies.name —
    so the name written here must come from the canonical CLUSTER_RESEARCH,
    never a local copy (two copies of the cluster dict once drifted on
    whether SWKS is "Skyworks Solutions" or "Skyworks Solutions Inc.")."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO companies (ticker, name, cik)
            VALUES (%s, %s, %s)
            ON CONFLICT (ticker) DO UPDATE SET name = EXCLUDED.name, cik = EXCLUDED.cik
            """,
            (ticker, CLUSTER_RESEARCH[ticker], cik),
        )
    conn.commit()


def _duration_ok(label: str, form: str, start: str | None, end: str) -> bool:
    """
    The annual/quarterly duration-mismatch bug (ON Semiconductor's Revenues
    tag carrying a quarterly slice with a period_end that ties the true
    annual figure's period_end) only shows up in 10-K filings, where a
    reader implicitly expects every duration fact to be the annual figure.
    10-Q filings are supposed to carry quarterly-duration facts (~90 days)
    -- that's correct, pre-existing, intentional data (financial_facts has
    always included 10-Q quarterly figures alongside 10-K annual ones), not
    a bug, so this check must not touch them.
    """
    if form != "10-K" or label not in DURATION_LABELS:
        return True  # instant concept, or a 10-Q's legitimate quarterly duration
    if not start:
        return False  # a 10-K duration concept must have a start date
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days
    return MIN_ANNUAL_DAYS <= days <= MAX_ANNUAL_DAYS


def extract_facts(ticker: str, facts: dict) -> tuple[list[dict], dict]:
    """Returns (rows, stats). stats reports what was filtered and why, so a
    dry-run can be inspected without guessing what the ingestion actually did."""
    gaap = facts.get("facts", {}).get("us-gaap", {})
    stats = {"tags_seen": 0, "facts_seen": 0, "skipped_duration": 0,
              "skipped_lower_priority": 0, "kept": 0}

    # group candidate rows by (label, fiscal_year, period_end) so we can
    # apply LABEL_TAG_PRIORITY when >1 tag produced a fact for the same slot
    candidates: dict[tuple, list[tuple[int, dict]]] = defaultdict(list)

    for tag, label in TAG_LABELS.items():
        if tag not in gaap:
            continue
        stats["tags_seen"] += 1
        priority_list = LABEL_TAG_PRIORITY.get(label)
        rank = priority_list.index(tag) if priority_list and tag in priority_list else 0

        for unit_key, filings in gaap[tag].get("units", {}).items():
            for f in filings:
                if f.get("form") not in ("10-K", "10-Q"):
                    continue
                stats["facts_seen"] += 1
                start, end = f.get("start"), f["end"]
                if not _duration_ok(label, f["form"], start, end):
                    stats["skipped_duration"] += 1
                    continue
                row = {
                    "ticker": ticker,
                    "tag": tag,
                    "label": label,
                    "value": f["val"],
                    "unit": unit_key,
                    "period_start": start,
                    "period_end": end,
                    "fiscal_year": f.get("fy"),
                    "fiscal_period": f.get("fp"),
                    "form": f.get("form"),
                    "accn": f["accn"],
                }
                key = (label, f.get("fy"), end)
                candidates[key].append((rank, row))

    rows = []
    for key, cands in candidates.items():
        best_rank = min(c[0] for c in cands)
        for rank, row in cands:
            if rank == best_rank:
                rows.append(row)
            else:
                stats["skipped_lower_priority"] += 1
    stats["kept"] = len(rows)
    return rows, stats


def replace_ticker_facts(conn, ticker: str, rows: list[dict], dry_run: bool) -> int:
    if dry_run:
        return len(rows)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM financial_facts WHERE ticker = %s", (ticker,))
        for r in rows:
            cur.execute(
                """
                INSERT INTO financial_facts
                    (ticker, tag, label, value, unit, period_start, period_end,
                     fiscal_year, fiscal_period, form, accn)
                VALUES
                    (%(ticker)s, %(tag)s, %(label)s, %(value)s, %(unit)s, %(period_start)s,
                     %(period_end)s, %(fiscal_year)s, %(fiscal_period)s, %(form)s, %(accn)s)
                ON CONFLICT (ticker, tag, period_end, accn) DO NOTHING
                """,
                r,
            )
    conn.commit()
    return len(rows)


def ingest_ticker(ticker: str, conn, dry_run: bool) -> None:
    print(f"[{ticker}] fetching CIK...")
    cik = get_cik(ticker)
    print(f"[{ticker}] CIK={cik}, fetching fresh companyfacts...")
    facts = get_company_facts(cik)
    if not dry_run:
        upsert_company(conn, ticker, cik)
    rows, stats = extract_facts(ticker, facts)
    n = replace_ticker_facts(conn, ticker, rows, dry_run)
    tag = "[DRY RUN] would write" if dry_run else "wrote"
    print(f"[{ticker}] {tag} {n} rows -- {stats}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    tickers = [args.ticker.upper()] if args.ticker else list(CLUSTER_RESEARCH.keys())

    conn = get_conn()
    create_tables(conn)  # no-op on an existing DB, bootstrap on an empty one
    migrate_add_period_start(conn)

    for i, ticker in enumerate(tickers):
        if i > 0:
            time.sleep(0.3)
        try:
            ingest_ticker(ticker, conn, args.dry_run)
        except Exception as e:
            print(f"[{ticker}] ERROR: {e}")

    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
