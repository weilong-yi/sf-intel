"""Step 2 of the pipeline: raw CSVs -> a unified model in DuckDB.

Layers (each one is a set of tables you can query and inspect):

  raw_*        the CSVs exactly as landed (all text)
  stg_*        one cleaned table per source, at the right GRAIN:
                 stg_police  = one row per incident  (raw is one row per offense)
                 stg_fire    = one row per call      (raw is one row per unit)
                 stg_311     = one row per request
  location     the shared "ontology" object every source resolves to:
               a street intersection, keyed by its sorted street names
  event        the unified spine: every police incident, fire/EMS call and
               311 request as one row, linked to a location, with
               provenance (source, source_record_id) and HOW it was matched
               (match_method, match_distance_m)

Run:  python -m src.build
"""

import sys
import time
from pathlib import Path

import duckdb

DB_PATH = Path("data/sf_intel.duckdb")
RAW_DIR = Path("data/raw")
SNAP_MAX_METERS = 250  # 311 addresses further than this from any intersection stay unmatched

# --- Reusable SQL macros --------------------------------------------------
# Street-name normalisation is the heart of entity resolution here. Each
# system spells the same street differently:
#   police "06TH ST \ MISSION ST"   fire "06TH ST/MISSION ST"   311 "6TH ST"
#   fire "VAN NESS AV"              others "VAN NESS AVE"
MACROS = r"""
CREATE OR REPLACE MACRO norm_street(s) AS
    nullif(trim(regexp_replace(regexp_replace(regexp_replace(regexp_replace(
    regexp_replace(regexp_replace(regexp_replace(
        upper(s),
        '\bSTREET\b', 'ST', 'g'),            -- human input: "Market Street"
        '\bAVENUE\b', 'AVE', 'g'),
        '\bBOULEVARD\b', 'BLVD', 'g'),
        '\bAV\b', 'AVE', 'g'),               -- AV -> AVE
        '\b0+([0-9])', '\1', 'g'),           -- 06TH -> 6TH
        '[^A-Z0-9 ]', ' ', 'g'),             -- punctuation -> space
        '\s+', ' ', 'g')), '');

-- 'A ST \ B ST' or 'B ST/A ST' -> 'A ST & B ST' (order-independent key)
CREATE OR REPLACE MACRO intersection_key(s, sep) AS
    nullif(array_to_string(list_sort(list_distinct(list_filter(
        list_transform(string_split_regex(s, sep), lambda x: norm_street(x)),
        lambda x: x IS NOT NULL))), ' & '), '');

-- Good-enough metres between two points at SF's latitude (no GIS extension needed)
CREATE OR REPLACE MACRO dist_m(lat1, lon1, lat2, lon2) AS
    sqrt(power((lat1 - lat2) * 110540, 2) +
         power((lon1 - lon2) * 111320 * cos(radians(37.77)), 2));
"""

STEPS = [
    # ---------------------------------------------------------------- raw
    ("raw_police", "CREATE OR REPLACE TABLE raw_police AS SELECT * FROM read_csv('{raw}/police.csv', all_varchar=true)"),
    ("raw_fire",   "CREATE OR REPLACE TABLE raw_fire   AS SELECT * FROM read_csv('{raw}/fire_ems.csv', all_varchar=true)"),
    ("raw_311",    "CREATE OR REPLACE TABLE raw_311    AS SELECT * FROM read_csv('{raw}/311.csv', all_varchar=true)"),

    # ------------------------------------------------------------ staging
    # Police: collapse offense rows into one incident. Location fields are
    # identical across an incident's rows; categories can differ, so we keep
    # the primary one and a list of all.
    ("stg_police", r"""
    CREATE OR REPLACE TABLE stg_police AS
    SELECT
        incident_id,
        any_value(incident_number)                          AS incident_number,
        any_value(cad_number)                               AS cad_number,
        min(incident_datetime::TIMESTAMP)                   AS occurred_at,
        min(report_datetime::TIMESTAMP)                     AS reported_at,
        arg_min(incident_category, row_id::BIGINT)          AS category,
        arg_min(incident_subcategory, row_id::BIGINT)      AS subcategory,
        arg_min(incident_description, row_id::BIGINT)      AS description,
        list_sort(list_distinct(list(incident_category)))   AS all_categories,
        bool_or(report_type_description ILIKE 'Coplogic%')  AS filed_online,
        any_value(resolution)                               AS resolution,
        any_value(intersection)                             AS raw_address,
        any_value(cnn)                                      AS cnn,
        any_value(police_district)                          AS police_district,
        any_value(analysis_neighborhood)                    AS neighborhood,
        any_value(latitude::DOUBLE)                         AS lat,
        any_value(longitude::DOUBLE)                        AS lon,
        count(*)                                            AS source_rows
    FROM raw_police
    GROUP BY incident_id
    """),

    # Fire/EMS: collapse unit rows into one call. Response time = first unit
    # on scene minus call received, which is what accreditation reports use.
    ("stg_fire", r"""
    CREATE OR REPLACE TABLE stg_fire AS
    SELECT
        call_number,
        any_value(incident_number)                          AS incident_number,
        min(received_dttm::TIMESTAMP)                       AS occurred_at,
        min(dispatch_dttm::TIMESTAMP)                       AS first_dispatch_at,
        min(on_scene_dttm::TIMESTAMP)                       AS first_on_scene_at,
        any_value(call_type)                                AS call_type,
        any_value(call_type_group)                          AS call_type_group,
        any_value(call_final_disposition)                   AS disposition,
        min(final_priority)                                 AS final_priority,
        any_value(address)                                  AS raw_address,
        -- 'CALL BOX: 16TH ST/MISSION ST' -> '16TH ST/MISSION ST'; ',SF' tail dropped
        any_value(trim(regexp_replace(regexp_replace(address,
            '^CALL BOX:\s*', ''), ',\s*SF$', '')))         AS address_clean,
        any_value(address LIKE 'CALL BOX:%')                AS from_call_box,
        any_value(station_area)                             AS station_area,
        any_value(battalion)                                AS battalion,
        any_value(neighborhoods_analysis_boundaries)        AS neighborhood,
        any_value(regexp_extract(case_location, 'POINT \(\S+ (\S+)\)', 1)::DOUBLE) AS lat,
        any_value(regexp_extract(case_location, 'POINT \((\S+) ', 1)::DOUBLE)      AS lon,
        count(*)                                            AS units_dispatched,
        list_sort(list_distinct(list(unit_type)))           AS unit_types
    FROM raw_fire
    GROUP BY call_number
    """),

    # 311: already one row per request. Clean the address: drop the
    # ', SAN FRANCISCO, CA 94110, US' tail and the stray ', Virtual' token,
    # and classify the address shape so we know how to resolve it.
    ("stg_311", r"""
    CREATE OR REPLACE TABLE stg_311 AS
    WITH cleaned AS (
        SELECT *,
            trim(regexp_replace(regexp_replace(address,
                ',\s*Virtual', '', 'gi'),
                ',\s*SAN FRANCISCO.*$', '', 'i')) AS addr_clean
        FROM raw_311
    )
    SELECT
        service_request_id,
        requested_datetime::TIMESTAMP                       AS occurred_at,
        closed_date::TIMESTAMP                              AS closed_at,
        status_description                                  AS status,
        agency_responsible,
        service_name,
        service_subtype,
        service_details,
        address                                             AS raw_address,
        CASE WHEN addr_clean ILIKE 'INTERSECTION %' THEN 'intersection'
             WHEN regexp_matches(addr_clean, '^[0-9]+ ') THEN 'street_address'
             ELSE 'unusable' END                            AS address_shape,
        CASE WHEN addr_clean ILIKE 'INTERSECTION %'
             THEN regexp_replace(addr_clean, '^INTERSECTION\s+', '', 'i') END
                                                            AS intersection_text,
        analysis_neighborhood                               AS neighborhood,
        nullif(lat::DOUBLE, 0)                              AS lat,
        nullif(long::DOUBLE, 0)                             AS lon,
        source                                              AS intake_channel
    FROM cleaned
    """),

    # ----------------------------------------------------------- location
    # The gazetteer of intersections, built from every system that reports
    # at intersection grain. Three tables:
    #   location_key    one row per distinct intersection NAME seen anywhere
    #   location_alias  name -> canonical location. Where 3+ streets meet,
    #                   systems list different subsets ('GROVE ST & POLK ST'
    #                   vs 'DR CARLTON B GOODLETT PL & GROVE ST & POLK ST');
    #                   a name whose streets are a subset of a longer name
    #                   within 60 m is an alias of it.
    #   location        one row per real place (the ontology object)
    ("location_key", r"""
    CREATE OR REPLACE TABLE location_key AS
    WITH obs AS (
        SELECT intersection_key(raw_address, '\\') AS key, lat, lon, cnn, neighborhood, 'police' AS src
        FROM stg_police WHERE raw_address IS NOT NULL
        UNION ALL
        SELECT intersection_key(address_clean, '/'), lat, lon, NULL, neighborhood, 'fire_ems'
        FROM stg_fire WHERE address_clean LIKE '%/%'
        UNION ALL
        SELECT intersection_key(intersection_text, ','), lat, lon, NULL, neighborhood, '311'
        FROM stg_311 WHERE address_shape = 'intersection'
    )
    SELECT key, string_split(key, ' & ')           AS streets,
           median(lat) AS lat, median(lon) AS lon,
           mode(neighborhood)                       AS neighborhood,
           any_value(cnn) FILTER (cnn IS NOT NULL)  AS cnn,
           list_sort(list_distinct(list(src)))      AS seen_in_sources,
           count(*)                                 AS observations
    FROM obs
    WHERE key LIKE '% & %' AND lat IS NOT NULL
    GROUP BY key
    """),

    ("location_alias", r"""
    CREATE OR REPLACE TABLE location_alias AS
    WITH canon AS (
        SELECT a.key AS alias_key,
               coalesce(arg_max(b.key, len(b.streets)), a.key) AS canonical_key
        FROM location_key a
        LEFT JOIN location_key b
          ON len(b.streets) > len(a.streets)
         AND list_has_all(b.streets, a.streets)
         AND dist_m(a.lat, a.lon, b.lat, b.lon) <= 60
        GROUP BY a.key
    )
    SELECT alias_key, canonical_key, 'loc_' || md5(canonical_key)[:12] AS location_id
    FROM canon
    """),

    ("location", r"""
    CREATE OR REPLACE TABLE location AS
    SELECT a.location_id,
           a.canonical_key                                              AS label,
           list_sort(list(a.alias_key) FILTER (a.alias_key <> a.canonical_key)) AS aliases,
           any_value(c.lat)                                             AS lat,
           any_value(c.lon)                                             AS lon,
           any_value(c.neighborhood)                                    AS neighborhood,
           any_value(k.cnn) FILTER (k.cnn IS NOT NULL)                  AS cnn,
           list_sort(list_distinct(flatten(list(k.seen_in_sources))))   AS seen_in_sources,
           sum(k.observations)::BIGINT                                  AS observations
    FROM location_alias a
    JOIN location_key k ON k.key = a.alias_key
    JOIN location_key c ON c.key = a.canonical_key
    GROUP BY a.location_id, a.canonical_key
    """),

    # -------------------------------------------------------- event_base
    # Every source mapped onto ONE shape, plus match_key: the intersection
    # key we will try to look up (NULL when the address isn't an intersection).
    ("event_base", r"""
    CREATE OR REPLACE TABLE event_base AS
    SELECT 'police:' || incident_id AS event_id, 'police' AS source,
           incident_id AS source_record_id, occurred_at,
           category, subcategory, description, resolution AS status,
           raw_address, intersection_key(raw_address, '\\') AS match_key,
           neighborhood, lat, lon
    FROM stg_police
    UNION ALL
    SELECT 'fire_ems:' || call_number, 'fire_ems', call_number, occurred_at,
           call_type_group, call_type, disposition,
           CASE WHEN first_on_scene_at IS NOT NULL THEN 'unit on scene' ELSE 'no unit on scene' END,
           raw_address,
           CASE WHEN address_clean LIKE '%/%' THEN intersection_key(address_clean, '/') END,
           neighborhood, lat, lon
    FROM stg_fire
    UNION ALL
    SELECT '311:' || service_request_id, '311', service_request_id, occurred_at,
           service_name, service_subtype, service_details, status,
           raw_address,
           CASE WHEN address_shape = 'intersection' THEN intersection_key(intersection_text, ',') END,
           neighborhood, lat, lon
    FROM stg_311
    """),

    # -------------------------------------------------------- event_snap
    # Fallback for anything the name match missed but that has coordinates
    # (311 street addresses, fire call boxes, ...): nearest intersection
    # within SNAP_MAX_METERS. A coarse grid (~220 m cells) keeps this a cheap
    # join instead of 200k x 8k distance comparisons.
    ("event_snap", r"""
    CREATE OR REPLACE TABLE event_snap AS
    WITH loc AS (
        SELECT location_id, lat, lon,
               floor(lat / 0.002)::INT AS gy, floor(lon / 0.0025)::INT AS gx
        FROM location
    ), todo AS (
        SELECT e.event_id, e.lat, e.lon,
               floor(e.lat / 0.002)::INT AS gy, floor(e.lon / 0.0025)::INT AS gx
        FROM event_base e
        WHERE e.lat IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM location_alias a WHERE a.alias_key = e.match_key)
    )
    SELECT todo.event_id,
           arg_min(loc.location_id, dist_m(todo.lat, todo.lon, loc.lat, loc.lon)) AS location_id,
           min(dist_m(todo.lat, todo.lon, loc.lat, loc.lon))                      AS distance_m
    FROM todo JOIN loc
      ON loc.gy BETWEEN todo.gy - 1 AND todo.gy + 1
     AND loc.gx BETWEEN todo.gx - 1 AND todo.gx + 1
    GROUP BY todo.event_id
    HAVING min(dist_m(todo.lat, todo.lon, loc.lat, loc.lon)) <= {snap_max}
    """),

    # -------------------------------------------------------------- event
    # The unified spine. Every row keeps:
    #   source + source_record_id  -> provenance: you can always go back
    #   match_method + distance    -> how confident the location link is
    ("event", r"""
    CREATE OR REPLACE TABLE event AS
    SELECT
        b.event_id, b.source, b.source_record_id, b.occurred_at,
        b.category, b.subcategory, b.description, b.status, b.raw_address,
        coalesce(l.location_id, s.location_id)                     AS location_id,
        CASE WHEN l.location_id IS NOT NULL THEN 'exact_name'
             WHEN s.location_id IS NOT NULL THEN 'nearest_intersection'
        END                                                        AS match_method,
        CASE WHEN l.location_id IS NOT NULL THEN 0.0
             ELSE round(s.distance_m, 1) END                       AS match_distance_m,
        b.neighborhood, b.lat, b.lon
    FROM event_base b
    LEFT JOIN location_alias l ON l.alias_key = b.match_key
    LEFT JOIN event_snap s USING (event_id)
    """),
]


def build() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB_PATH))
    con.execute(MACROS)
    print(f"Building {DB_PATH}")
    for name, sql in STEPS:
        t = time.time()
        con.execute(sql.format(raw=RAW_DIR.as_posix(), snap_max=SNAP_MAX_METERS))
        n = con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
        print(f"  {name:<11} {n:>9,} rows  {time.time() - t:5.2f}s")
    con.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    build()
