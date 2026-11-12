"""Database access for SQLite (local / Windows / Linux server) and
PostgreSQL (Neon, used when the portal runs on Vercel).

The application is written against SQLite's dialect. With DATABASE_URL set,
statements are passed through translate() and a few SQLite functions the
queries use (julianday, date with modifiers, group_concat, sum over a
boolean) are created inside PostgreSQL, so the same SQL works on both.
Everything lives in its own schema (PORTAL_DB_SCHEMA, default "adust_it"),
so the portal can share a Neon database with other apps.
"""

import os
import re
import sqlite3
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
IS_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
SCHEMA = re.sub(r"[^a-z0-9_]", "", os.environ.get("PORTAL_DB_SCHEMA", "adust_it").lower()) or "adust_it"

if IS_PG:
    import psycopg2
    IntegrityError = (sqlite3.IntegrityError, psycopg2.IntegrityError)
else:
    IntegrityError = (sqlite3.IntegrityError,)


class Row:
    """A result row usable like sqlite3.Row: row[0], row["name"], dict(row), row.keys()."""
    __slots__ = ("_values", "_index")

    def __init__(self, names, values):
        self._values = tuple(values)
        self._index = names

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        return self._values[self._index[key]]

    def keys(self):
        return list(self._index)

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __repr__(self):
        return f"Row({dict(zip(self._index, self._values))})"


class _Cursor:
    def __init__(self, cur=None, lastrowid=None, rows=None):
        self._cur = cur
        self.lastrowid = lastrowid
        self._buffer = rows
        self.rowcount = cur.rowcount if cur is not None else 0

    def _wrap(self, rec):
        if rec is None:
            return None
        names = {d[0]: i for i, d in enumerate(self._cur.description)}
        return Row(names, (bytes(v) if isinstance(v, memoryview) else v for v in rec))

    def fetchone(self):
        if self._buffer is not None:
            return self._buffer.pop(0) if self._buffer else None
        if self._cur is None or self._cur.description is None:
            return None
        return self._wrap(self._cur.fetchone())

    def fetchall(self):
        if self._buffer is not None:
            rows, self._buffer = self._buffer, []
            return rows
        if self._cur is None or self._cur.description is None:
            return []
        return [self._wrap(r) for r in self._cur.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


_INSERT = re.compile(r"^\s*INSERT\s+(OR\s+IGNORE\s+)?INTO\s+(\w+)", re.I)
NO_ID_TABLES = {"settings"}


def translate(sql):
    """SQLite dialect -> PostgreSQL for the statements this app uses."""
    sql = sql.replace("%", "%%").replace("?", "%s")
    m = _INSERT.match(sql)
    if m and m.group(1):
        sql = _INSERT.sub(lambda mm: f"INSERT INTO {mm.group(2)}", sql, count=1) + " ON CONFLICT DO NOTHING"
    sql = re.sub(r"\bLIKE\b", "ILIKE", sql)
    return sql


class PGConnection:
    """psycopg2 connection with the sqlite3 interface the app uses."""

    def __init__(self):
        self.conn = psycopg2.connect(clean_url(DATABASE_URL), connect_timeout=15)
        self._fresh_tx = True

    def _cursor(self):
        cur = self.conn.cursor()
        if self._fresh_tx:
            # SET LOCAL lasts for this transaction only, which also works through
            # Neon's transaction-pooling endpoint (a plain SET could be lost).
            cur.execute(f'SET LOCAL search_path TO "{SCHEMA}", public')
            self._fresh_tx = False
        return cur

    def execute(self, sql, params=()):
        if sql.lstrip().upper().startswith("PRAGMA"):
            return _Cursor(rows=[])
        pg = translate(sql)
        m = _INSERT.match(sql)
        want_id = bool(m) and m.group(2).lower() not in NO_ID_TABLES and "RETURNING" not in pg.upper()
        if want_id:
            pg += " RETURNING id"
        cur = self._cursor()
        try:
            cur.execute(pg, tuple(params))
        except psycopg2.Error:
            self.rollback()
            raise
        lastrowid = None
        if want_id:
            rec = cur.fetchone()
            lastrowid = rec[0] if rec else None
            return _Cursor(cur, lastrowid=lastrowid, rows=[])
        return _Cursor(cur)

    def executemany(self, sql, seq):
        cur = self._cursor()
        try:
            cur.executemany(translate(sql), [tuple(p) for p in seq])
        except psycopg2.Error:
            self.rollback()
            raise
        return _Cursor(cur)

    def executescript(self, script):
        cur = self._cursor()
        try:
            cur.execute(script)
        except psycopg2.Error:
            self.rollback()
            raise

    def commit(self):
        self.conn.commit()
        self._fresh_tx = True

    def rollback(self):
        self.conn.rollback()
        self._fresh_tx = True

    def close(self):
        self.conn.close()

    def columns(self, table):
        cur = self._cursor()
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = %s",
                    (SCHEMA, table))
        return {r[0] for r in cur.fetchall()}


class SQLiteConnection(sqlite3.Connection):
    def columns(self, table):
        return {r[1] for r in self.execute(f"PRAGMA table_info({table})")}


def clean_url(url):
    """Drop query options psycopg2 does not know (e.g. Prisma's schema=, pgbouncer=)."""
    parts = urlsplit(url)
    keep = [(k, v) for k, v in parse_qsl(parts.query) if k not in ("schema", "pgbouncer", "connection_limit",
                                                                   "pool_timeout", "statement_cache_size")]
    return urlunsplit(parts._replace(scheme="postgresql", query=urlencode(keep)))


def connect(sqlite_path):
    if IS_PG:
        return PGConnection()
    conn = sqlite3.connect(sqlite_path, timeout=15, factory=SQLiteConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def pg_schema(sqlite_schema):
    """The SQLite schema rewritten for PostgreSQL."""
    s = sqlite_schema.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
    s = s.replace(" COLLATE NOCASE", "").replace(" BLOB", " BYTEA")
    return f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}";\nSET LOCAL search_path TO "{SCHEMA}", public;\n' + s + PG_FUNCTIONS


# SQLite functions used by the queries, recreated in the portal's own schema.
PG_FUNCTIONS = r"""
CREATE OR REPLACE FUNCTION julianday(t text) RETURNS double precision LANGUAGE sql IMMUTABLE AS
$$ SELECT extract(epoch FROM t::timestamp) / 86400.0 + 2440587.5 $$;

CREATE OR REPLACE FUNCTION _sqlite_date(t text, mods text[]) RETURNS text LANGUAGE plpgsql STABLE AS $$
DECLARE d timestamp; m text; n int;
BEGIN
  IF t IS NULL THEN RETURN NULL; END IF;
  d := CASE WHEN lower(t) = 'now' THEN now()::timestamp ELSE t::timestamp END;
  FOREACH m IN ARRAY mods LOOP
    IF m = 'start of month' THEN d := date_trunc('month', d);
    ELSIF m ~ '^[+-]?[0-9]+ (day|days)$' THEN n := split_part(m, ' ', 1)::int; d := d + make_interval(days => n);
    ELSIF m ~ '^[+-]?[0-9]+ (month|months)$' THEN n := split_part(m, ' ', 1)::int; d := d + make_interval(months => n);
    ELSIF m ~ '^[+-]?[0-9]+ (year|years)$' THEN n := split_part(m, ' ', 1)::int; d := d + make_interval(years => n);
    END IF;
  END LOOP;
  RETURN to_char(d, 'YYYY-MM-DD');
END $$;
CREATE OR REPLACE FUNCTION date(t text, a text) RETURNS text LANGUAGE sql STABLE AS $$ SELECT _sqlite_date(t, ARRAY[a]) $$;
CREATE OR REPLACE FUNCTION date(t text, a text, b text) RETURNS text LANGUAGE sql STABLE AS $$ SELECT _sqlite_date(t, ARRAY[a, b]) $$;

CREATE OR REPLACE FUNCTION _gc(acc text, val text, sep text) RETURNS text LANGUAGE sql IMMUTABLE AS
$$ SELECT CASE WHEN val IS NULL THEN acc WHEN acc IS NULL THEN val ELSE acc || sep || val END $$;
CREATE OR REPLACE AGGREGATE group_concat(text, text) (SFUNC = _gc, STYPE = text);

CREATE OR REPLACE FUNCTION _bool_sum(acc bigint, val boolean) RETURNS bigint LANGUAGE sql IMMUTABLE AS
$$ SELECT CASE WHEN val IS NULL THEN acc ELSE coalesce(acc, 0) + val::int END $$;
CREATE OR REPLACE AGGREGATE sum(boolean) (SFUNC = _bool_sum, STYPE = bigint);

CREATE OR REPLACE FUNCTION round(v double precision, places integer) RETURNS numeric LANGUAGE sql IMMUTABLE AS
$$ SELECT round(v::numeric, places) $$;
"""
