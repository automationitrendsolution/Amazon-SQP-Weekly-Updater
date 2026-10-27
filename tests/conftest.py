"""Shared fixtures: SQP report builders shaped like Amazon's JSON, isolated project folders."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

START, END = "2026-10-11", "2026-10-17"


def make_item(asin="B0TESTAAAA", query="Garlic Press", start=START, end=END, **overrides):
    """A dataByAsin entry shaped like Amazon's SQP report."""
    money = lambda amount: {"amount": amount, "currencyCode": "USD"}
    item = {
        "startDate": start,
        "endDate": end,
        "asin": asin,
        "searchQueryData": {"searchQuery": query, "searchQueryScore": 1, "searchQueryVolume": 5000},
        "impressionData": {
            "totalQueryImpressionCount": 100000,
            "asinImpressionCount": 2500,
            "asinImpressionShare": 0.025,
        },
        "clickData": {
            "totalClickCount": 4000, "totalClickRate": 0.8, "asinClickCount": 120, "asinClickShare": 0.03,
            "totalMedianClickPrice": money(19.99), "asinMedianClickPrice": money(24.99),
            "totalSameDayShippingClickCount": 10, "totalOneDayShippingClickCount": 300,
            "totalTwoDayShippingClickCount": 2000,
        },
        "cartAddData": {
            "totalCartAddCount": 900, "totalCartAddRate": 0.18, "asinCartAddCount": 30, "asinCartAddShare": 0.033,
            "totalMedianCartAddPrice": money(18.99), "asinMedianCartAddPrice": money(24.99),
            "totalSameDayShippingCartAddCount": 2, "totalOneDayShippingCartAddCount": 80,
            "totalTwoDayShippingCartAddCount": 500,
        },
        "purchaseData": {
            "totalPurchaseCount": 400, "totalPurchaseRate": 0.08, "asinPurchaseCount": 12, "asinPurchaseShare": 0.03,
            "totalMedianPurchasePrice": money(17.99), "asinMedianPurchasePrice": money(24.99),
            "totalSameDayShippingPurchaseCount": 1, "totalOneDayShippingPurchaseCount": 40,
            "totalTwoDayShippingPurchaseCount": 200,
        },
    }
    for path, value in overrides.items():
        section, _, key = path.partition("__")
        if key:
            item[section][key] = value
        else:
            item[section] = value
    return item


def make_report(items, start=START, end=END, marketplace="ATVPDKIKX0DER"):
    return {
        "reportSpecification": {
            "reportType": "GET_BRAND_ANALYTICS_SEARCH_QUERY_PERFORMANCE_REPORT",
            "reportOptions": {"reportPeriod": "WEEK", "asin": " ".join({i["asin"] for i in items})},
            "dataStartTime": start,
            "dataEndTime": end,
            "marketplaceIds": [marketplace],
        },
        "dataByAsin": items,
    }


@pytest.fixture
def report_factory():
    return make_report


@pytest.fixture
def item_factory():
    return make_item


class FakeTable:
    """powerbidb.2024BrandAnalytics in memory, answering the statements app/powerbi.py sends.

    Read-only connections refuse writes like MySQL does; ``fail_on`` makes a statement
    starting with that text raise, to test the rollback.
    """

    def __init__(self):
        self.rows: list[dict] = []
        self.next_id = 1
        self.read_only = True
        self.fail_on: str | None = None
        self.committed = 0
        self._snapshot = None

    def add(self, **row):
        row = {"id": self.next_id, "Index": self.next_id, **row}
        self.next_id += 1
        self.rows.append(row)
        return row

    # connection
    def cursor(self):
        return FakeTableCursor(self)

    def begin(self):
        self._snapshot = ([dict(r) for r in self.rows], self.next_id)

    def commit(self):
        self._snapshot = None
        self.committed += 1

    def rollback(self):
        if self._snapshot:
            self.rows, self.next_id = [dict(r) for r in self._snapshot[0]], self._snapshot[1]
        self._snapshot = None


class FakeTableCursor:
    def __init__(self, table: FakeTable):
        self.table, self.result = table, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def _check(self, sql):
        import pymysql

        if self.table.fail_on and sql.startswith(self.table.fail_on):
            raise pymysql.err.OperationalError(2013, "Lost connection to MySQL server during query")
        if self.table.read_only and sql.startswith(("UPDATE", "INSERT", "DELETE")):
            raise pymysql.err.OperationalError(1792, "Cannot execute statement in a READ ONLY transaction.")

    def execute(self, sql, params=()):
        import re

        sql = " ".join(sql.split())
        self._check(sql)
        rows = self.table.rows
        if sql.startswith("SELECT id, asin, search_query"):
            self.result = [dict(r) for r in rows if r["reporting_date"] == params[0]]
        elif sql.startswith("SELECT t.asin, t.name"):
            newest = {}
            for r in rows:
                if r["asin"] in params and r["id"] > newest.get(r["asin"], {"id": 0})["id"]:
                    newest[r["asin"]] = r
            self.result = [{"asin": r["asin"], "name": r["name"]} for r in newest.values()]
        elif sql.startswith("SELECT COUNT(*)"):
            self.result = [{"n": sum(1 for r in rows if r["reporting_date"] == params[0])}]
        elif "SET `Index` = id" in sql:
            for r in rows:
                if r["reporting_date"] == params[0] and r.get("Index") is None:
                    r["Index"] = r["id"]
        elif sql.startswith("UPDATE"):
            columns = re.findall(r"`(\w+)` = %s", sql.split(" WHERE ")[0])
            row = next(r for r in rows if r["id"] == params[-1])
            row.update(zip(columns, params[:-1]))
        else:
            raise AssertionError(f"unexpected SQL: {sql}")

    def executemany(self, sql, seq):
        import re

        sql = " ".join(sql.split())
        self._check(sql)
        assert sql.startswith("INSERT"), sql
        columns = re.findall(r"`(\w+)`", sql.split(" VALUES ")[0])[1:]  # [0] is the table
        for values in seq:
            row = {"id": self.table.next_id, "Index": None, **dict(zip(columns, values))}
            self.table.next_id += 1
            self.table.rows.append(row)

    def fetchall(self):
        return self.result

    def fetchone(self):
        return self.result[0]


@pytest.fixture
def fake_table(monkeypatch):
    """Replace the MySQL connection with a FakeTable (the table starts empty)."""
    from contextlib import contextmanager

    from app import powerbi

    table = FakeTable()

    @contextmanager
    def connect(settings, read_only=True):
        table.read_only = read_only
        yield table

    monkeypatch.setattr(powerbi, "connect", connect)
    return table


@pytest.fixture(autouse=True)
def project_dirs(tmp_path, monkeypatch):
    """Every test gets its own data/ and logs/ and no real .env values."""
    data = tmp_path / "data"
    monkeypatch.setenv("SQP_DATA_DIR", str(data))
    monkeypatch.setenv("SQP_LOG_DIR", str(tmp_path / "logs"))
    for name in ("SPAPI_LWA_CLIENT_ID", "SPAPI_LWA_CLIENT_SECRET", "SPAPI_LWA_REFRESH_TOKEN", "SPAPI_ENDPOINT",
                 "SPAPI_MARKETPLACE_ID", "DB_HOST", "DB_USER", "DB_PASSWORD", "DB_TABLE", "EXPECTED_ASIN_COUNT",
                 "POWERBI_DB_HOST", "POWERBI_DB_USER", "POWERBI_DB_PASSWORD", "POWERBI_DB_TABLE",
                 "SQP_TIMEZONE", "SQP_AVAILABILITY_LAG_DAYS", "LOG_LEVEL"):
        monkeypatch.setenv(name, "")
    return data
