"""Table-driven accept/reject contract for `dashboard_sql_guard.py`.

A straight Python port of harumi-platform's `sql-guard.test.ts` — same cases,
same intent — so a rule ported wrong from `sql-guard.ts` (the backslash-escape
edge case, a missed forbidden function, dollar-quote handling, ...) fails here
instead of only being caught by a human reading the diff. See
`dashboard_sql_guard.py`'s module docstring for why this file, not
`test_dashboard.py`'s two indirect cases through `parse_metric_entry`, is the
thing that actually pins this module's behavior.
"""

import pytest

from harumi.dashboard_sql_guard import MAX_SQL_CHARS, SqlGuardError, ensure_read_only_select


def _rejects(sql):
    try:
        ensure_read_only_select(sql)
    except SqlGuardError as exc:
        return str(exc)
    return None


ACCEPTED = [
    "SELECT count(*) FROM schedule",
    "select MAX(end_h) - MIN(start_h) as makespan from schedule",
    "WITH ordered AS (SELECT * FROM setups ORDER BY at) SELECT last(paint) FROM ordered",
    "(SELECT 1) UNION ALL (SELECT 2)",
    "SELECT * FROM schedule WHERE start_h <= 12 AND end_h > 12",
    # A CASE expression reads like a keyword salad but is an ordinary read.
    "SELECT CASE WHEN qty > 0 THEN 'yes' ELSE 'no' END FROM rows",
    "SELECT * FROM schedule -- only the bars that matter\n",
    "SELECT /* inline */ count(*) /* note */ FROM t",
    "SELECT * FROM t ORDER BY resource LIMIT 100",
    # Trailing semicolon on a single statement is fine.
    "SELECT 1;",
    "SELECT 1;   \n  ",
    # DuckDB's FROM-first shorthand is real and verified working.
    "FROM schedule",
    "FROM schedule WHERE start_h <= 12",
    "FROM (SELECT 1 AS a)",
    # Read-only introspection an author reaches for while iterating.
    "DESCRIBE SELECT 1",
    "SUMMARIZE SELECT * FROM schedule",
]


@pytest.mark.parametrize("sql", ACCEPTED)
def test_accepts_real_metric_queries(sql):
    assert _rejects(sql) is None


def test_allows_a_forbidden_word_inside_a_string_literal():
    # The whole reason literals are masked before scanning: a raw-keyword scan
    # rejects honest SQL whose *data* mentions one of these words.
    assert _rejects("SELECT * FROM t WHERE note = 'create the order'") is None
    assert _rejects("SELECT 'drop table' AS label") is None
    assert _rejects("SELECT * FROM t WHERE msg = 'read_parquet(1)'") is None


def test_allows_a_forbidden_word_inside_a_quoted_identifier():
    assert _rejects('SELECT "load" FROM t') is None
    assert _rejects('SELECT t."set" FROM t') is None


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT created_at FROM t",
        "SELECT dropped_count FROM t",
        "SELECT setup_h FROM t",
        "SELECT offset_minutes FROM t",
        "SELECT preloaded FROM t",
    ],
)
def test_allows_a_column_whose_name_merely_contains_a_forbidden_word(sql):
    # Word-boundary matching, not substring: `created_at` is not `CREATE`.
    assert _rejects(sql) is None


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("DELETE FROM schedule", "start with SELECT"),
        ("DROP TABLE schedule", "start with SELECT"),
        ("CREATE TABLE t AS SELECT 1", "start with SELECT"),
        ("UPDATE t SET x = 1", "start with SELECT"),
        ("INSERT INTO t VALUES (1)", "start with SELECT"),
        # Data-modifying CTE: opens with WITH, so a start-of-statement check
        # alone would wave this through.
        ("WITH x AS (SELECT 1) DELETE FROM t", "DELETE"),
        ("WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x", "INSERT"),
    ],
)
def test_blocks_writes(sql, expected):
    message = _rejects(sql)
    assert message is not None and expected in message


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("SELECT * FROM read_parquet('https://evil.test/x.parquet')", "read_parquet"),
        ("SELECT * FROM read_csv('https://evil.test/x.csv')", "read_csv"),
        ("SELECT * FROM read_json_auto('https://evil.test/x.json')", "read_json_auto"),
        ("SELECT * FROM parquet_scan('s3://bucket/x.parquet')", "parquet_scan"),
        ("SELECT * FROM glob('/etc/*')", "glob"),
        ("SELECT * FROM read_text('/etc/passwd')", "read_text"),
        ("SELECT * FROM postgres_scan('host=…', 'public', 't')", "postgres_scan"),
        # Whitespace between name and paren doesn't help.
        ("SELECT * FROM read_parquet  ('https://evil.test/x')", "read_parquet"),
        ("SELECT * FROM read_parquet\n('https://evil.test/x')", "read_parquet"),
        ("SELECT * FROM READ_PARQUET('https://evil.test/x')", "READ_PARQUET"),
    ],
)
def test_blocks_path_or_url_taking_table_functions(sql, expected):
    message = _rejects(sql)
    assert message is not None and expected in message


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("ATTACH 'other.db' AS other", "start with SELECT"),
        ("COPY t TO '/tmp/out.csv'", "start with SELECT"),
        ("INSTALL httpfs", "start with SELECT"),
        ("LOAD httpfs", "start with SELECT"),
        ("SET allow_unsigned_extensions = true", "start with SELECT"),
        ("PRAGMA disable_verification", "start with SELECT"),
        # Chained after a legitimate read — this is the shape a keyword-at-start
        # check misses entirely.
        ("SELECT 1; ATTACH 'x.db' AS x", "single SELECT statement"),
        ("SELECT 1; LOAD httpfs", "single SELECT statement"),
        ("WITH x AS (SELECT 1) SELECT * FROM x; COPY x TO '/tmp/o'", "single SELECT"),
    ],
)
def test_blocks_reaching_outside_the_run_output(sql, expected):
    message = _rejects(sql)
    assert message is not None and expected in message


def test_a_keyword_split_by_a_block_comment():
    # The canonical failure of scanning the raw string: `DR/**/OP` contains no
    # `DROP` substring, but DuckDB reads it as one token boundary apart.
    # Masking turns it into `DR  OP`, and the statement split still sees two
    # statements — which is what actually stops it.
    message = _rejects("SELECT 1; DR/**/OP TABLE t")
    assert message is not None and "single SELECT statement" in message


def test_a_semicolon_hidden_in_a_comment_does_not_split_a_statement():
    assert _rejects("SELECT 1 -- ; DROP TABLE t") is None
    assert _rejects("SELECT 1 /* ; DROP TABLE t */") is None


def test_a_forbidden_keyword_hidden_in_a_comment_is_not_a_false_positive():
    assert _rejects("SELECT 1 -- we could DROP this later") is None


def test_a_forbidden_keyword_after_a_comment_is_still_caught():
    message = _rejects("SELECT 1; /* pause */ DROP TABLE t")
    assert message is not None and "single SELECT" in message


@pytest.mark.parametrize(
    "sql,expected",
    [
        # Unclosed delimiters must be rejected, not analysed: masking one
        # swallows the rest of the query, including any `;`, which would make
        # the statement count read as 1.
        ("SELECT $$ ; DROP TABLE t", "unterminated dollar-quoted string"),
        ("SELECT ' ; DROP TABLE t", "unterminated ' quoted string"),
        ('SELECT " ; DROP TABLE t', 'unterminated " quoted string'),
        ("SELECT 1 /* ; DROP TABLE t", "unterminated /* comment"),
    ],
)
def test_unclosed_delimiters_are_rejected(sql, expected):
    message = _rejects(sql)
    assert message is not None and expected in message


def test_a_backslash_before_the_closing_quote_of_a_plain_string_does_not_escape_it():
    # This one was exploitable against a real DuckDB: `SELECT 'a\' ; DROP
    # TABLE probe` dropped the table. In a plain '...' DuckDB treats the
    # backslash as a literal and the quote still closes the string ('a\'), so
    # ` ; DROP TABLE t` is a second statement. Masking the backslash as an
    # escape would swallow the closing quote and the rest of the line.
    message = _rejects(r"SELECT 'a\' ; DROP TABLE t")
    assert message is not None and "single SELECT statement" in message
    message = _rejects(r"SELECT 'a\' ; ATTACH 'x.db' AS x")
    assert message is not None and "single SELECT statement" in message


def test_the_exploit_that_survives_an_accidental_unterminated_verdict():
    # Without the trailing `--'`, a masker that wrongly treats `\` as an
    # escape runs off the end of the input and reports "unterminated" —
    # blocking the injection by luck, not by logic. The trailing quote inside
    # a line comment makes the masked text look balanced again, so the whole
    # statement gets swallowed and the split sees one harmless read. Confirmed
    # against a real DuckDB: the table dropped.
    message = _rejects(r"SELECT 'a\' ; DROP TABLE t --'")
    assert message is not None and "single SELECT statement" in message
    message = _rejects(r"SELECT 'a\' ; CREATE TABLE t2 AS SELECT 1 --'")
    assert message is not None and "single SELECT statement" in message


def test_a_backslash_is_an_escape_in_an_e_string_where_duckdb_says_it_is():
    # The mirror image: `E'a\'` really does escape the quote, so DuckDB reads
    # this as unterminated (verified). Treating it as closed here would
    # report a bogus statement count instead of the real problem.
    message = _rejects(r"SELECT E'a\' AS x")
    assert message is not None and "unterminated" in message
    message = _rejects(r"SELECT e'a\' AS x")
    assert message is not None and "unterminated" in message
    # An E-string that closes properly is fine, `;` inside and all.
    assert _rejects(r"SELECT E'a\'b ; c' AS x") is None


def test_a_plain_string_keeps_working_with_the_doubled_quote_escape():
    assert _rejects(r"SELECT 'a\''b ; c' AS x") is None


def test_a_previously_prepared_statement_cannot_be_run():
    # DuckDB accepts both (verified). PREPARE can't smuggle a write —
    # `PREPARE x AS DROP TABLE t` is a parser error — but EXECUTE runs
    # something prepared earlier on the same connection, so what runs would
    # no longer be what the spec says.
    message = _rejects("EXECUTE stmt")
    assert message is not None and "start with" in message
    message = _rejects("PREPARE stmt AS SELECT 1")
    assert message is not None and "start with" in message
    message = _rejects("SELECT 1; EXECUTE stmt")
    assert message is not None and "single SELECT" in message


def test_a_properly_closed_dollar_quote_still_passes():
    assert _rejects("SELECT $$hello ; world$$ AS greeting") is None
    assert _rejects("SELECT $tag$a ; b$tag$ AS x") is None


def test_a_lone_dollar_sign_is_not_treated_as_a_quote_tag():
    assert _rejects("SELECT price_$ FROM t") is None


def test_nested_block_comments_close_at_the_right_depth():
    # If nesting were mishandled, the tail after the outer close would be
    # masked and a chained statement could hide there.
    message = _rejects("SELECT 1 /* a /* b */ c */; DROP TABLE t")
    assert message is not None and "single SELECT" in message


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("", "empty"),
        ("   \n  ", "empty"),
        (";", "empty"),
        (";;;", "empty"),
        ("-- just a comment", "empty"),
    ],
)
def test_rejects_degenerate_input(sql, expected):
    message = _rejects(sql)
    assert message is not None and expected in message


def test_refuses_an_oversized_query_before_scanning_it():
    huge = f"SELECT {'x' * MAX_SQL_CHARS}"
    message = _rejects(huge)
    assert message is not None and "too large" in message


@pytest.mark.parametrize("not_sql", [None, 42, {}, ["SELECT 1"]])
def test_refuses_a_non_string_query(not_sql):
    # The value comes from TOML an agent or a person wrote, so the static type
    # is a claim: `sql = 42` in a spec arrives here as an int and would
    # otherwise fail somewhere less obvious than a clean guard rejection.
    with pytest.raises(SqlGuardError):
        ensure_read_only_select(not_sql)


def test_raises_sql_guard_error_so_callers_can_tell_a_guard_refusal_from_a_duckdb_error():
    with pytest.raises(SqlGuardError):
        ensure_read_only_select("DROP TABLE t")
