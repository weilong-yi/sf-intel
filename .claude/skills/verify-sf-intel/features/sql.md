# SQL view

The SQL view lets a user run read-only queries against the unified database with the same guardrails as the
assistant: one SELECT or WITH statement, no writes or configuration, at most 500 rows shown. A schema
sidebar lists tables and columns, and an examples menu loads ready-made queries.

## Sub-features

- `sql-run` runs a SELECT and renders a result table with row count and timing.
- `sql-guard` rejects writes, multiple statements and config keywords with an error.
- `sql-examples` loads and runs a query from `Example queries…`.
- `sql-schema` lists tables and columns; clicking a column inserts its name.
- `sql-event-link` opens the event drawer from an `event_id` cell.

## How to get to it (user POV)

- Choose the `SQL` tab, edit the query, choose `Run query` or press Ctrl+Enter.
- Choose an entry in `Example queries…`.
- Open `?view=sql`.

## Driving it with vctl

Preconditions:

- Baseline preconditions hold.

- **Open.** Run `$B open ""`, `$B click --css 'button[role=tab][data-view=sql]'`, `$B wait --css '#schema details'`.
- **Run.** Run `$B fill --css '#sqlInput' --value 'SELECT source, count(*) AS n FROM event GROUP BY 1 ORDER BY 1'`, `$B click --css '#runSql'`, `$B wait --css '#sqlOut table'`. `$B text --css '#sqlOut'` shows `3 rows`, and the three sources with counts that equal the header chips (`$B text --css '#topMeta'`). Run `$B screenshot --name sql-run`.
- **Guard: write.** Run `$B fill --css '#sqlInput' --value 'DELETE FROM event'`, `$B click --css '#runSql'`, `$B wait --css '#sqlOut .err'`. The text is `ValueError: Only a single SELECT (or WITH ... SELECT) query is allowed.`
- **Guard: hidden write.** Run `$B fill --css '#sqlInput' --value 'SELECT 1; DROP TABLE event'`, `$B click --css '#runSql'`, `$B wait --css '#sqlOut .err' --text 'one statement'`.
- **Guard: config.** Run `$B fill --css '#sqlInput' --value "SELECT * FROM read_csv('C:/Windows/win.ini')"`, `$B click --css '#runSql'`, `$B wait --css '#sqlOut .err'`. The error comes from DuckDB (external access disabled), not a result table. Run `$B screenshot --name sql-guard`.
- **Unchanged.** Re-run **Run**; the counts are the same as before the guard attempts.
- **Example.** Run `$B select --css '#examples' --value 2` and `$B wait --css '#sqlOut table'`. The result has an `hour` column with 24 rows.
- **Event link.** Run `$B fill --css '#sqlInput' --value 'SELECT event_id FROM event LIMIT 3'`, `$B click --css '#runSql'`, `$B click --css '#sqlOut button.evlink'`, `$B wait --css '#drawer'`.

## Gotchas

- The textarea handles Tab itself (inserts two spaces), so `press --key Tab` does not move focus out.
- The server wraps queries as `SELECT * FROM (<sql>) LIMIT 501`; a trailing `;` is stripped, a second statement is rejected.
- Words inside single-quoted strings are ignored by the keyword check, so `WHERE category = 'Update'` is allowed.
- The database is opened read-only with external access disabled, so a write that slips past the keyword check still fails in DuckDB. Report which layer rejected it.
