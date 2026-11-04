"""Top Search Terms: streaming the daily report, keeping the brand's search terms, table rows, days due."""
from __future__ import annotations

import datetime as dt
import gzip
import json

import pytest

from app import top_search
from app.exceptions import MalformedReport

DAY = dt.date(2026, 4, 29)


def rec(term, rank, click_rank, asin, title="Other Brand Thing", click=0.1, conversion=0.05):
    return {"departmentName": "Amazon.com", "searchTerm": term, "searchFrequencyRank": rank, "clickedAsin": asin,
            "clickedItemName": title, "clickShareRank": click_rank, "clickShare": click, "conversionShare": conversion}


def report(records):
    return {"reportSpecification": {"reportType": top_search.REPORT_TYPE, "reportOptions": {"reportPeriod": "DAY"},
                                    "dataStartTime": "2026-04-29", "dataEndTime": "2026-04-29",
                                    "marketplaceIds": ["ATVPDKIKX0DER"]},
            "dataByDepartmentAndSearchTerm": records}


def chunks(data: bytes, size: int):
    return [data[i:i + size] for i in range(0, len(data), size)]


@pytest.mark.parametrize("size", [1, 7, 64, 1 << 20])
def test_records_stream_out_of_a_gzipped_report_in_any_chunk_size(size):
    records = [rec("sonnenschirm für auto", 5, 1, "B0AAAAAAA1", title="Sonnenschutz – Größe M"),
               rec("car shade", 9, 1, "B0AAAAAAA2")]
    data = gzip.compress(json.dumps(report(records), indent=2, ensure_ascii=False).encode("utf-8"))
    assert list(top_search.iter_records(chunks(data, size), gzipped=True)) == records


def test_a_report_without_the_search_term_list_is_rejected():
    with pytest.raises(MalformedReport):
        list(top_search.iter_records([b'{"errorDetails": "nope"}'], gzipped=False))


def test_only_search_terms_with_one_of_our_products_in_the_top_3_are_kept():
    records = [
        rec("needoh", 1, 1, "B0X1"), rec("needoh", 1, 2, "B0X2"), rec("needoh", 1, 3, "B0X3"),
        rec("car sun shade", 7, 1, "B0OTHER1"), rec("car sun shade", 7, 2, "B0OURS0001"),
        rec("car tray", 8, 1, "B0NEWECO1", title="EcoNour Car Tray for Eating"),
        rec("toilet paper", 9, 1, "B0X4"),
    ]
    groups = top_search.brand_terms(records, {"B0OURS0001"}, "EcoNour")
    assert [[r["clickShareRank"] for r in g] for g in groups] == [[1, 2], [1]]
    assert [g[0]["searchTerm"] for g in groups] == ["car sun shade", "car tray"]


def test_table_rows_put_products_in_click_share_order_and_shares_in_percent():
    group = [rec("car sun shade", 18160, 2, "B01KIFISX2", "EcoNour Sunshade", 0.0806, 0.0789),
             rec("car sun shade", 18160, 1, "B074DLQ2YM", "Magnelex Sunshade", 0.1155, 0.0921)]
    [row] = top_search.table_rows([group], DAY)
    assert row["search_frequency_rank"] == 18160 and row["reporting_date"] == "2026-04-29"
    assert (row["top_clicked_product_1_asin"], row["top_clicked_product_1_click_share"],
            row["top_clicked_product_1_conversion_share"]) == ("B074DLQ2YM", 11.55, 9.21)
    assert row["top_clicked_product_2_title"] == "EcoNour Sunshade" and row["top_clicked_product_3_asin"] is None
    assert row["top_clicked_brand_1"] is None and row["top_clicked_category_1"] is None  # not in the SP-API report
    assert set(row) == set(top_search.COLUMNS)


def test_days_due_are_the_days_without_rows_oldest_first():
    stored = {dt.date(2026, 4, 30)}
    assert top_search.days_due(stored, DAY, dt.date(2026, 5, 2)) == [DAY, dt.date(2026, 5, 1), dt.date(2026, 5, 2)]


class _Client:
    def __init__(self, fail=None):
        self.fail = fail


def test_run_stores_days_in_order_and_stops_at_a_day_amazon_has_not_published(monkeypatch, capsys):
    import run

    days = [DAY, DAY + dt.timedelta(days=1), DAY + dt.timedelta(days=2)]
    loaded, requested = [], []
    monkeypatch.setattr(top_search, "stored_days", lambda settings, first: set())
    monkeypatch.setattr(top_search, "days_due", lambda stored, first, last: list(days))
    monkeypatch.setattr(top_search, "request_day", lambda client, settings, day: requested.append(day))

    def fetch(client, settings, day, asins, brand, deadline):
        if day == days[2]:
            raise top_search.NotPublished(str(day))
        return [{"search_term": "car sun shade"}]

    monkeypatch.setattr(top_search, "fetch_day", fetch)
    monkeypatch.setattr(top_search, "load_day", lambda settings, rows, day: loaded.append(day) or {"inserted": 1})
    monkeypatch.setattr(run.spapi, "SpApiClient", lambda settings: _Client())
    monkeypatch.setenv("SPAPI_MARKETPLACE_ID", "ATVPDKIKX0DER")
    assert run.main(["--top-search", "--apply", "--no-lock"]) == 0
    assert requested == days and loaded == days[:2]
    assert "has not published this day yet" in capsys.readouterr().out


def test_a_refused_report_request_waits_and_collects_the_reports_already_asked_for(monkeypatch):
    import time

    import run

    days = [DAY + dt.timedelta(days=i) for i in range(4)]
    order, refusals, sleeps = [], iter([False, False, True, True, False, False]), []
    monkeypatch.setattr(top_search, "stored_days", lambda settings, first: set())
    monkeypatch.setattr(top_search, "days_due", lambda stored, first, last: list(days))

    def request(client, settings, day):
        if next(refusals):
            order.append(f"refused {day.day}")
            raise top_search.Throttled(str(day))
        order.append(f"request {day.day}")

    monkeypatch.setattr(top_search, "request_day", request)
    monkeypatch.setattr(top_search, "fetch_day", lambda c, s, day, *a: order.append(f"fetch {day.day}") or [])
    monkeypatch.setattr(top_search, "load_day", lambda settings, rows, day: {"inserted": 0})
    monkeypatch.setattr(run.spapi, "SpApiClient", lambda settings: _Client())
    monkeypatch.setattr(time, "sleep", sleeps.append)
    assert run.main(["--top-search", "--apply", "--no-lock"]) == 0
    # day 3 is refused: days 1-2 are collected meanwhile, then it waits once and asks again
    assert order == ["request 29", "request 30", "refused 1", "fetch 29", "refused 1", "fetch 30",
                     "request 1", "request 2", "fetch 1", "fetch 2"]
    assert sleeps == []


def test_a_dry_run_never_stores(monkeypatch):
    import run

    monkeypatch.setattr(top_search, "stored_days", lambda settings, first: set())
    monkeypatch.setattr(top_search, "days_due", lambda stored, first, last: [DAY])
    monkeypatch.setattr(top_search, "request_day", lambda *a: None)
    monkeypatch.setattr(top_search, "fetch_day", lambda *a: [])
    monkeypatch.setattr(top_search, "load_day", lambda *a: pytest.fail("a dry run wrote to MySQL"))
    monkeypatch.setattr(run.spapi, "SpApiClient", lambda settings: _Client())
    assert run.main(["--top-search"]) == 0


def test_max_days_is_only_for_top_search():
    import run

    with pytest.raises(SystemExit):
        run.main(["--weekly", "--max-days", "5"])
