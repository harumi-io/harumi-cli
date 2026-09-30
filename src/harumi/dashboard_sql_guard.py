"""Read-only enforcement for `[[metrics]]` SQL in a project's `dashboard.toml`.

A straight Python port of harumi-platform's `packages/ui/src/dashboard/sql-guard.ts`
— same rules, same masking algorithm, same messages where possible — because the
SQL runs client-side in DuckDB-WASM and this module never executes it; it only
gives the author (or the chat agent) the same rejection *before* a commit that
the platform would give at render time. See sql-guard.ts's module docstring for
the full rationale (blanked public share links, DuckDB's `LOAD httpfs` reach,
etc.) — not repeated here to avoid the two copies drifting in prose.

Unlike the widget contract, there's no generated JSON artifact for this — the
forbidden-keyword/function lists aren't exported by `dashboard-schema.json` —
so this file is a hand-ported second copy, not a consumer of a shared source.
`tests/test_dashboard_sql_guard.py` pins a same-behavior contract (a table of
queries and expected accept/reject, ported case-for-case from
`sql-guard.test.ts`) so a rule ported wrong fails a test here, but a
`sql-guard.ts` change that isn't ported at all is only caught by a human
reading the diff.

Not a substitute for DuckDB's own configuration were this ever executed — it
isn't, here — but the layer that can give a precise, author-facing message
without a DuckDB dependency in the CLI.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

# Scanning is linear but not free. Real metric queries are a few hundred
# characters; this only stops a spec from spending CPU on text no engine would
# accept.
MAX_SQL_CHARS = 100_000

# Statement openers that can only read. `FROM` because DuckDB's FROM-first
# shorthand (`FROM schedule WHERE ...`) is real and idiomatic; `DESCRIBE` /
# `SUMMARIZE` are read-only introspection an author reaches for while iterating.
_READ_ONLY_OPENERS = ("SELECT", "WITH", "FROM", "DESCRIBE", "SUMMARIZE")

# Rejected wherever they appear, not just at the start — a data-modifying CTE
# opens with an innocent WITH, and the interesting verbs here (ATTACH, COPY,
# INSTALL, LOAD, EXPORT, PRAGMA) aren't writes at all, they're how a query
# reaches outside the run output it's supposed to be summarizing.
_FORBIDDEN_KEYWORDS = (
    "INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE", "ALTER", "CREATE",
    "REPLACE", "MERGE", "UPSERT", "GRANT", "REVOKE",
    "ATTACH", "DETACH", "COPY", "INSTALL", "LOAD", "EXPORT", "IMPORT",
    "PRAGMA", "SET", "RESET", "CALL", "CHECKPOINT", "USE",
    "PREPARE", "EXECUTE", "DEALLOCATE",
)
_FORBIDDEN_KEYWORD_RE = re.compile(r"\b(" + "|".join(_FORBIDDEN_KEYWORDS) + r")\b", re.IGNORECASE)

# Table functions that take a path or URL — not keywords, so a plain SELECT
# calling one of these passes every check above without this.
_FORBIDDEN_FUNCTIONS = (
    "read_csv", "read_csv_auto", "read_parquet", "read_json", "read_json_auto",
    "read_json_objects", "read_ndjson", "read_ndjson_auto", "read_ndjson_objects",
    "read_text", "read_blob", "parquet_scan", "csv_scan", "iceberg_scan",
    "delta_scan", "postgres_scan", "postgres_query", "sqlite_scan", "sqlite_query",
    "mysql_scan", "mysql_query", "glob", "sniff_csv", "parquet_metadata", "parquet_schema",
    # Reach outside the run output too: spreadsheet/spatial readers, SQL-in-a-string,
    # and process/secret introspection.
    "read_xlsx", "st_read", "query", "query_table", "getenv", "duckdb_secrets",
)
_FORBIDDEN_FUNCTION_RE = re.compile(r"\b(" + "|".join(_FORBIDDEN_FUNCTIONS) + r")\s*\(", re.IGNORECASE)


class SqlGuardError(ValueError):
    """Raised when a `[[metrics]]` SQL string isn't a single read-only query."""


def _is_e_string_prefix(sql: str, i: int) -> bool:
    """Whether the quote at `sql[i]` opens an `E'...'` string.

    The `E` must be a token of its own. In `ESCAPE'\\'` the `E` ends a keyword,
    and treating that quote as an E-string made the backslash swallow the closing
    quote, hiding every later statement from the scanner.
    """
    if i == 0 or sql[i - 1] not in ("E", "e"):
        return False
    return i < 2 or not (sql[i - 2].isalnum() or sql[i - 2] in ("_", "$"))


def _mask_literals_and_comments(
    sql: str, quoted: Optional[List[Tuple[int, int, str, str]]] = None
) -> Tuple[str, Optional[str]]:
    """`sql` with every string literal and comment blanked to spaces of the same
    length (offsets preserved, newlines kept), plus the first delimiter that was
    never closed, if any.

    Mirrors `maskLiteralsAndComments` in sql-guard.ts, including its one
    security-relevant detail: a backslash is a *literal* character in a plain
    `'...'` string (only `''` escapes a quote) and an *escape* only inside an
    `E'...'` string — getting this wrong let `SELECT 'a\\' ; DROP TABLE t` mask
    as one harmless statement against a real DuckDB.

    When `quoted` is given, every closed quoted token is appended to it as
    `(start, end, quote_char, content)`, since masking erases exactly the text
    (`"read_csv"(...)`, `FROM 'https://...'`) the later checks need to see.
    """
    out: List[str] = []
    unterminated: Optional[str] = None
    i, n = 0, len(sql)

    def blank(frm: int, to: int) -> None:
        for j in range(frm, to):
            out.append("\n" if sql[j] == "\n" else " ")

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if ch == "-" and nxt == "-":
            stop = sql.find("\n", i)
            end = n if stop == -1 else stop
            blank(i, end)
            i = end
            continue

        if ch == "/" and nxt == "*":
            depth = 0
            closed = False
            while i < n:
                if sql[i] == "/" and sql[i + 1 : i + 2] == "*":
                    depth += 1
                    out.append("  ")
                    i += 2
                    continue
                if sql[i] == "*" and sql[i + 1 : i + 2] == "/":
                    depth -= 1
                    out.append("  ")
                    i += 2
                    if depth == 0:
                        closed = True
                        break
                    continue
                out.append("\n" if sql[i] == "\n" else " ")
                i += 1
            if not closed:
                unterminated = unterminated or "/* comment"
            continue

        if ch == "$":
            tag_end = sql.find("$", i + 1)
            inner = sql[i + 1 : tag_end] if tag_end != -1 else ""
            is_tag = tag_end != -1 and (tag_end == i + 1 or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", inner) is not None)
            if is_tag:
                tag = sql[i : tag_end + 1]
                close = sql.find(tag, tag_end + 1)
                if close == -1:
                    unterminated = unterminated or "dollar-quoted string"
                stop = n if close == -1 else close + len(tag)
                if quoted is not None and close != -1:
                    quoted.append((i, stop, "$", sql[tag_end + 1 : close]))
                blank(i, stop)
                i = stop
                continue

        if ch in ("'", '"', "`"):
            quote = ch
            # Only E'...' / e'...' opts into backslash escapes; a plain '...'
            # treats \ as a literal character.
            backslash_escapes = quote == "'" and _is_e_string_prefix(sql, i)
            out.append(" ")
            token_start = i
            i += 1
            closed = False
            while i < n:
                if backslash_escapes and sql[i] == "\\" and i + 1 < n:
                    out.append("  ")
                    i += 2
                    continue
                if sql[i] == quote:
                    if sql[i + 1 : i + 2] == quote:
                        out.append("  ")
                        i += 2
                        continue
                    out.append(" ")
                    if quoted is not None:
                        quoted.append((token_start, i + 1, quote, sql[token_start + 1 : i]))
                    i += 1
                    closed = True
                    break
                out.append("\n" if sql[i] == "\n" else " ")
                i += 1
            if not closed:
                unterminated = unterminated or f"{quote} quoted string"
            continue

        out.append(ch)
        i += 1

    return "".join(out), unterminated


def _split_statements(masked: str) -> List[Tuple[int, int]]:
    """Spans of the non-empty statements in `masked`, split on real semicolons."""
    spans: List[Tuple[int, int]] = []
    start = 0
    for idx, ch in enumerate(masked):
        if ch == ";":
            if masked[start:idx].strip():
                spans.append((start, idx))
            start = idx + 1
    if masked[start:].strip():
        spans.append((start, len(masked)))
    return spans


_FROM_OR_JOIN_RE = re.compile(r"\b(FROM|JOIN)\s*$", re.IGNORECASE)
_IS_DISTINCT_FROM_RE = re.compile(r"\bDISTINCT\s+FROM\s*$", re.IGNORECASE)
# After a comma or a dot a quoted token may still be a table (`FROM t, 'x.csv'`,
# `FROM main."/etc/hosts"`), but only inside a FROM clause: in a select or IN list
# the same text is an ordinary value or column (`SELECT t."report.json"`).
_TABLE_SEPARATOR_RE = re.compile(r"[,.]\s*$")
_CLAUSE_KEYWORD_RE = re.compile(
    r"\b(SELECT|FROM|WHERE|GROUP|ORDER|HAVING|WINDOW|QUALIFY|LIMIT|ON|USING|JOIN)\b", re.IGNORECASE
)
# A name DuckDB's replacement scan would open as a file or URL.
_FILE_LIKE_RE = re.compile(
    r"[/\\:]|\.(csv|tsv|parquet|json|jsonl|ndjson|txt|xlsx|gz|zst|db|duckdb|sqlite|arrow|avro)$", re.IGNORECASE
)
# Stricter, for a token after a comma or a dot, where an ordinary column name or
# value (`'N/A'`, `'12:30'`) is far more likely: only a URL, a rooted or relative
# path, or a data-file extension.
_STRICT_FILE_LIKE_RE = re.compile(
    r"^[a-z][a-z0-9+.-]*://|^\.{0,2}/|\.(csv|tsv|parquet|json|jsonl|ndjson|txt|xlsx|gz|zst|db|duckdb|sqlite|arrow|avro)$", re.IGNORECASE
)


def _in_from_clause(before: str) -> bool:
    """Whether the innermost clause keyword before this point is FROM or JOIN.

    Parenthesized groups that have already closed are skipped, so a subselect
    (`FROM (SELECT 1) a, 'x.csv'`) does not hide the FROM around it.
    """
    levels: List[str] = [""]
    for ch in before:
        if ch == "(":
            levels.append("")
        elif ch == ")" and len(levels) > 1:
            levels.pop()
        else:
            levels[-1] += ch
    keywords = _CLAUSE_KEYWORD_RE.findall(levels[-1])
    return bool(keywords) and keywords[-1].upper() in ("FROM", "JOIN")


def _quoted_source_hazard(masked: str, quoted: List[Tuple[int, int, str, str]]) -> Optional[str]:
    """The first quoted token that names an outside source or hides a forbidden function.

    Masking blanks quoted text, so two ways in slip past the keyword and
    function checks. A quoted function name (`"read_csv"('https://...')`) is
    still the function. And DuckDB reads a string or identifier in table
    position as a file or URL (`FROM 'https://...'`, `FROM "/etc/hosts"`,
    `FROM t, 'x.csv'`, `FROM main."x.csv"`). Table position is approximated
    from the token before: this is a denylist, so the engine setting that turns
    external access off is the real boundary where there is an engine.
    """
    for start, end, quote, content in quoted:
        if quote in ('"', "`") and re.match(r"\s*\(", masked[end:]) and content.lower() in _FORBIDDEN_FUNCTIONS:
            return content
        before = masked[:start]
        file_like = _FILE_LIKE_RE.search(content) is not None
        if _FROM_OR_JOIN_RE.search(before):
            # Only a file-shaped name is a read: `substring(c FROM 'regex')` and
            # `EXTRACT(year FROM '2024-01-01'::date)` put a string here too, and
            # `a IS DISTINCT FROM 'x'` is a comparison.
            if file_like and not _IS_DISTINCT_FROM_RE.search(before):
                return content
        elif _TABLE_SEPARATOR_RE.search(before) and _STRICT_FILE_LIKE_RE.search(content) and _in_from_clause(before):
            return content
    return None


def ensure_read_only_select(sql: str, *, max_chars: int = MAX_SQL_CHARS) -> None:
    """Raises `SqlGuardError` unless `sql` is exactly one read-only statement.

    Mirrors `ensureReadOnlySelect` in sql-guard.ts: rejects, in order, a query
    too large to scan, an unclosed quote or comment, an empty query, more than
    one statement, anything whose first real keyword isn't SELECT/WITH/FROM/
    DESCRIBE/SUMMARIZE, any forbidden keyword, and any path- or URL-taking table
    function.
    """
    if not isinstance(sql, str):
        raise SqlGuardError(f"Query must be a string, got {'null' if sql is None else type(sql).__name__}.")

    if len(sql) > max_chars:
        raise SqlGuardError(f"Query is too large to validate ({len(sql)} characters, limit {max_chars}).")

    quoted: List[Tuple[int, int, str, str]] = []
    masked, unterminated = _mask_literals_and_comments(sql, quoted)
    if unterminated:
        raise SqlGuardError(f"Query has an unterminated {unterminated}.")

    spans = _split_statements(masked)
    if not spans:
        raise SqlGuardError("Query is empty.")
    if len(spans) > 1:
        raise SqlGuardError(f"Only a single SELECT statement is allowed. Found {len(spans)} statements separated by ';'.")

    start, end = spans[0]
    statement = masked[start:end]
    opener = statement.strip().split(None, 1)[0].upper() if statement.strip() else ""
    opener = opener.lstrip("(") or opener

    if not any(opener.startswith(word) for word in _READ_ONLY_OPENERS):
        raise SqlGuardError(
            "Only read-only queries are allowed. A metric query must start with "
            f'SELECT, WITH, or FROM; this one starts with "{opener[:20]}".'
        )

    keyword = _FORBIDDEN_KEYWORD_RE.search(statement)
    if keyword:
        raise SqlGuardError(
            f'Only SELECT queries are allowed. "{keyword.group(1).upper()}" is not permitted in a dashboard metric.'
        )

    fn = _FORBIDDEN_FUNCTION_RE.search(statement)
    if fn:
        raise SqlGuardError(
            f'"{fn.group(1)}" is not permitted in a dashboard metric — it reads a path or URL '
            "outside the run output. Query the datasets declared in [[datasets]] instead; "
            "for outside data, connect it as a datasource."
        )

    hazard = _quoted_source_hazard(
        masked[start:end], [(a - start, b - start, q, c) for a, b, q, c in quoted if start <= a < end]
    )
    if hazard is not None:
        raise SqlGuardError(
            f'"{hazard[:60]}" is not permitted in a dashboard metric — it reads a path or URL '
            "outside the run output. Query the datasets declared in [[datasets]] instead; "
            "for outside data, connect it as a datasource."
        )
