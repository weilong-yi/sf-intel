"""How well did entity resolution work? Match rates, distances, and failures.

Run after `python -m src.build`:  python experiments/02_match_quality.py
"""

import sys

import duckdb

sys.stdout.reconfigure(encoding="utf-8")
con = duckdb.connect("data/sf_intel.duckdb", read_only=True)


def show(title, sql):
    print(f"\n--- {title}")
    print(con.sql(sql))


show("match rate by source and method", """
    SELECT source, coalesce(match_method, 'UNMATCHED') method, count(*) n,
           round(100.0 * count(*) / sum(count(*)) OVER (PARTITION BY source), 1) pct
    FROM event GROUP BY 1, 2 ORDER BY source, n DESC""")

show("311 snap distance (metres) for nearest_intersection matches", """
    SELECT round(quantile_cont(match_distance_m, 0.5)) p50,
           round(quantile_cont(match_distance_m, 0.9)) p90,
           round(quantile_cont(match_distance_m, 0.99)) p99,
           max(match_distance_m) max
    FROM event WHERE match_method = 'nearest_intersection'""")

show("cross-source coverage: intersections seen by >1 system", """
    SELECT array_to_string(seen_in_sources, ' + ') sources, count(*) intersections
    FROM location GROUP BY ALL ORDER BY intersections DESC""")

for src, col in [("police", "raw_address"), ("fire_ems", "raw_address"), ("311", "raw_address")]:
    show(f"top UNMATCHED raw addresses: {src}", f"""
        SELECT {col}, count(*) n FROM event
        WHERE source = '{src}' AND location_id IS NULL
        GROUP BY ALL ORDER BY n DESC LIMIT 6""")

show("busiest intersections across all three systems", """
    SELECT l.label, l.neighborhood,
           count(*) FILTER (e.source = 'police')   police,
           count(*) FILTER (e.source = 'fire_ems') fire_ems,
           count(*) FILTER (e.source = '311')      s311,
           count(*) total
    FROM event e JOIN location l USING (location_id)
    GROUP BY ALL ORDER BY total DESC LIMIT 10""")
