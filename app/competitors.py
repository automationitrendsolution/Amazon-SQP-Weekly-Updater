"""Brand Analytics "Search Query Details" (Seller Central page): the top 10 ASINs for one search query,
next to our ASIN, for one week. Read from the page with the saved Seller Central sign-in.

One page = one (our ASIN, search query, week). The queries per ASIN come from the week's SQP data
(our own report records, else the rows already in 2024BrandAnalytics), highest search volume first.
Each page's table is saved as JSON in data/sellercentral/ before anything is parsed or written.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import random
import re
import time
from pathlib import Path
from urllib.parse import quote

from .config import Settings
from .logging_config import get_logger
from .report import normalize_query

log = get_logger("competitors")

DETAIL_URL = ("https://sellercentral.amazon.com/brand-analytics/dashboard/query-detail?view-id=query-detail-asin-view"
              "&asin={asin}&search-term-freeform={query}&reporting-range=weekly&weekly-week={week_end}&country-id=us")

# Every element of the page, shadow roots included, as rows of cell texts plus the page's plain text.
_CAPTURE_JS = """() => {
  const all = [];
  const walk = root => { for (const el of root.querySelectorAll('*')) { all.push(el); if (el.shadowRoot) walk(el.shadowRoot); } };
  walk(document);
  const text = el => (el.innerText !== undefined && el.innerText !== '' ? el.innerText : el.textContent || '').replace(/\\s+/g, ' ').trim();
  const isRow = el => el.tagName === 'TR' || el.tagName === 'KAT-TABLE-ROW' || el.getAttribute('role') === 'row';
  const isCell = el => ['TD', 'TH', 'KAT-TABLE-CELL'].includes(el.tagName) || ['cell', 'gridcell', 'columnheader'].includes(el.getAttribute('role'));
  const rows = all.filter(isRow).map(r => {
    const kids = [...r.children, ...(r.shadowRoot ? r.shadowRoot.children : [])];
    return kids.filter(isCell).map(c => text(c));
  }).filter(cells => cells.length);
  const pageText = all.filter(el => !el.children.length && el.textContent.trim()).map(el => el.textContent.trim());
  return {url: location.href, title: document.title, rows, pageText};
}"""


ASIN_RE = re.compile(r"^B0[A-Z0-9]{8}$|^[0-9]{9}[0-9X]$")
COMPETITOR_FIELDS = ("product_title", "competitor_asin", "competitor_brand", "price_median", "impressions",
                     "impression_share", "clicks", "click_share")
HEADERS = ("Product Title", "ASIN", "Brand", "Price (median)", "Impressions", "Impression Share", "Clicks", "Click Share")


def number(text: str | None) -> float | None:
    """'348,991' -> 348991, '4.89%' -> 4.89, '$13.99' -> 13.99, '-' or '' -> None."""
    if text is None:
        return None
    cleaned = text.strip().replace(",", "").replace("$", "").replace("%", "")
    if cleaned in ("", "-", "--", "N/A"):
        return None
    return float(cleaned)


def _labelled(texts: list[str], start: int, labels: tuple[str, ...]) -> dict[str, str]:
    """Values of 'Label', ':', 'value' triples after ``start``, first occurrence of each label."""
    found = {}
    for i in range(start, len(texts) - 2):
        if texts[i] in labels and texts[i] not in found and texts[i + 1] == ":":
            found[texts[i]] = texts[i + 2]
        if len(found) == len(labels):
            break
    return found


def parse_capture(data: dict) -> dict:
    """One saved page: the query's totals, our ASIN's numbers, and the competitor rows (top 10)."""
    texts = [t for t in data["pageText"] if not all("" <= ch <= "" for ch in t)]  # drop icon glyphs
    if not any(row[:len(HEADERS)] == list(HEADERS) for row in data["rows"]):
        raise ValueError(f"{data['asin']} / {data['search_query']!r}: table headers changed: {data['rows'][:2]}")
    q_at = texts.index("Query Metrics")
    a_at = texts.index("ASIN Metrics")
    query = _labelled(texts, q_at, ("Search Query", "Query Volume", "Total Impressions", "Total Clicks", "Click Rate"))
    ours = _labelled(texts, a_at, ("ASIN", "Impressions", "Impression Share", "Clicks", "Click Share"))
    if ours.get("ASIN") != data["asin"]:
        raise ValueError(f"page shows ASIN {ours.get('ASIN')!r}, expected {data['asin']}")
    # The table starts after the last header ("Click Share" without a ':' after it).
    body = next(i for i in range(a_at, len(texts)) if texts[i] == "Click Share" and texts[i + 1] != ":"
                and texts[i - 1] == "Clicks")
    competitors = []
    for i in range(body + 1, len(texts) - 6):
        if ASIN_RE.match(texts[i]):
            values = texts[i - 1:i + 7]
            row = dict(zip(COMPETITOR_FIELDS, values))
            competitors.append({
                "rank": len(competitors) + 1,
                "product_title": row["product_title"],
                "competitor_asin": row["competitor_asin"],
                "competitor_brand": None if row["competitor_brand"] in ("-", "") else row["competitor_brand"],
                **{f: number(row[f]) for f in ("price_median", "impressions", "impression_share", "clicks",
                                               "click_share")},
            })
    return {
        "asin": data["asin"],
        "search_query": query.get("Search Query", data["search_query"]),
        "query_volume": number(query.get("Query Volume")),
        "query_total_impressions": number(query.get("Total Impressions")),
        "query_total_clicks": number(query.get("Total Clicks")),
        "query_click_rate": number(query.get("Click Rate")),
        "our_impressions": number(ours.get("Impressions")),
        "our_impression_share": number(ours.get("Impression Share")),
        "our_clicks": number(ours.get("Clicks")),
        "our_click_share": number(ours.get("Click Share")),
        "competitors": competitors,
    }


def queries_for_week(settings: Settings, start: dt.date, end: dt.date, asins: list[str], top: int) -> dict[str, list[str]]:
    """The ``top`` search queries of each ASIN for the week, by search volume."""
    by_asin: dict[str, list[tuple[int, str]]] = {a: [] for a in asins}
    records = settings.report_dir / f"sqp_{start}_{end}_records.csv"
    if records.is_file():
        with records.open(encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if row["asin"] in by_asin:
                    by_asin[row["asin"]].append((int(row["search_query_volume"] or 0), row["search_query"]))
    missing = [a for a, q in by_asin.items() if not q]
    if missing:  # e.g. ASINs whose report is not downloaded: take the queries already in the table
        from .powerbi import connect
        from .schema import quote as q

        with connect(settings) as conn, conn.cursor() as cur:
            marks = ", ".join(["%s"] * len(missing))
            cur.execute(f"SELECT asin, search_query, search_query_volume FROM {q(settings.db_table)} "
                        f"WHERE reporting_date = %s AND asin IN ({marks})", (dt.datetime.combine(end, dt.time()), *missing))
            for row in cur.fetchall():
                text = str(row["search_query"])
                if "?" not in text:  # stored with damaged characters: cannot be searched for
                    by_asin[str(row["asin"]).strip().upper()].append((int(row["search_query_volume"] or 0), text))
            conn.rollback()
    result = {}
    for asin, pairs in by_asin.items():
        seen, chosen = set(), []
        for _, query in sorted(pairs, key=lambda p: -p[0]):
            if normalize_query(query) not in seen:
                seen.add(normalize_query(query))
                chosen.append(query)
            if len(chosen) == top:
                break
        result[asin] = chosen
    return result


TABLE = "BrandAnalytics_Competitor"

# The table's existing columns (inspected 2026-10-27, loaded since 2024 in this format): one row per
# competitor ASIN, keyed in practice by (Year, week_num, ASIN, Keyword, Comp ASIN); no unique index.
LEGACY_COLUMNS = ("Week", "week_num", "Date Range", "Keyword", "Title", "Brand", "Price", "Imp", "Clicks", "ASIN",
                  "Name", "Comp ASIN", "Year", "Index", "imp_share", "click_share")


def date_range(start: dt.date, end: dt.date) -> str:
    """The table's "Date Range": "Feb 01 to Feb 07"."""
    return f"{start:%b %d} to {end:%b %d}"


def week_rows(pages: list[dict], start: dt.date, end: dt.date, names: dict[str, str]) -> list[dict]:
    """Parsed pages as table rows (without Index): one per competitor, duplicates dropped."""
    from .powerbi import week_label

    rows, seen = [], set()
    for parsed in pages:
        for c in parsed["competitors"]:
            key = (parsed["asin"], normalize_query(parsed["search_query"]), c["competitor_asin"])
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "Week": week_label(end), "week_num": float(int(week_label(end).split()[1])),
                "Date Range": date_range(start, end), "Keyword": parsed["search_query"],
                "Title": c["product_title"], "Brand": c["competitor_brand"], "Price": c["price_median"],
                "Imp": c["impressions"], "Clicks": c["clicks"], "ASIN": parsed["asin"], "Name": names.get(parsed["asin"]),
                "Comp ASIN": c["competitor_asin"], "Year": float(end.year),
                "imp_share": c["impression_share"], "click_share": c["click_share"],
            })
    return rows


def check_rows(rows: list[dict], asins: set[str]) -> list[str]:
    """Reasons not to load: anything outside what the page can show."""
    problems = []
    for r in rows:
        where = f"{r['ASIN']} / {r['Keyword']!r} / {r['Comp ASIN']}"
        if r["ASIN"] not in asins:
            problems.append(f"{where}: ASIN not in config/asins.txt")
        if not ASIN_RE.match(r["Comp ASIN"] or ""):
            problems.append(f"{where}: competitor ASIN is not an ASIN")
        if not r["Keyword"]:
            problems.append(f"{where}: empty keyword")
        if not r["Name"]:
            problems.append(f"{where}: no product name for the ASIN")
        for field in ("Imp", "Clicks", "Price"):
            if r[field] is not None and r[field] < 0:
                problems.append(f"{where}: {field} is negative")
        for field in ("imp_share", "click_share"):
            if r[field] is not None and not 0 <= r[field] <= 100:
                problems.append(f"{where}: {field} {r[field]} is not a percentage")
    return problems


def product_names(cur, settings: Settings, asins: list[str]) -> dict[str, str]:
    """Each ASIN's name as on its newest row in this table, else in 2024BrandAnalytics (never changed)."""
    marks = ", ".join(["%s"] * len(asins))
    names = {}
    for table, column in ((settings.db_table, "name"), (TABLE, "Name")):
        cur.execute(f"SELECT t.`ASIN` AS asin, t.`{column}` AS name FROM `{table}` t JOIN (SELECT `ASIN`, MAX(id) AS id "
                    f"FROM `{table}` WHERE `ASIN` IN ({marks}) GROUP BY `ASIN`) newest ON newest.id = t.id", asins)
        for row in cur.fetchall():
            if row["name"]:
                names[str(row["asin"]).strip().upper()] = row["name"]  # this table's own name wins (read last)
    return names


def load_week(settings: Settings, rows: list[dict], end: dt.date) -> dict:
    """Insert the week's rows in one transaction, skipping any (ASIN, keyword, competitor) already stored
    for that week, so a rerun adds nothing. Never updates or deletes existing rows."""
    import pymysql

    from .powerbi import connect

    week_num, year = rows[0]["week_num"], rows[0]["Year"]
    names = ", ".join(f"`{c}`" for c in LEGACY_COLUMNS)
    with connect(settings, read_only=False) as conn:
        try:
            conn.begin()
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) AS n FROM `{TABLE}`")
                total_before = cur.fetchone()["n"]
                cur.execute(f"SELECT `ASIN`, `Keyword`, `Comp ASIN` FROM `{TABLE}` WHERE `Year` = %s AND `week_num` = %s "
                            "FOR UPDATE", (year, week_num))
                stored = {(str(r["ASIN"]).strip().upper(), normalize_query(str(r["Keyword"])), str(r["Comp ASIN"]).strip())
                          for r in cur.fetchall()}
                new = [r for r in rows if (r["ASIN"], normalize_query(r["Keyword"]), r["Comp ASIN"]) not in stored]
                cur.execute(f"SELECT COALESCE(MAX(`Index`), 0) AS i FROM `{TABLE}` FOR UPDATE")
                index = cur.fetchone()["i"]
                values = []
                for r in new:
                    index += 1
                    values.append(tuple(index if c == "Index" else r[c] for c in LEGACY_COLUMNS))
                if values:
                    cur.executemany(f"INSERT INTO `{TABLE}` ({names}) VALUES ({', '.join(['%s'] * len(LEGACY_COLUMNS))})",
                                    values)
                cur.execute(f"SELECT COUNT(*) AS n FROM `{TABLE}`")
                total_after = cur.fetchone()["n"]
                if total_after != total_before + len(values):
                    raise RuntimeError(f"expected {total_before + len(values)} rows after the insert, found {total_after}")
            conn.commit()
        except (pymysql.MySQLError, RuntimeError):
            try:
                conn.rollback()
            except pymysql.MySQLError:
                pass
            raise
    log.info("%s week %s %s: inserted %d rows, %d already stored", TABLE, int(week_num), int(year), len(values),
             len(rows) - len(values))
    return {"inserted": len(values), "already_stored": len(rows) - len(values), "total_before": total_before,
            "total_after": total_after}


def page_file(out_dir: Path, asin: str, query: str) -> Path:
    return out_dir / f"{asin}_{re.sub(r'[^a-z0-9]+', '_', query.lower()).strip('_')[:60]}.json"


def capture_week(settings: Settings, end: dt.date, plan: dict[str, list[str]], out_dir: Path,
                 pause: tuple[float, float] = (4.0, 9.0), max_new: int | None = None) -> list[Path]:
    """Open every (ASIN, query) page once, in one browser session; save each page's table as JSON.

    Pages saved earlier are skipped; ``max_new`` stops after that many newly opened pages (a part).
    """
    from .sellercentral import _close, _open, _settle, _signed_in

    out_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    playwright, context = _open(settings, headless=False)
    try:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://sellercentral.amazon.com/home", wait_until="domcontentloaded")
        _settle(page)
        if not _signed_in(page):  # never log in from here: that would make Amazon send an OTP
            raise RuntimeError("the saved Seller Central sign-in has expired; run --sc-login (OTP) first")
        total = sum(len(q) for q in plan.values())
        done = opened = misses = 0
        for asin, queries in plan.items():
            for query in queries:
                done += 1
                path = page_file(out_dir, asin, query)
                if path.is_file():
                    saved.append(path)
                    continue
                if max_new is not None and opened >= max_new:
                    log.info("part finished: %d pages opened in this run", opened)
                    return saved
                opened += 1
                page.goto(DETAIL_URL.format(asin=asin, query=quote(query), week_end=end.isoformat()),
                          wait_until="domcontentloaded")
                try:
                    page.wait_for_function("""() => /Access Required|Product Title|No data|no results/i
                                                  .test(document.body.innerText)""", timeout=45000)
                except Exception:
                    pass
                _settle(page, 15000)
                data = page.evaluate(_CAPTURE_JS)
                data.update(asin=asin, search_query=query, week_end=end.isoformat(),
                            captured_at=f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}")
                visible = " ".join(data["pageText"]).lower()
                if "access required" in visible:
                    raise RuntimeError("Seller Central shows 'Access Required' for Brand Analytics; stopped")
                if any(k in visible for k in ("captcha", "enter the characters", "unusual activity",
                                              "verify that you are", "/ap/signin")) or "/ap/" in page.url:
                    raise RuntimeError(f"Seller Central asks for a check or sign-in ({page.url.split('?')[0]}); stopped "
                                       "so the account is not pushed further")
                if not any(row[:len(HEADERS)] == list(HEADERS) for row in data["rows"]):
                    misses += 1
                    log.warning("[%d/%d] %s / %r: table did not load; not saved (%d in a row)", done, total, asin,
                                query, misses)
                    if misses >= 5:
                        raise RuntimeError("5 pages in a row without the table; stopped")
                    time.sleep(random.uniform(*pause))
                    continue
                misses = 0
                path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
                saved.append(path)
                log.info("[%d/%d] %s / %r: %d table rows saved", done, total, asin, query, len(data["rows"]))
                time.sleep(random.uniform(*pause))  # not faster than a person paging through
    finally:
        _close(context, playwright)
    return saved
