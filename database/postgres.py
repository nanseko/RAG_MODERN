"""Narrow DB-API compatibility layer for this application's PostgreSQL SQL.

Supported SQLite conveniences: qmark parameters, named/integer rows,
INSERT OR REPLACE on the two documented metadata tables, and BEGIN IMMEDIATE.
The latter takes a database-scoped advisory transaction lock for short metadata
critical sections; parsing and model calls never hold that lock. Unsupported
SQLite extension SQL fails explicitly rather than using a local fallback.
"""
import re

_REPLACE_TABLES = {
    "documents": (("document_id",), None),
    "document_versions": (("document_id", "version"),
                          ("document_id", "version", "original_path", "markdown_path", "allowed_groups", "content_hash")),
}
_METADATA_LOCK = 724123981


class HybridRow:
    def __init__(self, names, values):
        self._names = tuple(names)
        self._values = tuple(values)
        self._indices = {name: i for i, name in enumerate(names)}

    def __getitem__(self, key):
        return self._values[self._indices[key]] if isinstance(key, str) else self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def keys(self):
        return self._names


def _scan(sql, bind_parameters=True, split=False):
    """Recognize quoted strings/identifiers, dollar quotes and SQL comments."""
    output, statements = [], []
    i, quote, dollar, comment, depth = 0, None, None, None, 0
    while i < len(sql):
        char = sql[i]
        pair = sql[i:i + 2]
        if comment == "line":
            if char == "\n":
                comment = None
        elif comment == "block":
            if pair == "/*":
                depth += 1
                output.append(pair)
                i += 2
                continue
            if pair == "*/":
                depth -= 1
                output.append(pair)
                i += 2
                if not depth:
                    comment = None
                continue
        elif dollar:
            if sql.startswith(dollar, i):
                output.append(dollar)
                i += len(dollar)
                dollar = None
                continue
        elif quote:
            if char == quote:
                if i + 1 < len(sql) and sql[i + 1] == quote:
                    output.append(char * 2)
                    i += 2
                    continue
                quote = None
        elif pair == "--":
            comment = "line"
        elif pair == "/*":
            comment, depth = "block", 1
            output.append(pair)
            i += 2
            continue
        elif char in ("'", '"'):
            quote = char
        elif char == "$":
            match = re.match(r"\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$", sql[i:])
            if match:
                dollar = match.group()
                output.append(dollar)
                i += len(dollar)
                continue
        elif char == "?" and bind_parameters:
            output.append("%s")
            i += 1
            continue
        elif char == ";" and split:
            statement = "".join(output).strip()
            if statement:
                statements.append(statement)
            output.clear()
            i += 1
            continue
        output.append("%%" if char == "%" and bind_parameters else char)
        i += 1
    if quote or dollar or comment == "block":
        raise ValueError("unterminated SQL literal/comment")
    final = "".join(output).strip() if split else "".join(output)
    if split:
        if final:
            statements.append(final)
        return statements
    return final


def convert_sql(sql, bind_parameters=True):
    if re.match(r"\s*(PRAGMA|CREATE\s+(?:VIRTUAL\s+TABLE|TRIGGER))\b", sql, re.I):
        raise ValueError("SQLite extension SQL is unavailable on PostgreSQL")
    if re.search(r"\bINSERT\s+OR\s+IGNORE\b", sql, re.I):
        raise ValueError("unsupported SQLite INSERT OR IGNORE; use ON CONFLICT")
    replace = re.match(r"\s*INSERT\s+OR\s+REPLACE\s+INTO\s+(\w+)\s*(\([^)]*\))?\s*(VALUES\b.*)", sql, re.I | re.S)
    if replace:
        table, column_sql, values = replace.groups()
        spec = _REPLACE_TABLES.get(table.lower())
        if spec is None:
            raise ValueError(f"unsupported INSERT OR REPLACE table: {table}")
        keys, default_columns = spec
        columns = tuple(x.strip() for x in column_sql[1:-1].split(",")) if column_sql else default_columns
        if not columns or any(not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", x) for x in columns):
            raise ValueError("unsupported replacement column list")
        updates = [f"{name}=EXCLUDED.{name}" for name in columns if name not in keys]
        action = "DO UPDATE SET " + ",".join(updates) if updates else "DO NOTHING"
        sql = f"INSERT INTO {table}{column_sql or ''} {values.rstrip().rstrip(';')} ON CONFLICT ({','.join(keys)}) {action}"
    elif re.search(r"\bINSERT\s+OR\s+REPLACE\b", sql, re.I):
        raise ValueError("unsupported INSERT OR REPLACE SQL")
    return _scan(sql, bind_parameters=bind_parameters)


class Cursor:
    def __init__(self, cursor):
        self.raw = cursor

    @property
    def rowcount(self):
        return self.raw.rowcount

    @property
    def description(self):
        return self.raw.description

    def _row(self, values):
        if values is None:
            return None
        names = [column.name if hasattr(column, "name") else column[0] for column in self.raw.description]
        return HybridRow(names, values)

    def fetchone(self):
        return self._row(self.raw.fetchone())

    def fetchall(self):
        return [self._row(values) for values in self.raw.fetchall()]

    def fetchmany(self, size=1):
        return [self._row(values) for values in self.raw.fetchmany(size)]

    def __iter__(self):
        for values in self.raw:
            yield self._row(values)

    def close(self):
        self.raw.close()


class PostgresConnection:
    backend = "postgresql"
    native_vectors = False

    def __init__(self, raw):
        self.raw = raw
        self.in_transaction = False

    def _begin(self, immediate=False):
        if not self.in_transaction:
            self.raw.execute("BEGIN")
            self.in_transaction = True
        if immediate:
            self.raw.execute(f"SELECT pg_advisory_xact_lock({_METADATA_LOCK})")

    def execute(self, sql, parameters=()):
        normalized = sql.strip().rstrip(";").upper()
        if normalized in ("BEGIN", "BEGIN IMMEDIATE"):
            self._begin(immediate=normalized == "BEGIN IMMEDIATE")
            return Cursor(self.raw.execute("SELECT 1 WHERE FALSE"))
        if normalized == "COMMIT":
            self.commit()
            return Cursor(self.raw.execute("SELECT 1 WHERE FALSE"))
        if normalized == "ROLLBACK":
            self.rollback()
            return Cursor(self.raw.execute("SELECT 1 WHERE FALSE"))
        converted = convert_sql(sql, bind_parameters=bool(parameters))
        # Like sqlite3, plain reads do not start transactions; every mutation
        # and a standalone SAVEPOINT does, preserving indexer rollback behavior.
        if not re.match(r"\s*(SELECT|EXPLAIN|SHOW)\b", sql, re.I):
            self._begin()
        return Cursor(self.raw.execute(converted, tuple(parameters) if parameters else None))

    def executemany(self, sql, parameters):
        self._begin()
        cursor = self.raw.cursor()
        cursor.executemany(convert_sql(sql), parameters)
        return Cursor(cursor)

    def executescript(self, sql):
        for statement in _scan(sql, bind_parameters=False, split=True):
            self.execute(statement)

    def commit(self):
        self.raw.commit()
        self.in_transaction = False

    def rollback(self):
        self.raw.rollback()
        self.in_transaction = False

    def close(self):
        self.raw.close()
        self.in_transaction = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.rollback() if exc_type else self.commit()


def connect(dsn):
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("PostgreSQL requires psycopg[binary]; install the operations dependencies") from exc
    raw = psycopg.connect(dsn, autocommit=True, connect_timeout=10)
    return PostgresConnection(raw)