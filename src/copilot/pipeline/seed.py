"""Export and load the database snapshot the results were produced on.

Why a snapshot rather than "re-run the ingestion"
    Because re-running the ingestion does not rebuild this database, and saying
    it does would be the reproducibility claim that fails quietly. Two reasons,
    both on the record:

    * `supply_edges` carries corrections applied by hand in a psql session --
      Qorvo's FY2018/FY2019 percentages, Skyworks' FY2016/FY2017 threshold
      classification, five Jabil citations, a three-row Huawei entity merge. No
      script contains them.
    * the `ix:nonfraction` fix landed after the last extraction run and changes
      the candidate blocks for 32 of 54 filings. The table was deliberately not
      re-extracted, because the newly visible text is of mixed quality and
      re-running risks introducing wrong edges rather than only recovering
      missing ones.

    So the honest artefact is the snapshot every stored result was measured
    against, plus a description of how it came to be. `db_fingerprint` in each
    result file is what ties the two together: load this seed, run the harness,
    and the fingerprints match or something is wrong.

Why embeddings are not in it
    They are 24MB of float32 that `embed_chunks` reproduces from the chunk text
    with a local model and no API key. The manifest records the model and
    dimension so a mismatch is visible rather than silent. Chunk TEXT is in the
    seed: it is the evidence, and it is not reproducible from anything else here
    without re-fetching every filing from EDGAR.

    Loading therefore leaves `embedding` NULL, and retrieval stays BM25-only
    until `embed_chunks` runs. That is a visible, documented gap rather than a
    quiet one.

Usage
    uv run --active python -m copilot.pipeline.seed --export
    uv run --active python -m copilot.pipeline.seed --load
"""

import argparse
import csv
import gzip
import json
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from copilot.storage.db import get_conn
from copilot.storage.schema import create_tables

SEED_DIR = Path("data/seed")

# Load order is FK order: filings reference companies, text_chunks reference
# filings. Truncation runs in reverse.
TABLES: list[tuple[str, tuple[str, ...]]] = [
    ("companies",       ("ticker", "name", "cik", "created_at")),
    ("filings",         ("accn", "ticker", "form", "filed_date", "fiscal_year", "doc_url")),
    # `tag` is the raw XBRL tag the value came from. It is what makes the
    # tag -> label mapping auditable after the fact, so a reader can check the
    # mapping rather than take the label on trust.
    ("financial_facts", ("ticker", "tag", "label", "value", "unit", "period_start",
                         "period_end", "fiscal_year", "fiscal_period", "form", "accn")),
    # embedding and embedding_v1 are excluded on purpose -- see the module
    # docstring. `id` is excluded because it is a surrogate key.
    ("text_chunks",     ("accn", "ticker", "section", "chunk_index", "text",
                         "token_count", "chunk_type")),
    # supply_edges.chunk_id references text_chunks.id, a surrogate key the seed
    # does not carry. It is exported separately as a natural key and rewired on
    # load -- see LINKS_FILE. Dropping it would be easier and would cost the
    # thing this project is about: the pointer from an edge to the exact chunk it
    # was extracted from.
    ("supply_edges",    ("supplier_ticker", "customer_ticker", "revenue_pct", "fiscal_year",
                         "disclosure_status", "accn", "source_text", "threshold_only",
                         "extracted_at")),
]


LINKS_FILE = "supply_edge_chunk_links.csv.gz"

# The edge's natural key -- the same one the UNIQUE constraint uses -- plus the
# chunk's natural key. Both survive a reload; the surrogate ids do not.
_LINKS_SQL = """
SELECT e.supplier_ticker, e.customer_ticker, e.fiscal_year,
       c.accn AS chunk_accn, c.chunk_index AS chunk_index
FROM supply_edges e JOIN text_chunks c ON c.id = e.chunk_id
ORDER BY 1, 2, 3
"""

_REWIRE_SQL = """
UPDATE supply_edges e SET chunk_id = c.id
FROM text_chunks c
WHERE c.accn = %s AND c.chunk_index = %s
  AND e.supplier_ticker = %s AND e.customer_ticker = %s AND e.fiscal_year = %s
"""


def _columns_present(conn, table: str, wanted: tuple[str, ...]) -> list[str]:
    """Intersect the wanted columns with what this database actually has.

    A seed that assumes a column exists is the same class of defect this file is
    here to fix, one level down.
    """
    with conn.cursor() as cur:
        cur.execute("""SELECT column_name FROM information_schema.columns
                       WHERE table_schema = 'public' AND table_name = %s""", (table,))
        live = {r["column_name"] for r in cur.fetchall()}
    missing = [c for c in wanted if c not in live]
    if missing:
        print(f"  note: {table} has no {missing} in this database -- skipped")
    return [c for c in wanted if c in live]


def _fingerprint(conn) -> dict:
    out = {}
    with conn.cursor() as cur:
        for table, _ in TABLES:
            cur.execute(f"SELECT count(*) AS n FROM {table}")
            out[table] = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM supply_edges WHERE disclosure_status = 'named'")
        out["named_edges"] = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM text_chunks WHERE embedding IS NOT NULL")
        out["embedded_chunks"] = cur.fetchone()["n"]
    return out


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def export(out_dir: Path = SEED_DIR) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = get_conn()
    manifest = {
        "created": datetime.now().astimezone().isoformat(timespec="seconds"),
        "fingerprint": _fingerprint(conn),
        "embeddings": {
            "included": False,
            "model": "BAAI/bge-small-en-v1.5",
            "dim": 384,
            "regenerate_with": "python -m copilot.pipeline.embed_chunks",
        },
        "tables": {},
    }
    try:
        for table, wanted in TABLES:
            cols = _columns_present(conn, table, wanted)
            path = out_dir / f"{table}.csv.gz"
            rows = 0
            with conn.cursor(name=f"seed_{table}") as cur:      # server-side cursor
                cur.itersize = 2000
                cur.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY 1, 2")
                with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
                    writer = csv.writer(fh)
                    writer.writerow(cols)
                    for row in cur:
                        writer.writerow([_cell(row[c]) for c in cols])
                        rows += 1
            size_mb = round(path.stat().st_size / 1e6, 2)
            manifest["tables"][table] = {"rows": rows, "columns": cols, "gz_mb": size_mb}
            print(f"  {table:<18} {rows:>7} rows  ->  {path}  ({size_mb} MB gz)")

        with conn.cursor() as cur:
            cur.execute(_LINKS_SQL)
            links = cur.fetchall()
        path = out_dir / LINKS_FILE
        with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["supplier_ticker", "customer_ticker", "fiscal_year",
                             "chunk_accn", "chunk_index"])
            for row in links:
                writer.writerow([_cell(row[c]) for c in
                                 ("supplier_ticker", "customer_ticker", "fiscal_year",
                                  "chunk_accn", "chunk_index")])
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM supply_edges WHERE chunk_id IS NOT NULL")
            with_pointer = cur.fetchone()["n"]
        manifest["chunk_links"] = {
            "resolvable": len(links),
            "edges_with_a_chunk_id": with_pointer,
            # Recorded rather than quietly exported as an empty file. On the
            # source database all of these dangle: text_chunks was re-ingested,
            # its surrogate ids changed, and supply_edges had no foreign key to
            # stop the pointers going stale. Loading the seed therefore leaves
            # chunk_id NULL, which is the truthful state -- carrying the numbers
            # across would reproduce a broken pointer as if it worked, and would
            # be rejected outright by the foreign key a fresh schema declares.
            "note": ("dangling on the source database" if with_pointer and not links
                     else "resolved by natural key"),
        }
        print(f"  {'chunk links':<18} {len(links):>7} resolvable "
              f"of {with_pointer} edges carrying a pointer  ->  {path}")
    finally:
        conn.close()

    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    total = round(sum(t["gz_mb"] for t in manifest["tables"].values()), 2)
    print(f"\n  total {total} MB compressed")
    print(f"  fingerprint {manifest['fingerprint']}")
    return manifest


def load(in_dir: Path = SEED_DIR, *, force: bool = False) -> dict:
    manifest_path = in_dir / "manifest.json"
    if not manifest_path.exists():
        sys.exit(f"no seed at {in_dir} -- run --export first, or check the path")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    conn = get_conn()
    try:
        create_tables(conn)                       # also applies every migration
        before = _fingerprint(conn)
        occupied = {t: n for t, n in before.items() if n and t in dict(TABLES)}
        if occupied and not force:
            conn.close()
            sys.exit(f"target database is not empty ({occupied}). Re-run with "
                     "--force to replace its contents.")

        with conn.cursor() as cur:
            for table, _ in reversed(TABLES):
                cur.execute(f"TRUNCATE {table} CASCADE")
            for table, _ in TABLES:
                path = in_dir / f"{table}.csv.gz"
                with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
                    header = fh.readline().rstrip("\r\n")
                    cur.copy_expert(
                        f"COPY {table} ({header}) FROM STDIN WITH (FORMAT csv, NULL '')",
                        fh)
                print(f"  loaded {table}")
            # Surrogate keys are excluded from the seed, so the sequences are
            # still at 1 and the next insert would collide.
            for table, seq_col in (("text_chunks", "id"), ("supply_edges", "id"),
                                   ("financial_facts", "id")):
                cur.execute(
                    f"SELECT setval(pg_get_serial_sequence('{table}', '{seq_col}'), "
                    f"COALESCE((SELECT MAX({seq_col}) FROM {table}), 1))")

            # Rewire each edge to the chunk it was extracted from. The ids
            # differ from the source database; the natural keys do not.
            links_path = in_dir / LINKS_FILE
            rewired = 0
            if links_path.exists():
                with gzip.open(links_path, "rt", encoding="utf-8", newline="") as fh:
                    for row in csv.DictReader(fh):
                        cur.execute(_REWIRE_SQL, (
                            row["chunk_accn"], int(row["chunk_index"]),
                            row["supplier_ticker"], row["customer_ticker"],
                            int(row["fiscal_year"])))
                        rewired += cur.rowcount
                expected_links = (manifest.get("chunk_links") or {}).get("resolvable", 0)
                mark = "" if rewired == expected_links else "  !! expected " + str(expected_links)
                print(f"  rewired {rewired} edge -> chunk links{mark}")
        conn.commit()

        after = _fingerprint(conn)
    finally:
        conn.close()

    expected = manifest["fingerprint"]
    # embedded_chunks is expected to differ: the seed carries no vectors.
    mismatch = {k: (expected.get(k), after.get(k)) for k in expected
                if k != "embedded_chunks" and expected.get(k) != after.get(k)}
    print(f"\n  expected {expected}")
    print(f"  loaded   {after}")
    if mismatch:
        print(f"  !! MISMATCH {mismatch}")
    else:
        print("  fingerprint matches the snapshot the stored results were measured on")
    print("\n  embeddings are NOT in the seed. Retrieval is BM25-only until:")
    print("    uv run --active python -m copilot.pipeline.embed_chunks")
    return after


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", action="store_true")
    parser.add_argument("--load", action="store_true")
    parser.add_argument("--dir", default=str(SEED_DIR))
    parser.add_argument("--force", action="store_true",
                        help="--load only: replace the contents of a non-empty database")
    args = parser.parse_args()
    if args.export == args.load:
        parser.error("pass exactly one of --export / --load")
    (export if args.export else lambda d: load(d, force=args.force))(Path(args.dir))


if __name__ == "__main__":
    main()
