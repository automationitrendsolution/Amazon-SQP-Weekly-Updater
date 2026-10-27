"""Writing a week to 2024BrandAnalytics, against the in-memory FakeTable (tests/conftest.py)."""
from __future__ import annotations

import datetime as dt

import pytest

from app import config, powerbi, schema
from app.exceptions import DatabaseError, MalformedReport
from app.report import combine
from tests.conftest import make_item, make_report

START, END = dt.date(2026, 10, 11), dt.date(2026, 10, 17)
DAY = dt.datetime(2026, 10, 17)
MP = "ATVPDKIKX0DER"


class Batch:
    def __init__(self, report_id, asins, payload):
        self.report_id, self.asins, self.payload = report_id, asins, payload


def week_of(*items, asins=("B0TESTAAAA", "B0TESTBBBB")):
    return combine([Batch("R1", list(asins), make_report(list(items)))], START, END, MP)


def stored(table, item, day=DAY, name="Garlic Press Pro", **overrides):
    """``item`` as the table holds it (a row loaded earlier)."""
    record = week_of(item, asins=(item["asin"],)).parsed.records[0]
    return table.add(**{**powerbi.table_values(record), "reporting_date": day, "week": "Week 42", "year": 2026,
                        "asin": item["asin"], "name": name, **overrides})


@pytest.fixture
def settings():
    return config.load_settings()


def test_week_label_counts_saturdays_like_the_table():
    # Labels as stored in the table, year boundaries included.
    assert powerbi.week_label(dt.date(2026, 10, 17)) == "Week 42"
    assert powerbi.week_label(dt.date(2024, 1, 6)) == "Week 1"
    assert powerbi.week_label(dt.date(2024, 12, 28)) == "Week 52"
    assert powerbi.week_label(dt.date(2025, 1, 4)) == "Week 1"
    assert powerbi.week_label(dt.date(2026, 1, 3)) == "Week 1"


def test_every_report_metric_has_a_column():
    record = week_of(make_item()).parsed.records[0]
    values = powerbi.table_values(record)
    assert list(values) == list(schema.TABLE_COLUMNS.values()) and len(values) == 33
    # Amazon's percentages and the table's agree (0.025 stays 0.03 %, not 2.5)
    assert values["impressions_asin_share_pct"] == 0.03 and values["clicks_rate_pct"] == 0.8
    assert values["purchases_asin_price_median"] == 24.99 and values["search_query_volume"] == 5000


def test_a_new_week_is_inserted_with_the_tables_names_and_index(settings, fake_table):
    stored(fake_table, make_item(), day=dt.datetime(2026, 10, 10), name="Garlic Press Pro")
    week = week_of(make_item(), make_item(query="Other"))
    plan = powerbi.write_week(settings, week)
    assert len(plan.inserts) == 2 and not plan.updates
    new = [r for r in fake_table.rows if r["reporting_date"] == DAY]
    assert len(new) == 2 and {r["name"] for r in new} == {"Garlic Press Pro"}
    assert all(r["Index"] == r["id"] and r["week"] == "Week 42" and r["year"] == 2026 for r in new)
    assert fake_table.committed == 1


def test_a_rerun_changes_nothing_and_adds_no_duplicates(settings, fake_table):
    week = week_of(make_item(), make_item(query="Other"))
    powerbi.write_week(settings, week)
    again = powerbi.write_week(settings, week)
    assert (len(again.inserts), len(again.updates), again.unchanged) == (0, 0, 2)
    assert len(fake_table.rows) == 2


def test_matching_rows_are_updated_in_place_and_nothing_else_is_touched(settings, fake_table):
    old = stored(fake_table, make_item(), name="Original name")
    other_week = stored(fake_table, make_item(), day=dt.datetime(2026, 10, 10))
    not_in_report = stored(fake_table, make_item(query="query Amazon dropped"))
    plan = powerbi.write_week(settings, week_of(make_item(searchQueryData__searchQueryVolume=6000)))
    assert [(row_id, new) for row_id, new, _ in plan.updates] == [(old["id"], {"search_query_volume": 6000})]
    assert old["search_query_volume"] == 6000 and old["name"] == "Original name"
    assert other_week["search_query_volume"] == 5000 and not_in_report in fake_table.rows
    assert plan.not_in_report == 1 and len(fake_table.rows) == 3


def test_an_empty_amazon_value_never_replaces_a_stored_one(settings, fake_table):
    row = stored(fake_table, make_item())
    plan = powerbi.write_week(settings, week_of(make_item(clickData__totalSameDayShippingClickCount=None)))
    assert row["clicks_shipping_same_day"] == 10 and plan.kept_values == 1 and plan.unchanged == 1


def test_queries_match_whatever_their_case_and_spacing(settings, fake_table):
    stored(fake_table, make_item(query="garlic  PRESS"))
    plan = powerbi.write_week(settings, week_of(make_item(query="Garlic Press")))
    assert plan.unchanged == 1 and not plan.inserts and len(fake_table.rows) == 1


def test_a_query_stored_with_question_marks_is_matched_by_its_numbers(settings, fake_table):
    # An earlier load turned special characters into "?": the same query, every number equal.
    damaged = stored(fake_table, make_item(query="24??13??car window shade"))
    plan = powerbi.write_week(settings, week_of(make_item(query="24”x13” car window shade")))
    assert not plan.inserts and plan.unchanged == 1 and plan.not_in_report == 0
    assert plan.text_differs == [(damaged["id"], "24??13??car window shade", "24”x13” car window shade")]
    assert fake_table.rows == [damaged]  # text kept, no near-duplicate


def test_a_row_broken_by_a_column_shift_is_repaired_not_duplicated(settings, fake_table):
    item = make_item(query="car sun shade 25”")
    good = powerbi.table_values(week_of(item).parsed.records[0])
    columns = schema.METRIC_COLUMNS
    # What the earlier load stored: score glued to the text, every number one column to the left.
    broken = {columns[i]: good[columns[i + 1]] for i in range(len(columns) - 1)}
    row = stored(fake_table, make_item(), **broken, search_query="car sun shade 25??1")
    plan = powerbi.write_week(settings, week_of(item))
    assert not plan.inserts and [r[0] for r in plan.repairs] == [row["id"]] and len(fake_table.rows) == 1
    assert row["search_query"] == "car sun shade 25”" and all(row[c] == good[c] for c in columns)
    assert powerbi.plan_week(settings, week_of(item)).unchanged == 1  # repaired rows read back as unchanged


def test_different_numbers_are_a_different_query(settings, fake_table):
    stored(fake_table, make_item(query="sun shade 25?"))
    plan = powerbi.plan_week(settings, week_of(make_item(query="sun shade 25”", searchQueryData__searchQueryVolume=9)))
    assert len(plan.inserts) == 1 and not plan.text_differs and plan.not_in_report == 1


def test_a_key_on_several_rows_is_skipped_not_guessed(settings, fake_table):
    stored(fake_table, make_item())
    stored(fake_table, make_item())
    plan = powerbi.write_week(settings, week_of(make_item(searchQueryData__searchQueryVolume=1)))
    assert plan.ambiguous == [("B0TESTAAAA", "garlic press")] and not plan.updates
    assert {r["search_query_volume"] for r in fake_table.rows} == {5000}


def test_a_failure_part_way_rolls_everything_back(settings, fake_table):
    before = dict(stored(fake_table, make_item()))
    fake_table.fail_on = "UPDATE `2024BrandAnalytics` SET `Index`"  # after the UPDATE and the INSERT
    with pytest.raises(DatabaseError, match="rolled back, nothing changed"):
        powerbi.write_week(settings, week_of(make_item(searchQueryData__searchQueryVolume=1), make_item(query="New")))
    assert fake_table.rows == [before] and fake_table.committed == 0


def test_a_dry_run_plan_cannot_write(settings, fake_table):
    stored(fake_table, make_item())
    plan = powerbi.plan_week(settings, week_of(make_item(searchQueryData__searchQueryVolume=1), make_item(query="New")))
    assert len(plan.updates) == 1 and len(plan.inserts) == 1
    assert len(fake_table.rows) == 1 and fake_table.rows[0]["search_query_volume"] == 5000


def test_a_price_in_another_currency_is_refused(settings, fake_table):
    item = make_item(clickData__asinMedianClickPrice={"amount": 9.99, "currencyCode": "GBP"})
    with pytest.raises(MalformedReport, match="USD only"):
        powerbi.write_week(settings, week_of(item))
    assert fake_table.rows == []


def test_no_insert_changes_existing_rows_only(settings, fake_table):
    row = stored(fake_table, make_item())
    plan = powerbi.write_week(settings, week_of(make_item(searchQueryData__searchQueryVolume=6000),
                                                make_item(query="brand new query")), insert=False)
    assert len(plan.inserts) == 1 and len(plan.updates) == 1  # planned, but the insert is not written
    assert fake_table.rows == [row] and row["search_query_volume"] == 6000


def test_large_prices_compare_at_float_precision():
    # MySQL FLOAT returns 31523.45 as 31523.4: the same stored value, not a change.
    assert powerbi._same(31523.4, 31523.45) and powerbi._same(21324.3, 21324.26)
    assert not powerbi._same(24.99, 25.99) and not powerbi._same(0.08, 0.09)
