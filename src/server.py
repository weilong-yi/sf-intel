"""Local web UI for the database and the assistant. Standard library only.

Run:  python -m src.server          then open http://127.0.0.1:8765

Endpoints (all JSON):
  GET  /api/overview                 counts, data window, match rates, busiest places
  GET  /api/schema                   tables and columns (for the SQL view)
  GET  /api/profile?place=..&radius= location profile (same as src/dossier.py)
  GET  /api/event?id=police:123      one event + its source records (provenance)
  POST /api/sql      {sql}           read-only query, same guardrails as the agent
  POST /api/ask      {question, conversation_id}
                                     runs the agent; streams one JSON event per line
  POST /api/reset    {conversation_id}

Only listens on 127.0.0.1, so nothing is reachable from other machines.
"""

import json
import os
import sys
import threading
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import duckdb

from src import dossier
from src.assistant import Assistant, connect, query_rows, schema_info

HOST, PORT = "127.0.0.1", int(os.getenv("SF_INTEL_PORT", "8765"))
UI_FILE = Path(__file__).parent / "ui" / "index.html"

# One guarded DuckDB instance for the whole process; each request gets its own
# cursor (DuckDB connections are not shared across threads, cursors are cheap).
BASE = connect()
CONVERSATIONS: dict[str, Assistant] = {}
CONV_LOCK = threading.Lock()

SOURCE_TABLES = {  # event source -> (staging table, key column, raw table, raw key column)
    "police": ("stg_police", "incident_id", "raw_police", "incident_id"),
    "fire_ems": ("stg_fire", "call_number", "raw_fire", "call_number"),
    "311": ("stg_311", "service_request_id", "raw_311", "service_request_id"),
}


def rows(cur, sql, params=()):
    c = cur.execute(sql, list(params))
    cols = [d[0] for d in c.description]
    return [dict(zip(cols, r)) for r in c.fetchall()]


# ------------------------------------------------------------------ handlers
def overview(cur):
    return {
        "window": rows(cur, "SELECT min(occurred_at)::DATE AS start, max(occurred_at)::DATE AS end FROM event")[0],
        "sources": rows(cur, """
            SELECT source, count(*) AS events,
                   round(100.0 * count(*) FILTER (match_method = 'exact_name') / count(*), 1) AS exact_pct,
                   round(100.0 * count(*) FILTER (match_method = 'nearest_intersection') / count(*), 1) AS nearest_pct,
                   round(100.0 * count(*) FILTER (location_id IS NULL) / count(*), 1) AS unmatched_pct
            FROM event GROUP BY source ORDER BY source"""),
        "locations": rows(cur, """
            SELECT count(*) AS places,
                   count(*) FILTER (len(seen_in_sources) = 3) AS in_all_three FROM location""")[0],
        "busiest": rows(cur, """
            SELECT l.label, l.neighborhood,
                   count(*) FILTER (e.source = 'police')   AS police,
                   count(*) FILTER (e.source = 'fire_ems') AS fire_ems,
                   count(*) FILTER (e.source = '311')      AS s311,
                   count(*) AS total
            FROM event e JOIN location l USING (location_id)
            GROUP BY 1, 2 ORDER BY total DESC LIMIT 12"""),
    }


def profile(cur, place, radius):
    match = dossier.resolve(cur, place)
    if "error" in match:
        return 404, match
    data = dossier.build(cur, match["location_id"], max(0.0, min(radius, 500.0)))
    data["resolved"] = {"input": place, "matched": match["label"], "method": match["method"]}
    return 200, data


def event_detail(cur, event_id):
    found = rows(cur, "SELECT * FROM event WHERE event_id = ?", [event_id])
    if not found:
        return 404, {"error": f"No event {event_id}"}
    ev = found[0]
    stg, key, raw, raw_key = SOURCE_TABLES[ev["source"]]
    staged = rows(cur, f"SELECT * FROM {stg} WHERE {key} = ?", [ev["source_record_id"]])
    raw_rows = rows(cur, f"SELECT * FROM {raw} WHERE {raw_key} = ? LIMIT 25", [ev["source_record_id"]])
    loc = rows(cur, "SELECT location_id, label, aliases, neighborhood FROM location WHERE location_id = ?",
               [ev["location_id"]]) if ev["location_id"] else []
    return 200, {"event": ev, "staged": staged[0] if staged else None,
                 "raw_rows": raw_rows, "raw_table": raw, "location": loc[0] if loc else None}


# ------------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    server_version = "sf-intel/0.1"

    def log_message(self, fmt, *args):  # quieter console: one line per API call
        if self.path.startswith("/api/"):
            sys.stderr.write(f"  {self.command} {self.path.split('?')[0]} {args[1] if len(args) > 1 else ''}\n")

    def _json(self, status, payload):
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        try:
            if url.path in ("/", "/index.html"):
                body = UI_FILE.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            cur = BASE.cursor()
            if url.path == "/api/overview":
                return self._json(200, overview(cur))
            if url.path == "/api/schema":
                return self._json(200, schema_info(cur))
            if url.path == "/api/profile":
                return self._json(*profile(cur, q.get("place", ""), float(q.get("radius", 100))))
            if url.path == "/api/event":
                return self._json(*event_detail(cur, q.get("id", "")))
            self._json(404, {"error": "Not found"})
        except (ValueError, duckdb.Error) as e:
            self._json(400, {"error": f"{type(e).__name__}: {e}"})
        except Exception as e:
            traceback.print_exc()
            self._json(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        try:
            body = self._body()
            if self.path == "/api/sql":
                result = query_rows(BASE.cursor(), body.get("sql", ""), limit=500)
                return self._json(200, result)
            if self.path == "/api/reset":
                with CONV_LOCK:
                    CONVERSATIONS.pop(body.get("conversation_id", ""), None)
                return self._json(200, {"ok": True})
            if self.path == "/api/ask":
                return self._ask(body)
            self._json(404, {"error": "Not found"})
        except (ValueError, duckdb.Error) as e:
            self._json(400, {"error": f"{type(e).__name__}: {e}"})
        except Exception as e:
            traceback.print_exc()
            self._json(500, {"error": f"{type(e).__name__}: {e}"})

    def _ask(self, body):
        question = (body.get("question") or "").strip()
        conv_id = body.get("conversation_id") or "default"
        if not question:
            return self._json(400, {"error": "Empty question"})
        with CONV_LOCK:
            bot = CONVERSATIONS.get(conv_id)
            if bot is None:
                bot = CONVERSATIONS[conv_id] = Assistant(BASE.cursor())

        # Stream newline-delimited JSON: the browser renders each step as it happens.
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

        def emit(ev):
            self.wfile.write((json.dumps(ev, default=str) + "\n").encode("utf-8"))
            self.wfile.flush()

        try:
            bot.ask(question, on_event=emit)
        except Exception as e:  # API errors, auth errors: show them in the chat
            traceback.print_exc()
            emit({"type": "error", "message": f"{type(e).__name__}: {e}"})
        self.close_connection = True


def open_browser(url):
    # Prefer Chrome if installed; otherwise the system default browser.
    for path in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                 r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                 os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")):
        if os.path.exists(path):
            webbrowser.register("chrome", None, webbrowser.BackgroundBrowser(path))
            return webbrowser.get("chrome").open(url)
    return webbrowser.open(url)


def main():
    url = f"http://{HOST}:{PORT}"
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"SF Intel UI running at {url}  (Ctrl+C to stop)")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.6, open_browser, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
