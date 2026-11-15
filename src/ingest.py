"""Step 1 of the pipeline: pull raw data from each source system to disk.

We land the data untouched (CSV, exactly as the source returns it) before
transforming anything. That "raw zone" lets us re-run every downstream
step without hitting the source again, and lets us prove what the source
actually said if a number is ever questioned.

Run:  python -m src.ingest
"""

import datetime as dt
import time
import urllib.parse
import urllib.request
from pathlib import Path

from src.sources import SOCRATA_BASE, SOURCES, WINDOW_DAYS

RAW_DIR = Path("data/raw")
ROW_LIMIT = 1_000_000  # safety cap; 90 days is ~250k rows at most


def download_source(name: str, since: dt.date) -> Path:
    src = SOURCES[name]
    params = {
        "$select": ",".join(src["columns"]),
        "$where": f"{src['time_column']} >= '{since.isoformat()}T00:00:00'",
        "$order": f"{src['time_column']} ASC",
        "$limit": str(ROW_LIMIT),
    }
    url = f"{SOCRATA_BASE}/{src['dataset_id']}.csv?{urllib.parse.urlencode(params)}"
    out = RAW_DIR / f"{name}.csv"

    start = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": "sf-intel-demo/0.1"})
    with urllib.request.urlopen(req, timeout=300) as resp, open(out, "wb") as f:
        while chunk := resp.read(1 << 20):
            f.write(chunk)

    rows = sum(1 for _ in open(out, encoding="utf-8")) - 1  # minus header
    print(f"  {name:<9} {rows:>8,} rows  {out.stat().st_size / 1e6:6.1f} MB  "
          f"{time.time() - start:5.1f}s  <- {src['label']}")
    if rows >= ROW_LIMIT:
        print(f"  WARNING: {name} hit ROW_LIMIT; data is truncated")
    return out


def main() -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    since = dt.date.today() - dt.timedelta(days=WINDOW_DAYS)
    print(f"Ingesting {len(SOURCES)} sources since {since} ({WINDOW_DAYS} days)")
    for name in SOURCES:
        download_source(name, since)


if __name__ == "__main__":
    main()
