"""MySQL table powerbidb.2024BrandAnalytics (DB_*): inspection, and writing one SQP week.

Read-only sessions make MySQL itself reject any write statement, so ``--inspect`` and
dry runs cannot change the table. Errors reaching MySQL end the run with exit 3.

How a week is stored (inspected 2026-10-24): one row per ASIN and search query, with
``reporting_date`` = the week's Saturday at 00:00, ``week`` = "Week N" (N counted by the
Saturdays of the year), ``year``, the ASIN's ``name`` and ``Index`` = ``id``. The table has
no unique key, so a week is written by matching (reporting_date, ASIN, search query):
matching rows get their metrics updated, new queries are inserted, nothing is ever deleted,
and names, other weeks and rows the report does not have stay as they are.
"""
from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from . import schema
from .config import Settings
from .exceptions import DatabaseError, MalformedReport
from .logging_config import get_logger
from .report import WeekReport, normalize_query, validate_asins
from .schema import quote

log = get_logger("powerbi")


@contextmanager
def connect(settings: Settings, read_only: bool = True) -> Iterator:
    """Open a connection. Read-only sessions make MySQL reject any write statement."""
    import pymysql
    import pymysql.cursors

    settings.require_db()
    try:
        conn = pymysql.connect(host=settings.db_host, port=settings.db_port, user=settings.db_user,
                               password=settings.db_password, database=settings.db_name, charset="utf8mb4",
                               autocommit=False, cursorclass=pymysql.cursors.DictCursor, connect_timeout=15)
    except pymysql.err.OperationalError as exc:
        raise DatabaseError(f"MySQL connection failed: {exc}") from None
    try:
        with conn.cursor() as cur:
            cur.execute("SET SESSION TRANSACTION READ ONLY" if read_only else "SET SESSION TRANSACTION READ WRITE")
        yield conn
    except pymysql.err.OperationalError as exc:
        raise DatabaseError(f"MySQL connection failed: {exc}") from None
    finally:
        conn.close()


@dataclass
class TableInfo:
    columns: list[dict]
    indexes: list[dict]
    create_sql: str
    row_count: int

    @property
    def column_names(self) -> list[str]:
        return [c["COLUMN_NAME"] for c in self.columns]

    def unique_indexes(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for idx in sorted(self.indexes, key=lambda i: (i["Key_name"], i["Seq_in_index"])):
            if not idx["Non_unique"]:
                result.setdefault(idx["Key_name"], []).append(idx["Column_name"])
        return result

    def find_column(self, *patterns: str) -> str | None:
        for pattern in patterns:
            for name in self.column_names:
                if re.search(pattern, name, re.IGNORECASE):
                    return name
        return None


def inspect_table(conn, settings: Settings) -> TableInfo:
    table = settings.db_table
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COLUMN_NAME, COLUMN_TYPE, DATA_TYPE, IS_NULLABLE, COLUMN_KEY,
                   COLUMN_DEFAULT, EXTRA, CHARACTER_SET_NAME, COLLATION_NAME
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
            ORDER BY ORDINAL_POSITION
            """,
            (settings.db_name, table),
        )
        columns = list(cur.fetchall())
        if not columns:
            raise DatabaseError(f"Table {settings.db_name}.{table} not found or not visible to this user")
        cur.execute(f"SHOW INDEX FROM {quote(table)}")
        indexes = list(cur.fetchall())
        cur.execute(f"SHOW CREATE TABLE {quote(table)}")
        create_sql = cur.fetchone()["Create Table"]
        cur.execute(f"SELECT COUNT(*) AS n FROM {quote(table)}")
        row_count = cur.fetchone()["n"]
    return TableInfo(columns, indexes, create_sql, row_count)


@dataclass
class AsinReport:
    asins: list[str]
    invalid: list[Any]
    raw_distinct: int
    name_conflicts: dict[str, list[str]] = field(default_factory=dict)


def load_asins(conn, settings: Settings, asin_col: str, name_col: str | None) -> AsinReport:
    """Read-only: unique ASINs from the table plus ASINs that have more than one product name."""
    table = quote(settings.db_table)
    with conn.cursor() as cur:
        cur.execute(f"SELECT DISTINCT {quote(asin_col)} AS asin FROM {table}")
        raw = [r["asin"] for r in cur.fetchall()]
        conflicts: dict[str, list[str]] = {}
        if name_col:
            cur.execute(
                f"""
                SELECT UPPER(TRIM({quote(asin_col)})) AS asin, {quote(name_col)} AS name
                FROM {table}
                GROUP BY UPPER(TRIM({quote(asin_col)})), {quote(name_col)}
                """
            )
            names: dict[str, set] = {}
            for r in cur.fetchall():
                names.setdefault(r["asin"], set()).add(r["name"])
            conflicts = {a: sorted(map(str, n)) for a, n in names.items() if len(n) > 1}
    valid, invalid = validate_asins(raw)
    return AsinReport(valid, invalid, len(raw), conflicts)


def inspection(settings: Settings, asin_list=None) -> tuple[int, list[str]]:
    """Read-only report on the table: columns, unique keys, ASINs and product-name conflicts, and how
    the table's ASINs differ from config/asins.txt (``asin_list``). Changes nothing."""
    lines = []
    with connect(settings, read_only=True) as conn:
        info = inspect_table(conn, settings)
        lines.append(f"\nTable {settings.db_name}.{settings.db_table}: {info.row_count} rows\n")
        lines.append(f"{'column':40} {'type':28} null key extra")
        for c in info.columns:
            lines.append(f"{c['COLUMN_NAME']:40} {c['COLUMN_TYPE']:28} {c['IS_NULLABLE']:4} "
                         f"{c['COLUMN_KEY']:3} {c['EXTRA']}")
        lines.append(f"\nUnique indexes: {info.unique_indexes() or 'none'}")
        lines.append("\n" + info.create_sql + "\n")

        asin_col = info.find_column(r"^asin$", r"asin")
        name_col = info.find_column(r"^name$", r"product.?name", r"^title$", r"item.?name", r"product")
        if not asin_col:
            lines.append("No ASIN column detected.")
            return 1, lines
        report = load_asins(conn, settings, asin_col, name_col)
        lines.append(f"ASIN column: {asin_col!r}; product name column: {name_col!r}")
        lines.append(f"Distinct raw values: {report.raw_distinct}; unique valid ASINs: {len(report.asins)} "
                     f"(expected {settings.expected_asin_count})")
        if report.invalid:
            lines.append(f"Invalid ASIN values: {report.invalid}")
        if len(report.asins) != settings.expected_asin_count:
            lines.append(f"WARNING: expected {settings.expected_asin_count} unique ASINs, found {len(report.asins)}")
        lines.append("ASINs: " + " ".join(report.asins))
        if report.name_conflicts:
            lines.append(f"\nASINs with conflicting product names ({len(report.name_conflicts)}), unchanged:")
            lines += [f"  {asin}: {names}" for asin, names in report.name_conflicts.items()]
        else:
            lines.append("No conflicting product names.")
        if asin_list is not None:
            lines += [""] + asin_list.summary(settings.expected_asin_count)
            only_table = sorted(set(report.asins) - set(asin_list.asins))
            only_file = sorted(set(asin_list.asins) - set(report.asins))
            lines.append(f"In the table, not in {asin_list.path.name}: {' '.join(only_table) or 'none'}")
            lines.append(f"In {asin_list.path.name}, not in the table: {' '.join(only_file) or 'none'}")
        conn.rollback()
    return 0, lines


# --------------------------------------------------------------------------- writing a week


def week_label(end: dt.date) -> str:
    """The table's "Week N": the week whose Saturday is the N-th Saturday of its year."""
    return f"Week {(end.timetuple().tm_yday - 1) // 7 + 1}"


def table_values(record: dict) -> dict[str, Any]:
    """One report record as table columns. Amazon sends shares and rates as percentages,
    like the table; percentages and prices are rounded to the table's 2 decimals."""
    values = {}
    for name, column in schema.TABLE_COLUMNS.items():
        value = record[name]
        if value is not None and (name in schema.PERCENT_FIELDS or name in schema.PRICE_FIELDS):
            value = round(float(value), 2)
        values[column] = value
    return values


def _same(old: Any, new: Any) -> bool:
    if old is None or new is None:
        return old is new
    if isinstance(new, float) or isinstance(old, float):
        # The table's FLOAT columns keep about 6-7 significant digits: a price of 31523.45 reads back as
        # 31523.4, so large values are compared to that precision, small ones to 2 decimals.
        a, b = float(old), float(new)
        return round(a, 2) == round(b, 2) or abs(a - b) <= 5e-6 * max(abs(a), abs(b))
    return int(old) == int(new)


def _shifted_copy(row: dict, new: dict) -> bool:
    """Whether ``row`` is this query as an earlier load broke it: the search query score glued to the
    text ("car sun shade 25??96") and every number one column to the left (score = volume, ...)."""
    if not str(row["search_query"]).endswith(str(new["search_query_score"])):
        return False
    columns = schema.METRIC_COLUMNS
    pairs = [(row[columns[i]], new[columns[i + 1]]) for i in range(len(columns) - 1)]
    comparable = [(a, b) for a, b in pairs if a is not None and b is not None]
    return len(comparable) >= 10 and sum(_same(a, b) for a, b in comparable) >= 0.8 * len(comparable)


@dataclass
class WeekPlan:
    reporting_date: dt.datetime
    week: str
    year: int
    existing_rows: int = 0
    inserts: list[dict] = field(default_factory=list)  # full rows
    updates: list[tuple[int, dict, dict]] = field(default_factory=list)  # (id, column -> new, column -> old)
    unchanged: int = 0
    kept_values: int = 0  # values Amazon left empty where the table has one: kept
    ambiguous: list[tuple[str, str]] = field(default_factory=list)  # keys on several rows: not written
    not_in_report: int = 0  # rows of the week the report does not have: kept
    unnamed: list[str] = field(default_factory=list)  # ASINs without a name in the table
    # Rows matched by their numbers because the stored query text was damaged: (id, table text, Amazon's text).
    # Counted in ``unchanged``; the stored text is left as it is.
    text_differs: list[tuple[int, str, str]] = field(default_factory=list)
    # Rows an earlier load broke (score glued to the text, numbers one column to the left): (id, stored text,
    # Amazon's values). Rewritten with Amazon's query text and every value.
    repairs: list[tuple[int, str, dict]] = field(default_factory=list)

    @property
    def changes(self) -> int:
        return len(self.inserts) + len(self.updates) + len(self.repairs)

    def summary(self) -> list[str]:
        lines = [
            f"table rows for {self.reporting_date:%Y-%m-%d} ({self.week} {self.year}): {self.existing_rows} now",
            f"plan: {len(self.inserts)} insert, {len(self.updates)} update, {len(self.repairs)} repair, "
            f"{self.unchanged} unchanged, "
            f"{len(self.ambiguous)} skipped (key on several rows), {self.not_in_report} table rows not in the "
            f"report (kept), {self.kept_values} empty Amazon values (table value kept)",
        ]
        if self.repairs:
            lines.append(f"{len(self.repairs)} broken row(s) repaired in place (score glued to the query text, numbers "
                         "one column to the left): Amazon's query text and values, no duplicate inserted")
        if self.text_differs:
            lines.append(f"{len(self.text_differs)} of the unchanged rows matched by their numbers: the stored query "
                         "text has '?' for special characters (kept as it is)")
        if self.unnamed:
            lines.append(f"ASINs without a name in the table (inserted with an empty name): {' '.join(self.unnamed)}")
        changed: dict[str, int] = defaultdict(int)
        for _, new, _ in self.updates:
            for column in new:
                changed[column] += 1
        if changed:
            lines.append("updated columns: " + ", ".join(
                f"{column} x{count}" for column, count in sorted(changed.items(), key=lambda item: -item[1])))
        return lines


def _names(cur, settings: Settings, asins: list[str]) -> dict[str, str]:
    """Each ASIN's name as on its newest row (names are never changed)."""
    table = quote(settings.db_table)
    marks = ", ".join(["%s"] * len(asins))
    cur.execute(f"SELECT t.asin, t.name FROM {table} t JOIN (SELECT asin, MAX(id) AS id FROM {table} "
                f"WHERE asin IN ({marks}) GROUP BY asin) newest ON newest.id = t.id", asins)
    return {str(row["asin"]).strip().upper(): row["name"] for row in cur.fetchall()}


def _plan(cur, settings: Settings, week: WeekReport, lock: bool = False) -> WeekPlan:
    for record in week.parsed.records:
        for name in schema.PRICE_FIELDS:
            currency = record.get(f"{name}_currency")
            if currency not in (None, "USD"):
                raise MalformedReport(f"{record['asin']} / {record['search_query']!r}: {name} is in {currency}; "
                                      f"{settings.db_table} holds USD only")
    day = dt.datetime.combine(week.end, dt.time())
    plan = WeekPlan(day, week_label(week.end), week.end.year)
    table = quote(settings.db_table)
    metrics = ", ".join(quote(c) for c in schema.METRIC_COLUMNS)
    cur.execute(f"SELECT id, asin, search_query, {metrics} FROM {table} WHERE reporting_date = %s"
                + (" FOR UPDATE" if lock else ""), (day,))
    existing: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in cur.fetchall():
        existing[(str(row["asin"]).strip().upper(), normalize_query(str(row["search_query"])))].append(row)
        plan.existing_rows += 1

    names = _names(cur, settings, week.returned) if week.returned else {}
    report_keys = {(r["asin"], r["search_query_normalized"]) for r in week.parsed.records}
    # Earlier loads stored some queries with their special characters turned into "?" ("24”x13”" became
    # "24??13??"), so the text cannot match. Such a row is the same query when it is the only row of that
    # ASIN and week, unmatched by text, with every one of Amazon's numbers equal.
    leftovers: dict[str, list[dict]] = defaultdict(list)
    for (asin, _), rows in existing.items():
        if (asin, _) not in report_keys and len(rows) == 1:
            leftovers[asin].append(rows[0])
    seen, taken = set(), set()
    for record in week.parsed.records:
        key = (record["asin"], record["search_query_normalized"])
        seen.add(key)
        new = table_values(record)
        rows = existing.get(key, [])
        if not rows:
            same_numbers = [row for row in leftovers.get(record["asin"], []) if row["id"] not in taken
                            and all(_same(row[c], new[c]) for c in schema.METRIC_COLUMNS)]
            if len(same_numbers) == 1:
                taken.add(same_numbers[0]["id"])
                plan.text_differs.append((same_numbers[0]["id"], same_numbers[0]["search_query"], record["search_query"]))
                plan.unchanged += 1
                continue
            if len(same_numbers) > 1:
                plan.ambiguous.append(key)
                continue
            shifted = [row for row in leftovers.get(record["asin"], []) if row["id"] not in taken
                       and _shifted_copy(row, new)]
            if len(shifted) == 1:
                taken.add(shifted[0]["id"])
                plan.repairs.append((shifted[0]["id"], shifted[0]["search_query"], new))
                continue
            if len(shifted) > 1:
                plan.ambiguous.append(key)
                continue
        if len(rows) > 1:
            plan.ambiguous.append(key)
        elif rows:
            old = rows[0]
            changes, before = {}, {}
            for column in schema.METRIC_COLUMNS:
                if new[column] is None:
                    plan.kept_values += old[column] is not None
                elif not _same(old[column], new[column]):
                    changes[column], before[column] = new[column], old[column]
            if changes:
                plan.updates.append((old["id"], changes, before))
            else:
                plan.unchanged += 1
        else:
            if record["asin"] not in names and record["asin"] not in plan.unnamed:
                plan.unnamed.append(record["asin"])
            plan.inserts.append({**new, "reporting_date": day, "week": plan.week, "year": plan.year,
                                 "asin": record["asin"], "name": names.get(record["asin"])})
    plan.not_in_report = sum(len(rows) for key, rows in existing.items() if key not in seen) - len(taken)
    return plan


def plan_week(settings: Settings, week: WeekReport) -> WeekPlan:
    """Read-only: what writing the week would insert and update. Changes nothing."""
    with connect(settings, read_only=True) as conn:
        with conn.cursor() as cur:
            plan = _plan(cur, settings, week)
        conn.rollback()
    return plan


def write_week(settings: Settings, week: WeekReport, insert: bool = True) -> WeekPlan:
    """Update and insert the week in one transaction; anything unexpected rolls it all back.

    ``insert=False`` changes existing rows only: the planned inserts stay in the plan, unwritten.
    """
    import pymysql

    table = quote(settings.db_table)
    with connect(settings, read_only=False) as conn:
        try:
            conn.begin()
            with conn.cursor() as cur:
                plan = _plan(cur, settings, week, lock=True)
                repairs = [(row_id, values) for row_id, _, values in plan.repairs]
                for row_id, changes in [*((i, c) for i, c, _ in plan.updates), *repairs]:
                    assignments = ", ".join(f"{quote(c)} = %s" for c in changes)
                    cur.execute(f"UPDATE {table} SET {assignments} WHERE id = %s", (*changes.values(), row_id))
                inserted = plan.inserts if insert else []
                if inserted:
                    columns = list(inserted[0])
                    cur.executemany(f"INSERT INTO {table} ({', '.join(quote(c) for c in columns)}) "
                                    f"VALUES ({', '.join(['%s'] * len(columns))})",
                                    [tuple(row[c] for c in columns) for row in inserted])
                    # The table keeps Index = id on every row.
                    cur.execute(f"UPDATE {table} SET `Index` = id WHERE reporting_date = %s AND `Index` IS NULL",
                                (plan.reporting_date,))
                cur.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE reporting_date = %s", (plan.reporting_date,))
                count = cur.fetchone()["n"]
                if count != plan.existing_rows + len(inserted):
                    raise DatabaseError(f"{plan.reporting_date:%Y-%m-%d}: expected "
                                        f"{plan.existing_rows + len(inserted)} rows after writing, found {count}")
            conn.commit()
        except (pymysql.MySQLError, DatabaseError, MalformedReport) as exc:
            try:
                conn.rollback()
            except pymysql.MySQLError:
                pass  # the connection is gone: MySQL discards the open transaction itself
            error = exc if isinstance(exc, (DatabaseError, MalformedReport)) else DatabaseError(str(exc))
            raise type(error)(f"writing {settings.db_table} {week.start}..{week.end}: {error}; "
                              "rolled back, nothing changed") from None
    log.info("%s.%s %s: inserted %d, updated %d, repaired %d, unchanged %d%s", settings.db_name, settings.db_table,
             plan.reporting_date.date(), len(plan.inserts) if insert else 0, len(plan.updates), len(plan.repairs),
             plan.unchanged, "" if insert else f", {len(plan.inserts)} new queries not inserted (--no-insert)")
    return plan
