"""Fetch raw 10-K documents from SEC EDGAR for the generic-agent baseline.

Pulls the ``doc_url`` already recorded in ``data/seed/filings.csv.gz`` (v1's
ingestion already resolved these) for the six tickers the eval sets touch --
no new EDGAR lookups needed. Output is deliberately *not* the pipeline's
structured extraction: it's the same raw documents a human analyst (or a
zero-customization agent) would start from. See docs/generic-agent-baseline.md.

Usage::

    uv run python scripts/fetch_raw_filings.py
"""

from __future__ import annotations

import csv
import gzip
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
FILINGS_GZ = REPO_ROOT / "data" / "seed" / "filings.csv.gz"
OUT_DIR = REPO_ROOT / "baseline" / "raw_filings"

# The six tickers eval_set.json (Tier 1-2) + eval_set_tier3.json touch.
TICKERS = {"AAPL", "AVGO", "CRUS", "GLW", "QRVO", "SWKS"}

# SEC requires a descriptive User-Agent with contact info on automated requests
# (https://www.sec.gov/os/webmaster-faq#developers). Rate limit stays well
# under SEC's fair-access guidance.
USER_AGENT = "Financial-Report-Copilot-v2 research-project renxiangchao2678@gmail.com"
DELAY_S = 0.35


def main() -> None:
    with gzip.open(FILINGS_GZ, "rt") as f:
        rows = [r for r in csv.DictReader(f) if r["ticker"] in TICKERS]

    print(f"{len(rows)} filings to fetch for {sorted(TICKERS)}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    ok = skip = fail = 0
    failures: list[tuple[str, str]] = []
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30,
                       follow_redirects=True) as client:
        for i, r in enumerate(rows, 1):
            ticker_dir = OUT_DIR / r["ticker"]
            ticker_dir.mkdir(exist_ok=True)
            ext = Path(r["doc_url"]).suffix or ".htm"
            fname = f"{r['fiscal_year']}_{r['form']}_{r['accn']}{ext}"
            dest = ticker_dir / fname

            if dest.exists() and dest.stat().st_size > 0:
                skip += 1
                continue

            try:
                resp = client.get(r["doc_url"])
                resp.raise_for_status()
                dest.write_bytes(resp.content)
                ok += 1
                print(f"[{i}/{len(rows)}] OK   {r['ticker']} FY{r['fiscal_year']} "
                      f"-> {dest.relative_to(REPO_ROOT)} ({len(resp.content):,} bytes)")
            except Exception as e:  # noqa: BLE001 -- one bad filing shouldn't kill the sweep
                fail += 1
                failures.append((r["doc_url"], str(e)))
                print(f"[{i}/{len(rows)}] FAIL {r['ticker']} FY{r['fiscal_year']}: {e}")

            time.sleep(DELAY_S)

    print(f"\ndone: {ok} fetched, {skip} already present, {fail} failed")
    if failures:
        print("\nfailures:")
        for url, err in failures:
            print(f"  {url}  ({err})")


if __name__ == "__main__":
    main()
