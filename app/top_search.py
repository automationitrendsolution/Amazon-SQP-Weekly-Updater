"""Brand Analytics "Top Search Terms" (SP-API, one day at a time) -> powerbidb.day_wise_top_search.

Amazon's daily report (GET_BRAND_ANALYTICS_SEARCH_TERMS_REPORT, reportPeriod DAY) lists every search
term on Amazon.com with its top 3 clicked products: a few hundred MB gzipped. It is read as a stream
and only the brand's search terms are kept: those where one of the top 3 clicked products is ours
(config/asins.txt) or its title starts with the brand name (TOP_SEARCH_BRAND, default EcoNour).

How the table is stored (inspected 2026-11-04): one row per search term and day, ``reporting_date``
= the day, product shares in percent (9.44, not 0.0944), ``id`` auto-increment, no unique key. Rows up
to 2026-04-28 came from Seller Central downloads filtered by brand; the SP-API report has no brand or
category columns, so ``top_clicked_brand_*`` and ``top_clicked_category_*`` stay empty (NULL) on the
days loaded from here. A day is written in one transaction: search terms already stored for that day
are skipped, nothing is updated or deleted.

Each day's filtered rows are kept in data/top_search/ and its report ID in data/state/, so a run that
stops (or a day Amazon has not published yet) carries on next time without asking Amazon again.
"""
from __future__ import annotations

import codecs
import datetime as dt
import json
import time
import zlib
from pathlib import Path
from typing import Iterable, Iterator

from .config import Settings
from .exceptions import MalformedReport
from .logging_config import get_logger
from .spapi import REPORTS_PATH, ReportFailed, SpApiClient, load_state, save_state

log = get_logger("top_search")

REPORT_TYPE = "GET_BRAND_ANALYTICS_SEARCH_TERMS_REPORT"
TABLE = "day_wise_top_search"
FIRST_DAY = dt.date(2026, 4, 29)  # the table holds Seller Central downloads up to 2026-04-28
NOT_AVAILABLE = "not available yet"  # Amazon's FATAL reason for a day it has not published
COLUMNS = ("search_frequency_rank", "search_term",
           "top_clicked_brand_1", "top_clicked_brand_2", "top_clicked_brand_3",
           "top_clicked_category_1", "top_clicked_category_2", "top_clicked_category_3",
           *(f"top_clicked_product_{i}_{c}" for i in (1, 2, 3)
             for c in ("asin", "title", "click_share", "conversion_share")),
           "reporting_date")


class NotPublished(Exception):
    """Amazon has no data for the day yet: stop, the next run asks again."""


def iter_records(chunks: Iterable[bytes], gzipped: bool) -> Iterator[dict]:
    """The report's dataByDepartmentAndSearchTerm records, one by one, without holding the file in memory."""
    inflate = zlib.decompressobj(16 + zlib.MAX_WBITS) if gzipped else None
    text = codecs.getincrementaldecoder("utf-8")("replace")
    decoder = json.JSONDecoder()
    buf, started = "", False
    for chunk in chunks:
        buf += text.decode(inflate.decompress(chunk) if inflate else chunk)
        if not started:
            i = buf.find('"dataByDepartmentAndSearchTerm"')
            j = buf.find("[", i) if i >= 0 else -1
            if j < 0:
                continue
            buf, started = buf[j + 1:], True
        pos = 0
        while True:
            while pos < len(buf) and buf[pos] in " \r\n\t,":
                pos += 1
            if pos >= len(buf) or buf[pos] == "]":
                break
            try:
                record, pos_next = decoder.raw_decode(buf, pos)
            except json.JSONDecodeError:
                break  # the record continues in the next chunk
            yield record
            pos = pos_next
        buf = buf[pos:]
    if not started:
        raise MalformedReport("Top Search Terms report has no dataByDepartmentAndSearchTerm list")


def brand_terms(records: Iterable[dict], asins: set[str], brand: str) -> list[list[dict]]:
    """Each search term (its 1-3 records, which Amazon lists together) where one of the top 3 clicked
    products is in ``asins`` or its title starts with ``brand``."""
    brand = brand.strip().lower()
    kept, group = [], []

    def ours(record: dict) -> bool:
        return (record.get("clickedAsin") in asins
                or bool(brand) and (record.get("clickedItemName") or "").strip().lower().startswith(brand))

    for record in records:
        if group and record.get("searchTerm") != group[0].get("searchTerm"):
            if any(map(ours, group)):
                kept.append(group)
            group = []
        group.append(record)
    if group and any(map(ours, group)):
        kept.append(group)
    return kept


def _percent(share) -> float | None:
    return None if share is None else round(float(share) * 100, 2)


def table_rows(groups: list[list[dict]], day: dt.date) -> list[dict]:
    """One table row per search term (products by click share rank; shares in percent)."""
    rows = []
    for group in groups:
        row = dict.fromkeys(COLUMNS)
        row.update(search_frequency_rank=int(group[0]["searchFrequencyRank"]), search_term=group[0]["searchTerm"],
                   reporting_date=day.isoformat())
        for record in sorted(group, key=lambda r: r.get("clickShareRank") or 9)[:3]:
            i = int(record.get("clickShareRank") or 0)
            if i not in (1, 2, 3):
                raise MalformedReport(f"{day} {record.get('searchTerm')!r}: clickShareRank {i}")
            row[f"top_clicked_product_{i}_asin"] = record.get("clickedAsin")
            row[f"top_clicked_product_{i}_title"] = record.get("clickedItemName")
            row[f"top_clicked_product_{i}_click_share"] = _percent(record.get("clickShare"))
            row[f"top_clicked_product_{i}_conversion_share"] = _percent(record.get("conversionShare"))
        rows.append(row)
    return sorted(rows, key=lambda r: r["search_frequency_rank"])


def days_due(stored: set[dt.date], first: dt.date, last: dt.date) -> list[dt.date]:
    """Days from ``first`` to ``last`` with no rows in the table, oldest first."""
    return [d for d in (first + dt.timedelta(days=i) for i in range((last - first).days + 1)) if d not in stored]


# --------------------------------------------------------------------------- Amazon

def rows_file(settings: Settings, day: dt.date) -> Path:
    return settings.data_dir / "top_search" / f"top_search_{day.isoformat()}.json"


def _state_file(settings: Settings, day: dt.date) -> Path:
    return settings.state_dir / f"top_search_{day.isoformat()}.json"


def request_day(client: SpApiClient, settings: Settings, day: dt.date) -> str:
    """The day's report ID: the one asked for before (data/state/), else a new request."""
    state_path = _state_file(settings, day)
    report_id = load_state(state_path).get("report_id")
    if report_id:
        return report_id
    body = {"reportType": REPORT_TYPE, "marketplaceIds": [settings.marketplace_id],
            "dataStartTime": day.isoformat(), "dataEndTime": day.isoformat(), "reportOptions": {"reportPeriod": "DAY"}}
    report_id = client._api("POST", f"{REPORTS_PATH}/reports", data=json.dumps(body))["reportId"]
    save_state(state_path, {"report_id": report_id})
    log.info("Top Search Terms %s: requested report %s", day, report_id)
    return report_id


def fetch_day(client: SpApiClient, settings: Settings, day: dt.date, asins: set[str], brand: str,
              deadline: float | None = None) -> list[dict]:
    """Wait for the day's report, keep the brand's search terms and save them to data/top_search/."""
    import requests

    path = rows_file(settings, day)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    report_id = request_day(client, settings, day)
    try:
        document_id = client.wait_for_report(report_id, deadline)
    except ReportFailed as exc:
        save_state(_state_file(settings, day), {})  # the next run asks again
        if NOT_AVAILABLE in str(exc):
            raise NotPublished(str(day)) from None
        raise
    meta = client._api("GET", f"{REPORTS_PATH}/documents/{document_id}")
    started = time.monotonic()
    with requests.get(meta["url"], stream=True, timeout=300) as resp:  # pre-signed URL: no SP-API headers
        resp.raise_for_status()
        records = iter_records(resp.iter_content(1 << 20), meta.get("compressionAlgorithm") == "GZIP")
        rows = table_rows(brand_terms(_checked(records, day), asins, brand), day)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    log.info("Top Search Terms %s: %d search terms kept (report %s, read in %.0fs)", day, len(rows), report_id,
             time.monotonic() - started)
    return rows


def _checked(records: Iterator[dict], day: dt.date) -> Iterator[dict]:
    count = 0
    for count, record in enumerate(records, 1):
        if "searchTerm" not in record or "searchFrequencyRank" not in record:
            raise MalformedReport(f"{day}: record {count} has no searchTerm/searchFrequencyRank")
        yield record
    if count == 0:
        raise MalformedReport(f"{day}: the report has no search terms")


# --------------------------------------------------------------------------- MySQL

def stored_days(settings: Settings, first: dt.date) -> set[dt.date]:
    from .powerbi import connect

    with connect(settings) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT DISTINCT reporting_date AS d FROM `{TABLE}` WHERE reporting_date >= %s", (first,))
        return {r["d"] for r in cur.fetchall()}


def load_day(settings: Settings, rows: list[dict], day: dt.date) -> dict:
    """Insert the day's rows in one transaction, skipping search terms already stored for that day."""
    import pymysql

    from .powerbi import connect

    names = ", ".join(f"`{c}`" for c in COLUMNS)
    with connect(settings, read_only=False) as conn:
        try:
            conn.begin()
            with conn.cursor() as cur:
                cur.execute(f"SELECT search_term FROM `{TABLE}` WHERE reporting_date = %s FOR UPDATE", (day,))
                stored = {str(r["search_term"]).strip().lower() for r in cur.fetchall()}
                new = [r for r in rows if r["search_term"].strip().lower() not in stored]
                if new:
                    cur.executemany(f"INSERT INTO `{TABLE}` ({names}) VALUES ({', '.join(['%s'] * len(COLUMNS))})",
                                    [tuple(r[c] for c in COLUMNS) for r in new])
                cur.execute(f"SELECT COUNT(*) AS n FROM `{TABLE}` WHERE reporting_date = %s", (day,))
                after = cur.fetchone()["n"]
                if after != len(stored) + len(new):
                    raise RuntimeError(f"expected {len(stored) + len(new)} rows for {day}, found {after}")
            conn.commit()
        except (pymysql.MySQLError, RuntimeError):
            try:
                conn.rollback()
            except pymysql.MySQLError:
                pass
            raise
    log.info("%s %s: inserted %d rows, %d already stored", TABLE, day, len(new), len(rows) - len(new))
    return {"inserted": len(new), "already_stored": len(rows) - len(new)}
