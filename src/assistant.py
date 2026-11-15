"""The AI assistant: a hand-written tool-use loop over the unified database.

The loop (this is all an "agent" is):
  1. send the question + tool definitions to Claude
  2. if Claude asks for a tool, run it HERE, in our code, and send the result back
  3. repeat until Claude answers in plain text, or MAX_STEPS is hit

Governance, which is the point of the demo:
  - the model never touches the database directly; it can only ask us to run
    a query, and we decide what is allowed
  - the DuckDB connection is read-only with file/network access disabled
  - every question, query and answer is appended to data/audit_log.jsonl

Run:  python -m src.assistant "What changed at 16th & Mission in the last 30 days?"
      python -m src.assistant          (interactive; follow-up questions keep context)
"""

import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

import anthropic
import duckdb
from dotenv import load_dotenv

from src import dossier
from src.prompts import SYSTEM_PROMPT, TOOLS

MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
EFFORT = os.getenv("CLAUDE_EFFORT", "medium")  # low | medium | high | xhigh | max
MAX_STEPS = 8          # hard cap on model calls per question: no runaway loops
MAX_TOKENS = 16000
ROW_LIMIT = 200        # rows returned to the model per query
CELL_CHARS = 120       # long text cells are truncated
AUDIT_LOG = Path("data/audit_log.jsonl")
SCHEMA_TABLES = ["event", "location", "location_alias", "stg_police", "stg_fire", "stg_311"]
PRICE_PER_MTOK = {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00}  # Opus 5.5

DIM, BOLD, CYAN, YELLOW, RESET = "\033[2m", "\033[1m", "\033[36m", "\033[33m", "\033[0m"


# ------------------------------------------------------------------ database
def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(dossier.DB_PATH, read_only=True)
    # Read-only stops writes; this stops read_csv('C:/anything'), http, etc.
    # lock_configuration stops a query from switching it back on.
    con.execute("SET enable_external_access = false")
    con.execute("SET lock_configuration = true")
    return con


def describe_schema(con) -> str:
    lines = []
    for table in SCHEMA_TABLES:
        cols = con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = ? ORDER BY ordinal_position", [table]).fetchall()
        n = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        lines.append(f"{table} ({n:,} rows): " + ", ".join(f"{c} {t}" for c, t in cols))
    return "\n".join(lines)


# --------------------------------------------------------------------- tools
FORBIDDEN = re.compile(
    r"\b(insert|update|delete|create|drop|alter|attach|detach|copy|export|import|"
    r"install|load|pragma|set|call|checkpoint)\b", re.I)


def run_sql(con, sql: str) -> str:
    sql = sql.strip().rstrip(";").strip()
    if not re.match(r"^(select|with)\b", sql, re.I):
        raise ValueError("Only a single SELECT (or WITH ... SELECT) query is allowed.")
    if ";" in sql:
        raise ValueError("Only one statement per call.")
    if FORBIDDEN.search(re.sub(r"'[^']*'", "''", sql)):  # ignore words inside string literals
        raise ValueError("Query contains a forbidden keyword (writes/config are not allowed).")

    cur = con.execute(f"SELECT * FROM ({sql}) LIMIT {ROW_LIMIT + 1}")
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    truncated = len(rows) > ROW_LIMIT
    rows = rows[:ROW_LIMIT]

    def cell(v):
        s = "" if v is None else str(v)
        return s if len(s) <= CELL_CHARS else s[:CELL_CHARS] + "…"

    out = [" | ".join(cols)] + [" | ".join(cell(v) for v in r) for r in rows]
    footer = f"({len(rows)} rows{', TRUNCATED at limit - aggregate further' if truncated else ''})"
    return "\n".join(out + [footer])


def location_profile(con, place: str, radius_m: int) -> str:
    match = dossier.resolve(con, place)
    if "error" in match:
        return json.dumps(match)
    profile = dossier.build(con, match["location_id"], max(0, min(radius_m, 500)))
    profile["resolved"] = {"input": place, "matched": match["label"], "method": match["method"]}
    return json.dumps(profile, default=str)


def execute_tool(con, name: str, args: dict) -> tuple[str, bool]:
    """Returns (result_text, is_error). Errors go back to the model as tool
    results so it can fix its query, instead of crashing the loop."""
    try:
        if name == "run_sql":
            return run_sql(con, args["sql"]), False
        if name == "location_profile":
            return location_profile(con, args["place"], args["radius_m"]), False
        return f"Unknown tool: {name}", True
    except (ValueError, KeyError, duckdb.Error) as e:
        return f"Error: {type(e).__name__}: {e}", True


# ---------------------------------------------------------------------- loop
class Assistant:
    def __init__(self):
        load_dotenv()
        self.client = anthropic.Anthropic()
        self.con = connect()
        self.system = SYSTEM_PROMPT.format(schema=describe_schema(self.con), row_limit=ROW_LIMIT)
        self.messages: list = []  # whole conversation, so follow-ups keep context
        self.usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}

    def ask(self, question: str) -> str:
        audit = {"ts": dt.datetime.now().isoformat(timespec="seconds"), "model": MODEL,
                 "question": question, "tool_calls": []}
        self.messages.append({"role": "user", "content": question})
        answer = None

        for step in range(1, MAX_STEPS + 1):
            t = time.time()
            response = self.client.beta.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=self.system,
                tools=TOOLS,
                messages=self.messages,
                output_config={"effort": EFFORT},
                cache_control={"type": "ephemeral"},  # system + tools + history reused across steps
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",  # if the model declines, the API retries on a fallback model
            )
            self._track(response.usage)
            # Keep the full content (incl. thinking blocks) so the next call has the whole turn
            self.messages.append({"role": "assistant", "content": response.content})
            print(f"{DIM}[step {step}] {response.stop_reason} · {time.time() - t:.1f}s{RESET}")

            if response.stop_reason == "refusal":
                answer = "The model declined to answer this request."
                break
            if response.stop_reason == "max_tokens":
                answer = "Stopped: response hit the max_tokens limit."
                break

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                answer = "\n".join(b.text for b in response.content if b.type == "text")
                break

            results = []
            for tu in tool_uses:
                self._print_call(tu.name, tu.input)
                output, is_error = execute_tool(self.con, tu.name, tu.input)
                self._print_result(output, is_error)
                audit["tool_calls"].append({"tool": tu.name, "input": tu.input, "is_error": is_error,
                                            "result_preview": output[:300]})
                results.append({"type": "tool_result", "tool_use_id": tu.id,
                                "content": output, "is_error": is_error})
            # All results for this step go back in ONE user message
            self.messages.append({"role": "user", "content": results})
        else:
            answer = f"Stopped after {MAX_STEPS} steps without a final answer."

        audit["answer"] = answer
        audit["usage_so_far"] = dict(self.usage)
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(audit, default=str) + "\n")
        return answer

    # -- display helpers
    def _track(self, u):
        self.usage["input"] += u.input_tokens
        self.usage["output"] += u.output_tokens
        self.usage["cache_read"] += u.cache_read_input_tokens or 0
        self.usage["cache_write"] += u.cache_creation_input_tokens or 0

    def cost(self) -> float:
        return sum(self.usage[k] * PRICE_PER_MTOK[k] for k in self.usage) / 1e6

    @staticmethod
    def _print_call(name, args):
        if name == "run_sql":
            print(f"{CYAN}→ run_sql: {args.get('purpose', '')}{RESET}")
            for line in args.get("sql", "").strip().splitlines():
                print(f"{DIM}    {line}{RESET}")
        else:
            print(f"{CYAN}→ {name}({json.dumps(args)}){RESET}")

    @staticmethod
    def _print_result(output, is_error):
        lines = output.splitlines()
        color = YELLOW if is_error else DIM
        preview = lines[:8] if not output.startswith("{") else [output[:300] + "…"]
        for line in preview:
            print(f"{color}    ← {line[:160]}{RESET}")
        if len(lines) > 8:
            print(f"{DIM}    ← … {len(lines) - 8} more lines{RESET}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    os.system("")  # enables ANSI colours in the Windows console
    bot = Assistant()
    print(f"{DIM}model={MODEL} effort={EFFORT} max_steps={MAX_STEPS}{RESET}")
    questions = [" ".join(sys.argv[1:])] if len(sys.argv) > 1 else None

    while True:
        q = questions.pop(0) if questions else (None if questions is not None else input(f"\n{BOLD}ask> {RESET}").strip())
        if not q or q in {"exit", "quit"}:
            break
        print(f"\n{BOLD}Q: {q}{RESET}")
        print(f"\n{BOLD}A:{RESET} {bot.ask(q)}")
        print(f"{DIM}[tokens in={bot.usage['input']} cache_read={bot.usage['cache_read']} "
              f"cache_write={bot.usage['cache_write']} out={bot.usage['output']} · ~${bot.cost():.3f}]{RESET}")
        if questions is not None and not questions:
            break


if __name__ == "__main__":
    main()
