"""Reading the SQP report: ASIN batches, metrics, refused weeks, rejected rows."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.report import ReportValidationError, combine, parse_report, validate_asins
from app.schema import canonical_fields
from app.spapi import ASIN_OPTION_MAX_CHARS, batch_asins

START, END = date(2026, 10, 11), date(2026, 10, 17)
MP = "ATVPDKIKX0DER"


def parse(report, asins=("B0TESTAAAA", "B0TESTBBBB")):
    return parse_report(report, START, END, asins, MP)


# --------------------------------------------------------------------------- ASIN validation / batching


def test_validate_asins_dedupes_and_normalises():
    valid, invalid = validate_asins(["b0testaaaa", " B0TESTAAAA ", "B0TESTBBBB", "", None, "B0SHORT", "B0TEST-AAA"])
    assert valid == ["B0TESTAAAA", "B0TESTBBBB"]
    assert invalid == ["", None, "B0SHORT", "B0TEST-AAA"]


def test_62_asins_batch_within_limit():
    asins = [f"B0{i:08d}" for i in range(62)]
    batches = batch_asins(asins)
    assert [a for b in batches for a in b] == asins
    assert all(len(" ".join(b)) <= ASIN_OPTION_MAX_CHARS for b in batches)
    assert len(batches) == 4  # 18 ASINs fit in 200 chars (18*10 + 17 spaces = 197)
    assert len(batches[0]) == 18


def test_batch_exact_boundary():
    # 200 chars exactly: 20 tokens of 9 chars + 19 spaces = 199; add a 1-char token -> 201
    assert len(batch_asins(["A" * 9] * 20)) == 1
    assert len(batch_asins(["A" * 9] * 20 + ["B"])) == 2


# --------------------------------------------------------------------------- parsing


def test_parses_all_metrics(report_factory, item_factory):
    result = parse(report_factory([item_factory()]))
    assert not result.rejected and not result.unknown_fields
    rec = result.records[0]
    assert set(rec) == set(canonical_fields())
    assert rec["asin"] == "B0TESTAAAA"
    assert rec["start_date"] == START and rec["end_date"] == END
    assert rec["search_query"] == "Garlic Press"
    assert rec["search_query_normalized"] == "garlic press"
    assert rec["search_query_volume"] == 5000
    assert rec["asin_impression_share"] == Decimal("0.025")
    assert rec["asin_median_purchase_price"] == Decimal("24.99")
    assert rec["asin_median_purchase_price_currency"] == "USD"
    assert rec["total_two_day_shipping_purchase_count"] == 200


def test_wrong_week_in_spec_is_refused(report_factory, item_factory):
    report = report_factory([item_factory()], start="2026-10-04", end="2026-10-10")
    with pytest.raises(ReportValidationError, match="another week"):
        parse(report)


def test_wrong_week_in_row_is_refused(report_factory, item_factory):
    report = report_factory([item_factory(), item_factory(start="2026-10-04", end="2026-10-10")])
    with pytest.raises(ReportValidationError, match="substitute"):
        parse(report)


def test_wrong_marketplace_refused(report_factory, item_factory):
    with pytest.raises(ReportValidationError):
        parse(report_factory([item_factory()], marketplace="A1F83G8C2ARO7P"))


def test_missing_data_by_asin_refused():
    with pytest.raises(ReportValidationError):
        parse({"reportSpecification": {}})


def test_bad_rows_rejected_not_written(report_factory, item_factory):
    items = [
        item_factory(),
        item_factory(asin="B0NOTASKED"),
        item_factory(query="   "),
        item_factory(query="negative", impressionData__asinImpressionCount=-1),
        item_factory(query="fraction", clickData__asinClickCount=1.5),
        item_factory(query="empty", searchQueryData__searchQueryVolume=None),
    ]
    result = parse(report_factory(items))
    assert len(result.records) == 1
    reasons = " | ".join(r for _, r in result.rejected)
    assert "not requested" in reasons
    assert "missing search query" in reasons
    assert "negative" in reasons
    assert "not an integer" in reasons
    assert "incomplete row" in reasons


def test_missing_optional_metric_warns(report_factory, item_factory):
    result = parse(report_factory([item_factory(clickData__totalSameDayShippingClickCount=None)]))
    assert len(result.records) == 1
    assert result.records[0]["total_same_day_shipping_click_count"] is None
    assert result.missing_values == {"totalSameDayShippingClickCount": 1}
    assert "totalSameDayShippingClickCount x1" in result.missing_summary()


def test_unknown_fields_are_reported(report_factory, item_factory):
    item = item_factory()
    item["clickData"]["newMetric"] = 1
    item["brandNewSection"] = {}
    result = parse(report_factory([item]))
    assert result.unknown_fields == {"clickData.newMetric", "brandNewSection"}
    assert any("does not map" in w for w in result.warnings)


def test_exact_duplicates_dropped(report_factory, item_factory):
    result = parse(report_factory([item_factory(), item_factory(query="garlic  PRESS")]))
    assert len(result.records) == 1


def test_conflicting_duplicates_refused(report_factory, item_factory):
    items = [item_factory(), item_factory(searchQueryData__searchQueryVolume=1)]
    with pytest.raises(ReportValidationError, match="Conflicting"):
        parse(report_factory(items))


# --------------------------------------------------------------------------- combining batches


class Batch:
    def __init__(self, report_id, asins, payload):
        self.report_id, self.asins, self.payload = report_id, asins, payload


def test_batches_combine_and_missing_asins_are_listed(report_factory, item_factory):
    batches = [
        Batch("R1", ["B0TESTAAAA", "B0TESTBBBB"], report_factory([item_factory(), item_factory(query="Other")])),
        Batch("R2", ["B0TESTCCCC"], report_factory([item_factory(asin="B0TESTCCCC")])),
    ]
    week = combine(batches, START, END, MP)
    assert week.report_ids == ["R1", "R2"] and len(week.parsed.records) == 3
    assert week.requested == ["B0TESTAAAA", "B0TESTBBBB", "B0TESTCCCC"]
    assert week.returned == ["B0TESTAAAA", "B0TESTCCCC"] and week.missing == ["B0TESTBBBB"]
    assert "missing ASINs: B0TESTBBBB" in week.summary()


def test_a_week_without_any_rows_is_not_an_empty_success(report_factory):
    with pytest.raises(ReportValidationError, match="refusing to treat this as an empty week"):
        combine([Batch("R1", ["B0TESTAAAA"], report_factory([]))], START, END, MP)


def test_one_bad_batch_refuses_the_whole_week(report_factory, item_factory):
    batches = [Batch("R1", ["B0TESTAAAA"], report_factory([item_factory()])),
               Batch("R2", ["B0TESTBBBB"], report_factory([item_factory(asin="B0TESTBBBB")], marketplace="A1F83G8C2ARO7P"))]
    with pytest.raises(ReportValidationError):
        combine(batches, START, END, MP)
