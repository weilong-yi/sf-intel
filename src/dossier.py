"""Location dossier: everything every system knows about one place.

Two parts:
  resolve(text)        free-text place -> location_id (+ HOW it was resolved)
  build(location_id)   one structured profile of the AREA around it
                       (default 100 m), with event_ids as citations

Why an area and not just the intersection? Police publish locations
snapped to a nearby intersection, so the same corner can live under a
neighbouring key (16th & Mission's police activity sits at 16th & Wiese,
71 m away). Exact-key lookups silently undercount; a small, stated radius
is the honest default. radius_m=0 gives the strict single-intersection view.

Run:  python -m src.dossier "16th St & Mission St"
      python -m src.dossier "1 Dr Carlton B Goodlett Pl"
"""

import json
import re
import sys

import duckdb

DB_PATH = "data/sf_intel.duckdb"
AREA_METERS = 100
RECENT_EVENTS = 10


def connect() -> duckdb.DuckDBPyConnection:
    # Read-only: nothing downstream of the pipeline can change the data.
    return duckdb.connect(DB_PATH, read_only=True)


# ------------------------------------------------------------------ resolve
def resolve(con, text: str) -> dict:
    """Turn what a person types into a location_id. Returns the match and how
    it was made, or candidates when ambiguous, or an error."""
    text = text.strip()

    # 1. Intersection: "16th St & Mission", "16TH ST/MISSION ST", "X and Y"
    if re.search(r"&|/|\\|\band\b", text, re.I):
        key = con.execute(
            r"SELECT intersection_key(?, '\s*(?:&|/|\\|\b[Aa][Nn][Dd]\b)\s*')", [text]
        ).fetchone()[0]
        row = con.execute("""
            SELECT a.location_id, l.label FROM location_alias a
            JOIN location l USING (location_id) WHERE a.alias_key = ?""", [key]).fetchone()
        if row:
            return {"location_id": row[0], "label": row[1], "method": "exact intersection name"}
        # People drop suffixes ("16th & Mission"). Treat each typed street as
        # the start of a real street name: 16TH -> 16TH ST, MISSION -> MISSION ST.
        fuzzy = _match_without_suffix(con, key.split(" & "))
        if fuzzy:
            return fuzzy
        candidates = next((c for p in key.split(" & ") if (c := _candidates(con, p))), [])
        return {"error": f"No intersection named '{key}' in any source system.",
                "candidates": candidates}

    # 2. Street address: "500 Market St". 311 has ~200k geocoded addresses,
    #    so it doubles as our geocoder: find the same street, closest house
    #    number, then snap its coordinates to the nearest intersection.
    m = re.match(r"^\s*(\d+)\s+(.+)$", text)
    if m:
        number, street = int(m.group(1)), con.execute(
            "SELECT norm_street(?)", [m.group(2)]).fetchone()[0]
        hit = con.execute(r"""
            WITH a AS (
                SELECT regexp_extract(raw_address, '^(\d+) ', 1)::INT AS num,
                       norm_street(regexp_extract(raw_address, '^\d+ ([^,]+)', 1)) AS street,
                       lat, lon
                FROM stg_311
                WHERE address_shape = 'street_address' AND lat IS NOT NULL
            )
            SELECT num, median(lat), median(lon) FROM a
            WHERE street = ? GROUP BY num
            ORDER BY abs(num - ?) LIMIT 1""", [street, number]).fetchone()
        if not hit:
            return {"error": f"No geocoded address on '{street}'.",
                    "candidates": _candidates(con, street)}
        loc = con.execute("""
            SELECT location_id, label, dist_m(?, ?, lat, lon) d
            FROM location ORDER BY d LIMIT 1""", [hit[1], hit[2]]).fetchone()
        how = "exact address" if hit[0] == number else f"nearest house number on file ({hit[0]})"
        return {"location_id": loc[0], "label": loc[1],
                "method": f"geocoded via 311 ({how}), snapped {loc[2]:.0f} m to nearest intersection"}

    # 3. Anything else: a street name -> list busiest intersections on it
    return {"error": "Ambiguous: give an intersection or a street address.",
            "candidates": _candidates(con, text)}


def _match_without_suffix(con, parts: list[str]) -> dict | None:
    """Find intersections where every typed street is a full street name or the
    start of one (word boundary). Prefer the fewest extra streets, then the
    busiest place."""
    def covers(streets, part):
        return any(s == part or s.startswith(part + " ") for s in streets)

    rows = con.execute("""
        SELECT a.location_id, l.label, a.alias_key,
               (SELECT count(*) FROM event e WHERE e.location_id = a.location_id) AS n
        FROM location_alias a JOIN location l USING (location_id)""").fetchall()
    hits = [(len(alias.split(" & ")), -n, loc_id, label)
            for loc_id, label, alias, n in rows
            if all(covers(alias.split(" & "), p) for p in parts)]
    if not hits:
        return None
    hits.sort()
    _, _, loc_id, label = hits[0]
    others = len({h[2] for h in hits}) - 1
    note = f"; {others} other match(es)" if others else ""
    return {"location_id": loc_id, "label": label,
            "method": f"street names completed (e.g. 16TH -> 16TH ST){note}"}


def _candidates(con, street_text: str) -> list[str]:
    street = con.execute("SELECT norm_street(?)", [street_text]).fetchone()[0] or ""
    rows = con.execute("""
        SELECT l.label FROM location l JOIN event e USING (location_id)
        WHERE l.label LIKE '%' || ? || '%'
        GROUP BY l.label ORDER BY count(*) DESC LIMIT 5""", [street]).fetchall()
    return [r[0] for r in rows]


# -------------------------------------------------------------------- build
def build(con, location_id: str, radius_m: float = AREA_METERS) -> dict:
    def rows(sql, params=()):
        cur = con.execute(sql, list(params))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    loc = rows("""
        SELECT location_id, label, aliases, neighborhood, cnn AS city_street_network_id,
               seen_in_sources, round(lat, 6) lat, round(lon, 6) lon
        FROM location WHERE location_id = ?""", [location_id])
    if not loc:
        return {"error": f"Unknown location_id {location_id}"}

    # The area: this intersection plus every intersection within radius_m.
    # A temp table so every query below uses exactly the same set.
    con.execute("""
        CREATE OR REPLACE TEMP TABLE area AS
        WITH me AS (SELECT lat, lon FROM location WHERE location_id = ?)
        SELECT l.location_id, l.label, round(dist_m(me.lat, me.lon, l.lat, l.lon)) AS meters
        FROM location l, me
        WHERE dist_m(me.lat, me.lon, l.lat, l.lon) <= ?""", [location_id, radius_m])

    included = rows("""
        SELECT a.label, a.meters,
               count(e.event_id) FILTER (e.source = 'police')   AS police,
               count(e.event_id) FILTER (e.source = 'fire_ems') AS fire_ems,
               count(e.event_id) FILTER (e.source = '311')      AS "311"
        FROM area a LEFT JOIN event e USING (location_id)
        GROUP BY 1, 2 ORDER BY a.meters""")

    # Windows are anchored on the newest event, not today's date, so the
    # numbers are reproducible whenever you run this.
    data_through = rows("SELECT max(occurred_at)::DATE AS d FROM event")[0]["d"]

    activity = rows("""
        WITH bounds AS (SELECT max(occurred_at) AS t FROM event)
        SELECT source,
               count(*)                                                    AS events_90d,
               count(*) FILTER (occurred_at >  t - INTERVAL 30 DAY)        AS last_30d,
               round(count(*) FILTER (occurred_at <= t - INTERVAL 30 DAY) / 2.0, 1)
                                                                           AS prior_30d_avg,
               count(*) FILTER (match_method = 'nearest_intersection')     AS matched_by_distance
        FROM event, bounds WHERE location_id IN (SELECT location_id FROM area)
        GROUP BY source ORDER BY source""")

    top_categories = rows("""
        SELECT source, category, count(*) AS n FROM event
        WHERE location_id IN (SELECT location_id FROM area)
        GROUP BY 1, 2 QUALIFY row_number() OVER (PARTITION BY source ORDER BY n DESC) <= 4
        ORDER BY source, n DESC""")

    fire_response = rows("""
        SELECT count(*) AS calls_with_unit_on_scene,
               round(median(epoch(f.first_on_scene_at - f.occurred_at)) / 60, 1) AS median_minutes_to_scene,
               round(quantile_cont(epoch(f.first_on_scene_at - f.occurred_at), 0.9) / 60, 1) AS p90_minutes_to_scene
        FROM event e JOIN stg_fire f ON f.call_number = e.source_record_id
        WHERE e.location_id IN (SELECT location_id FROM area) AND e.source = 'fire_ems'
          AND f.first_on_scene_at > f.occurred_at""")[0]

    open_311 = rows("""
        SELECT s.service_name, count(*) AS open_requests,
               max(date_diff('day', s.occurred_at, (SELECT max(occurred_at) FROM event))) AS oldest_days
        FROM event e JOIN stg_311 s ON s.service_request_id = e.source_record_id
        WHERE e.location_id IN (SELECT location_id FROM area) AND e.source = '311'
          AND s.status = 'Open'
        GROUP BY 1 ORDER BY open_requests DESC LIMIT 5""")

    recent = rows(f"""
        SELECT event_id, strftime(occurred_at, '%Y-%m-%d %H:%M') AS occurred_at,
               source, category, subcategory, status
        FROM event WHERE location_id IN (SELECT location_id FROM area)
        ORDER BY occurred_at DESC LIMIT {RECENT_EVENTS}""")

    return {"location": loc[0], "radius_m": radius_m, "data_through": str(data_through),
            "intersections_in_area": included, "activity_by_source": activity,
            "top_categories": top_categories, "fire_ems_response": fire_response,
            "open_311_requests": open_311, "recent_events": recent}


# ---------------------------------------------------------------------- CLI
def print_dossier(d: dict) -> None:
    loc = d["location"]
    print(f"\n{'=' * 70}\n{loc['label']}  ({loc['neighborhood']})")
    print(f"location_id {loc['location_id']} | CNN {loc['city_street_network_id']} | "
          f"seen in: {', '.join(loc['seen_in_sources'])} | data through {d['data_through']}")
    if loc["aliases"]:
        print(f"also known as: {'; '.join(loc['aliases'])}")
    print("=" * 70)

    print(f"\nArea = {len(d['intersections_in_area'])} intersection(s) within {d['radius_m']:.0f} m"
          f"         police  fire_ems   311")
    for i in d["intersections_in_area"]:
        print(f"  {i['meters']:>4.0f} m  {i['label'][:42]:<42}{i['police']:>7}{i['fire_ems']:>9}{i['311']:>6}")

    print("\nActivity (90 days)         total  last30  prior30avg  matched-by-distance")
    for a in d["activity_by_source"]:
        trend = "↑" if a["last_30d"] > a["prior_30d_avg"] * 1.25 else \
                "↓" if a["last_30d"] < a["prior_30d_avg"] * 0.75 else " "
        print(f"  {a['source']:<24}{a['events_90d']:>7}{a['last_30d']:>8} {trend}"
              f"{a['prior_30d_avg']:>10}{a['matched_by_distance']:>16}")

    print("\nTop categories")
    for c in d["top_categories"]:
        print(f"  {c['source']:<10} {c['n']:>4}  {c['category']}")

    r = d["fire_ems_response"]
    if r["calls_with_unit_on_scene"]:
        print(f"\nFire/EMS response: median {r['median_minutes_to_scene']} min, "
              f"p90 {r['p90_minutes_to_scene']} min ({r['calls_with_unit_on_scene']} calls)")

    if d["open_311_requests"]:
        print("\nOpen 311 requests")
        for o in d["open_311_requests"]:
            print(f"  {o['open_requests']:>4}  {o['service_name']}  (oldest {o['oldest_days']} days)")

    print("\nMost recent events (cite by event_id)")
    for e in d["recent_events"]:
        print(f"  {e['occurred_at']}  {e['event_id']:<22} {e['category']} / {e['subcategory']}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    args = sys.argv[1:]
    radius = AREA_METERS
    if "--radius" in args:
        i = args.index("--radius")
        radius = float(args[i + 1])
        del args[i:i + 2]
    query = " ".join(args) or "16th St & Mission St"
    con = connect()
    match = resolve(con, query)
    if "error" in match:
        print(json.dumps(match, indent=2))
        return
    print(f"Resolved '{query}' -> {match['label']}  [{match['method']}]")
    print_dossier(build(con, match["location_id"], radius))


if __name__ == "__main__":
    main()
