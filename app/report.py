"""Parse and validate SQP report JSON into flat, canonical records (see schema.py).

A report for another week, report type or marketplace is refused as a whole; a
single bad row (unrequested ASIN, missing query, negative or fractional count) is
rejected and listed, never written.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from .config import REPORT_TYPE
from .exceptions import MalformedReport
from .schema import METRIC_FIELDS

ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")


class ReportValidationError(MalformedReport):
    """The report as a whole cannot be trusted (wrong period, type, marketplace...)."""


@dataclass
class ParseResult:
    records: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[tuple[dict, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unknown_fields: set[str] = field(default_factory=set)
    missing_values: Counter = field(default_factory=Counter)  # JSON field -> rows where Amazon left it empty

    def extend(self, other: "ParseResult") -> None:
        self.records.extend(other.records)
        self.rejected.extend(other.rejected)
        self.warnings.extend(other.warnings)
        self.unknown_fields |= other.unknown_fields
        self.missing_values += other.missing_values

    def missing_summary(self) -> str | None:
        if not self.missing_values:
            return None
        return "values Amazon left empty (stored as NULL, never over an existing value): " + ", ".join(
            f"{name} x{count}" for name, count in self.missing_values.most_common())


# --------------------------------------------------------------------------- values


def validate_asins(values: Iterable[Any]) -> tuple[list[str], list[str]]:
    """Return (sorted unique valid ASINs, invalid raw values). Normalises case/whitespace."""
    valid, invalid = set(), []
    for raw in values:
        asin = str(raw).strip().upper() if raw is not None else ""
        if ASIN_RE.match(asin):
            valid.add(asin)
        else:
            invalid.append(raw)
    return sorted(valid), invalid


def normalize_query(query: str) -> str:
    return " ".join(query.split()).lower()


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"boolean {value!r} is not a count")
    number = Decimal(str(value))
    if number != number.to_integral_value():
        raise ValueError(f"{value!r} is not an integer")
    return int(number)


def _to_dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"boolean {value!r} is not numeric")
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{value!r} is not numeric") from exc


# --------------------------------------------------------------------------- reports


def _check_spec(payload: dict, start: date, end: date, marketplace_id: str | None) -> None:
    spec = payload.get("reportSpecification") or {}
    if spec.get("reportType") and spec["reportType"] != REPORT_TYPE:
        raise ReportValidationError(f"Unexpected report type {spec['reportType']!r}")
    period = (spec.get("reportOptions") or {}).get("reportPeriod")
    if period and period != "WEEK":
        raise ReportValidationError(f"Unexpected report period {period!r}")
    for key, expected in (("dataStartTime", start), ("dataEndTime", end)):
        if spec.get(key) and str(spec[key])[:10] != expected.isoformat():
            raise ReportValidationError(
                f"Report {key} is {spec[key]}, requested {expected}; refusing to use another week"
            )
    markets = spec.get("marketplaceIds")
    if marketplace_id and markets and marketplace_id not in markets:
        raise ReportValidationError(f"Report marketplaces {markets} do not include {marketplace_id}")


def parse_report(payload: dict, start: date, end: date, requested_asins: Iterable[str],
                 marketplace_id: str) -> ParseResult:
    if not isinstance(payload, dict) or "dataByAsin" not in payload:
        raise ReportValidationError("Report JSON has no 'dataByAsin' section")
    _check_spec(payload, start, end, marketplace_id)

    requested = set(requested_asins)
    result = ParseResult()
    known_sections = {s for s, _, _, _ in METRIC_FIELDS} | {"startDate", "endDate", "asin"}
    known_by_section: dict[str, set[str]] = {}
    for section, json_name, _, _ in METRIC_FIELDS:
        known_by_section.setdefault(section, set()).add(json_name)
    known_by_section["searchQueryData"].add("searchQuery")

    for item in payload["dataByAsin"] or []:
        reason, pending_missing = None, []
        record: dict[str, Any] = {"marketplace_id": marketplace_id}
        try:
            for key in item:
                if key not in known_sections:
                    result.unknown_fields.add(key)
            for section, names in known_by_section.items():
                for key in item.get(section) or {}:
                    if key not in names:
                        result.unknown_fields.add(f"{section}.{key}")

            row_start, row_end = str(item.get("startDate", ""))[:10], str(item.get("endDate", ""))[:10]
            if row_start != start.isoformat() or row_end != end.isoformat():
                # A mismatched row means Amazon returned a different week: fail loudly.
                raise ReportValidationError(
                    f"Row dated {row_start}..{row_end} in report for {start}..{end}; refusing to substitute"
                )
            record["start_date"], record["end_date"] = start, end

            asin = str(item.get("asin") or "").strip().upper()
            if not ASIN_RE.match(asin):
                raise ValueError(f"invalid ASIN {item.get('asin')!r}")
            if asin not in requested:
                raise ValueError(f"ASIN {asin} was not requested")
            record["asin"] = asin

            query = (item.get("searchQueryData") or {}).get("searchQuery")
            if not isinstance(query, str) or not query.strip():
                raise ValueError("missing search query")
            record["search_query"] = query.strip()
            record["search_query_normalized"] = normalize_query(query)

            missing = []
            for section, json_name, name, kind in METRIC_FIELDS:
                value = (item.get(section) or {}).get(json_name)
                if value is None:
                    missing.append(json_name)
                    record[name] = None
                    if kind == "money":
                        record[f"{name}_currency"] = None
                    continue
                if kind == "int":
                    record[name] = _to_int(value)
                elif kind == "dec":
                    record[name] = _to_dec(value)
                else:
                    record[name] = _to_dec(value.get("amount")) if isinstance(value, dict) else None
                    record[f"{name}_currency"] = value.get("currencyCode") if isinstance(value, dict) else None
                    if record[name] is None:
                        missing.append(json_name)
                if record[name] is not None and record[name] < 0:
                    raise ValueError(f"{json_name} is negative ({value})")

            # Without volume and ASIN impressions the row carries no usable data.
            if record["search_query_volume"] is None or record["asin_impression_count"] is None:
                raise ValueError("incomplete row: missing searchQueryVolume or asinImpressionCount")
            pending_missing = missing
        except ReportValidationError:
            raise
        except (ValueError, TypeError, AttributeError) as exc:
            reason = str(exc)
        if reason:
            result.rejected.append((item, reason))
        else:
            result.records.append(record)
            result.missing_values.update(pending_missing)

    _dedupe(result)
    if result.unknown_fields:
        result.warnings.append(
            "Report contains fields this parser does not map: " + ", ".join(sorted(result.unknown_fields))
        )
    return result


@dataclass
class WeekReport:
    """Every batch of one week, validated and combined: nothing is written before all batches pass."""

    start: date
    end: date
    marketplace_id: str
    requested: list[str]
    report_ids: list[str] = field(default_factory=list)
    parsed: ParseResult = field(default_factory=ParseResult)

    @property
    def returned(self) -> list[str]:
        return sorted({r["asin"] for r in self.parsed.records})

    @property
    def missing(self) -> list[str]:
        """Requested ASINs without a single search-query row this week."""
        return sorted(set(self.requested) - set(self.returned))

    def summary(self) -> list[str]:
        lines = [
            f"week {self.start}..{self.end}, marketplace {self.marketplace_id}",
            f"reports: {', '.join(self.report_ids)}",
            f"ASINs: {len(self.requested)} requested, {len(self.returned)} returned, {len(self.missing)} missing",
            f"search-query rows: {len(self.parsed.records)} valid, {len(self.parsed.rejected)} rejected",
        ]
        if self.missing:
            lines.append(f"missing ASINs: {' '.join(self.missing)}")
        return lines


def combine(results: Iterable[Any], start: date, end: date, marketplace_id: str) -> WeekReport:
    """Parse each downloaded batch (objects with report_id, asins, payload) into one WeekReport.

    A week where no requested ASIN has a single row is refused: Amazon returns rows for
    every ASIN with search traffic, so an empty result means a failed or unpublished week.
    """
    week = None
    for result in results:
        if week is None:
            week = WeekReport(start, end, marketplace_id, [])
        week.requested += result.asins
        week.report_ids.append(result.report_id)
        week.parsed.extend(parse_report(result.payload, start, end, result.asins, marketplace_id))
    if week is None:
        raise ReportValidationError(f"no report was downloaded for {start}..{end}")
    week.requested.sort()
    _dedupe(week.parsed)
    if not week.parsed.records:
        raise ReportValidationError(
            f"the reports for {start}..{end} have no usable search-query rows for any of the "
            f"{len(week.requested)} ASINs; refusing to treat this as an empty week"
        )
    return week


def record_key(record: dict) -> tuple:
    return (
        record["marketplace_id"],
        record["start_date"],
        record["end_date"],
        record["asin"],
        record["search_query_normalized"],
    )


def _dedupe(result: ParseResult) -> None:
    """Drop exact duplicate rows; conflicting duplicates for the same key are an error."""
    seen: dict[tuple, dict] = {}
    unique = []
    for record in result.records:
        key = record_key(record)
        if key in seen:
            if {k: v for k, v in seen[key].items() if k != "search_query"} != {
                k: v for k, v in record.items() if k != "search_query"
            }:
                raise ReportValidationError(f"Conflicting duplicate rows for {key}")
            result.warnings.append(f"Dropped duplicate row for {key}")
            continue
        seen[key] = record
        unique.append(record)
    result.records = unique
