"""Profile the raw CSVs before designing the model: grain, nulls, address formats.

Run: python experiments/01_profile_raw.py
"""

import sys

import duckdb

sys.stdout.reconfigure(encoding="utf-8")  # Windows console + DuckDB table borders
con = duckdb.connect()
for name in ["police", "fire_ems", "311"]:
    con.execute(f"CREATE VIEW {'s311' if name == '311' else name} AS "
                f"SELECT * FROM read_csv('data/raw/{name}.csv', all_varchar=true)")


def show(title, sql):
    print(f"\n--- {title}")
    print(con.sql(sql))


# Grain: is one row one event?
show("police: rows vs distinct incidents (one incident can have several offense rows)", """
    SELECT count(*) rows, count(DISTINCT incident_id) incidents,
           count(DISTINCT row_id) row_ids FROM police""")
show("fire_ems: rows vs distinct calls (one row per dispatched UNIT)", """
    SELECT count(*) rows, count(DISTINCT call_number) calls,
           count(DISTINCT incident_number) incidents FROM fire_ems""")
show("311: rows vs distinct requests", """
    SELECT count(*) rows, count(DISTINCT service_request_id) requests FROM s311""")

# Location quality
show("missing location", """
    SELECT 'police' src, count(*) FILTER (latitude IS NULL) no_coords,
           count(*) FILTER (intersection IS NULL) no_address, count(*) total FROM police
    UNION ALL SELECT 'fire_ems', count(*) FILTER (case_location IS NULL),
           count(*) FILTER (address IS NULL), count(*) FROM fire_ems
    UNION ALL SELECT '311', count(*) FILTER (lat IS NULL OR lat = '0'),
           count(*) FILTER (address IS NULL), count(*) FROM s311""")

show("address samples", """
    (SELECT 'police' src, intersection addr FROM police WHERE intersection IS NOT NULL LIMIT 3)
    UNION ALL (SELECT 'fire_ems', address FROM fire_ems WHERE address LIKE '%Block%' LIMIT 3)
    UNION ALL (SELECT 'fire_ems', address FROM fire_ems WHERE address LIKE '%/%' LIMIT 3)
    UNION ALL (SELECT '311', address FROM s311 LIMIT 3)
    UNION ALL (SELECT 'fire coords', case_location FROM fire_ems LIMIT 1)""")

show("fire_ems address shapes", """
    SELECT CASE WHEN address LIKE '%Block of%' THEN 'block'
                WHEN address LIKE '%/%' THEN 'intersection'
                ELSE 'other' END shape, count(*) n,
           any_value(address) example
    FROM fire_ems GROUP BY 1 ORDER BY n DESC""")

show("311 address shapes", """
    SELECT CASE WHEN regexp_matches(address, '^[0-9]+ ') THEN 'street number'
                WHEN address LIKE '%INTERSECTION%' OR address LIKE '% & %' THEN 'intersection'
                ELSE 'other' END shape, count(*) n, any_value(address) example
    FROM s311 GROUP BY 1 ORDER BY n DESC""")

show("police: do rows with same cad_number span incidents?", """
    SELECT count(*) FILTER (cad_number IS NULL) no_cad,
           count(DISTINCT cad_number) cads, count(DISTINCT incident_id) incidents FROM police""")

show("police report types (filed online = no officer on scene)", """
    SELECT report_type_description, count(*) n FROM police GROUP BY 1 ORDER BY n DESC""")
