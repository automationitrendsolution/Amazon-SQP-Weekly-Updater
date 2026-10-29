#!/usr/bin/env python3
"""Amazon SQP weekly updater -- Brand Analytics "Search Query Performance", command-line entry point.

Requests Amazon's weekly SQP report (SP-API, report type
GET_BRAND_ANALYTICS_SEARCH_QUERY_PERFORMANCE_REPORT) for the ASINs in
config/asins.txt, checks it, and writes it to powerbidb.2024BrandAnalytics: rows of the
week that match (ASIN, search query) are updated, new ones inserted, nothing deleted, in one
transaction. ``--inspect`` and dry runs read the table only (read-only session).

Amazon can keep a report queued for hours. Each report ID is kept in data/state/, so a run
that is cut off or times out (exit 6) carries on with the same reports next time, and
batches already downloaded to data/reports/ are not requested again.

Running under cron
------------------
The scheduler (Windows Task Scheduler, deploy/install_task.ps1, or cron, deploy/crontab.example)
calls ``run.py --weekly --apply --quiet`` every Monday at 15:00 IST for the week that ended on
Saturday, with backup runs Tuesday-Friday for when Amazon keeps a report queued for a day or
more. The first run that completes writes the week; later runs find it written and stop at once.
Overlapping runs are refused by an internal lock (exit 11).

Exit codes
----------
    0   success
    1   unexpected error (see logs/sqp_updater.log)
    2   configuration error
    3   MySQL failed, or a write was rolled back (nothing changed)
    5   the period is not a valid or available SQP week (Sunday-Saturday)
    6   SP-API request failed, or the report was not ready in time (the next run resumes it)
    7   the report was for another week/marketplace, or had rejected or conflicting rows
    11  another run holds the lock
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # no __pycache__ folders in the project; set before app/ is imported

from app import __version__, powerbi, report, spapi
from app.asins import AsinList, load_asin_list
from app.config import Settings, load_settings
from app.exceptions import ConfigurationError, DatabaseError, PipelineError
from app.logging_config import append_run, configure_logging, get_logger
from app.schedule import RunLock, check_available, latest_completed_week, parse_date, today_in, validate_week
from app.schema import canonical_fields
from app.schema import quote as schema_quote
from app.spapi import batch_asins

_HELP_EPILOG = """
SQP data (SP-API -> 2024BrandAnalytics):
  python run.py --inspect                                 read-only check of the table and its ASINs
  python run.py --period 2026-10-11 2026-10-17 --dry-run  fetch, validate and preview one week; never writes
  python run.py --period 2026-10-11 2026-10-17 --apply    write that week to MySQL
  python run.py --weekly --apply                          the latest completed, available week (cron)
  python run.py --weekly                                  the same, as a dry run
  python run.py --period 2026-10-11 2026-10-17 --dry-run --refetch   ask Amazon again (ignore downloads)

Competitor data (Seller Central web scraping -> BrandAnalytics_Competitor):
  python run.py --sc-login --terminal                     sign in once on a server (type the OTP)
  python run.py --weekly-scrape                           scrape up to 400 pages of the latest unfinished week
                                                          (then older ones) and store them (cron, 3 times a night)
  python run.py --sc-capture 2026-10-11 2026-10-17        scrape one week by hand (continues where it stopped)
  python run.py --sc-load 2026-10-11 2026-10-17           store a week's scraped pages in the table

Check what is stored (read-only, both tables, week by week):
  python run.py --status

exit codes:
  0 success · 1 unexpected · 2 config · 3 MySQL · 4 Seller Central sign-in / page · 5 bad or unavailable week
  6 SP-API failed · 7 malformed report · 11 locked
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amazon-sqp-weekly-updater",
        description="Fetch Amazon SQP weekly data and upsert it into MySQL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_HELP_EPILOG,
    )
    parser.add_argument("--version", action="version", version=f"amazon-sqp-weekly-updater {__version__}")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--inspect", action="store_true", help="Inspect table and ASINs (read-only).")
    modes.add_argument("--status", action="store_true",
                       help="Read-only: for each week since SC_COMPETITORS_FROM, what is stored in 2024BrandAnalytics "
                            "(SP-API) and BrandAnalytics_Competitor (scraping), and whether it is complete.")
    modes.add_argument("--period", nargs=2, metavar=("START", "END"), help="Sunday and Saturday, YYYY-MM-DD.")
    modes.add_argument("--weekly", action="store_true", help="Process the latest completed, available week.")
    modes.add_argument("--sc-login", action="store_true",
                       help="Seller Central: sign in once in Chrome (email/password from .env, OTP by you).")
    modes.add_argument("--sc-inspect", metavar="URL",
                       help="Seller Central: open URL with the saved sign-in; save a screenshot and its buttons.")
    modes.add_argument("--sc-capture", nargs=2, metavar=("START", "END"),
                       help="Seller Central: save the Search Query Details pages (top competitors) of the week's "
                            "keywords to data/sellercentral/; continues where the last part stopped. No database.")
    modes.add_argument("--weekly-scrape", action="store_true",
                       help="Cron: web-scrape the competitor data (Seller Central Search Query Details) of the "
                            "latest unfinished week, up to --max-pages pages, and store it in "
                            "BrandAnalytics_Competitor. Same as --sc-night.")
    modes.add_argument("--sc-night", action="store_true",
                       help="Scheduled: the latest week in 2024BrandAnalytics whose Search Query Details pages are "
                            "not all saved yet (then older ones, back to SC_COMPETITORS_FROM): save up to "
                            "--max-pages of them, then load the week's saved pages into BrandAnalytics_Competitor "
                            "(no duplicates).")
    modes.add_argument("--sc-load", nargs=2, metavar=("START", "END"),
                       help="Load the week's saved Search Query Details pages into BrandAnalytics_Competitor "
                            "(rows already there are skipped; nothing updated or deleted).")
    parser.add_argument("--terminal", action="store_true",
                        help="With --sc-login: no window (a server); type the OTP in this terminal.")
    parser.add_argument("--top", type=int, default=100, metavar="N",
                        help="With --sc-capture: keywords per ASIN, highest search volume first (default 100 = all).")
    parser.add_argument("--max-pages", type=int, default=400, metavar="N",
                        help="With --sc-capture: pages to open in this run (default 400, about 3 hours).")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--dry-run", action="store_true", help="Fetch, validate and preview; never writes.")
    action.add_argument("--apply", action="store_true", help="Write the validated changes to MySQL.")
    parser.add_argument("--refetch", action="store_true",
                        help="Ask Amazon again: ignore downloaded reports and pending report IDs of the week.")
    parser.add_argument("--downloaded-only", action="store_true",
                        help="With --period: use only the reports already downloaded; ASINs whose report is still "
                             "pending are left out (run again later for them). Amazon is not called.")
    parser.add_argument("--no-insert", action="store_true",
                        help="With --period: change existing rows only; new search queries are listed, not inserted.")
    parser.add_argument("--env-file", type=Path, metavar="PATH", help="Alternative .env file.")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Override LOG_LEVEL.")
    parser.add_argument("--quiet", action="store_true", help="Console shows warnings and errors only (cron).")
    parser.add_argument("--no-lock", action="store_true", help="Skip the single-run lock (supervised runs only).")
    return parser


def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if (args.inspect or args.status or args.sc_login or args.sc_inspect or args.sc_capture or args.sc_load
            or args.sc_night or args.weekly_scrape) and \
            (args.dry_run or args.apply):
        parser.error("--inspect and the --sc-* modes cannot be combined with --dry-run/--apply")
    if args.period and not (args.dry_run or args.apply):
        parser.error("--period requires --dry-run or --apply")
    if (args.downloaded_only or args.no_insert) and not args.period:
        parser.error("--downloaded-only and --no-insert are for a manual --period run only")
    if args.downloaded_only and args.refetch:
        parser.error("--downloaded-only cannot be combined with --refetch")
    if args.weekly and not (args.dry_run or args.apply):
        # Scheduled runs must say --apply explicitly; anything else previews.
        args.dry_run = True


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)  # the downloaded reports stay private to this user
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_args(parser, args)
    if args.weekly_scrape:  # the cron name of --sc-night
        args.sc_night = True
    started = dt.datetime.now()
    try:
        settings = load_settings(args.env_file)
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return exc.exit_code
    settings.ensure_directories()
    level = args.log_level or settings.log_level
    configure_logging(settings.log_dir, level=level, console_level="WARNING" if args.quiet else level)
    logger = get_logger("cli")

    code, summary = 1, ""
    try:
        code, summary = _run(args, settings, logger)
    except PipelineError as exc:
        logger.error("%s: %s", type(exc).__name__, exc)
        code, summary = exc.exit_code, f"{type(exc).__name__}: {exc}"
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        code = 130
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected failure")
        code, summary = 1, f"{type(exc).__name__}: {exc}"
    append_run(settings.log_dir, f"{started:%Y-%m-%d %H:%M:%S} exit={code} {' '.join(argv or sys.argv[1:])} | {summary}")
    return code


def _run(args: argparse.Namespace, settings: Settings, logger) -> tuple[int, str]:
    if args.inspect:
        code, lines = powerbi.inspection(settings, load_asin_list(settings.asin_file))
        print("\n".join(lines))
        return code, "inspected" if code == 0 else "no ASIN column"
    if args.status:
        return _status(settings)
    if args.sc_login:
        from app import sellercentral

        url = sellercentral.login(settings, terminal=args.terminal)
        print(f"Seller Central: signed in ({url.split('?')[0]}). The sign-in is saved for the next runs.")
        return 0, "seller central signed in"
    if args.sc_inspect:
        from app import sellercentral

        shot, listing = sellercentral.inspect(settings, args.sc_inspect)
        print(f"Screenshot: {shot}\nButtons:    {listing}")
        return 0, "seller central page inspected"
    if args.sc_capture or args.sc_load or args.sc_night:
        with RunLock(settings.log_dir / ".sellercentral.lock"):  # one Seller Central run at a time
            if args.sc_night:
                return _competitors_night(args, settings, logger)
            return _competitors(args, settings, logger)

    lock = RunLock(settings.log_dir / ".run.lock") if not args.no_lock else None
    if lock:
        lock.__enter__()
    try:
        return _update_week(args, settings, logger)
    finally:
        if lock:
            lock.__exit__(None, None, None)


def _competitors_night(args: argparse.Namespace, settings: Settings, logger) -> tuple[int, str]:
    """One scheduled part: the newest unfinished week (latest in 2024BrandAnalytics first, then older ones back
    to SC_COMPETITORS_FROM) gets up to --max-pages new pages, then its saved pages are loaded. The page limit
    and the load happen even if Amazon stops the part early (pages are kept)."""
    from app import competitors

    asins = load_asin_list(settings.asin_file).asins
    with powerbi.connect(settings) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT MAX(reporting_date) AS d FROM {schema_quote(settings.db_table)}")
        latest = cur.fetchone()["d"].date()
        conn.rollback()
    first = settings.sc_competitors_from + dt.timedelta(days=6)
    for end in (latest - dt.timedelta(days=7 * i) for i in range((latest - first).days // 7 + 1)):
        start = end - dt.timedelta(days=6)
        plan = {a: q for a, q in competitors.queries_for_week(settings, start, end, asins, top=args.top).items() if q}
        folder = settings.data_dir / "sellercentral" / end.isoformat()
        missing = sum(not competitors.page_file(folder, a, q).is_file() for a, qs in plan.items() for q in qs)
        if missing:
            break
        if _loaded_pages(settings, end) < len(list(folder.glob("*.json"))):  # saved but not (all) stored yet
            logger.info("Search Query Details: week %s..%s is scraped but not fully stored; storing it", start, end)
            args.sc_capture, args.sc_load = None, [start.isoformat(), end.isoformat()]
            code, summary = _competitors(args, settings, logger)
            return code, f"sc-night {summary}"
    else:
        logger.info("Search Query Details: every week from %s to %s is scraped and stored; nothing to do",
                    first, latest)
        return 0, f"sc-night: all weeks up to {latest} scraped and stored"
    logger.info("Search Query Details: week %s..%s, %d pages left; this part opens up to %d", start, end, missing,
                args.max_pages)
    args.sc_capture, args.sc_load = [start.isoformat(), end.isoformat()], None
    try:
        code, summary = _competitors(args, settings, logger)
    finally:  # whatever was saved, also when Amazon stopped the part, goes into the table
        args.sc_capture, args.sc_load = None, [start.isoformat(), end.isoformat()]
        load_code, load_summary = _competitors(args, settings, logger)
    return max(code, load_code), f"sc-night {summary}; {load_summary}"


def _status(settings: Settings) -> tuple[int, str]:
    """--status (read-only): each week from the newest Amazon has published back to SC_COMPETITORS_FROM, with
    what 2024BrandAnalytics and BrandAnalytics_Competitor hold for it and whether that week is complete."""
    import json

    from app import competitors

    asins = load_asin_list(settings.asin_file).asins
    table, comp = schema_quote(settings.db_table), f"`{competitors.TABLE}`"
    first = settings.sc_competitors_from + dt.timedelta(days=6)
    _, newest = latest_completed_week(today_in(settings.timezone), settings.availability_lag_days)
    weeks = [newest - dt.timedelta(days=7 * i) for i in range((newest - first).days // 7 + 1)]
    lines, open_items = [], 0
    for end in weeks:
        start, day = end - dt.timedelta(days=6), dt.datetime.combine(end, dt.time())
        label = powerbi.week_label(end)
        with powerbi.connect(settings) as conn, conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS n, COUNT(DISTINCT asin) AS a FROM {table} WHERE reporting_date = %s", (day,))
            sqp = cur.fetchone()
            cur.execute(f"SELECT COUNT(*) AS d FROM (SELECT 1 FROM {table} WHERE reporting_date = %s "
                        "GROUP BY asin, search_query HAVING COUNT(*) > 1) x", (day,))
            sqp_dups = cur.fetchone()["d"]
            week_num = int(label.split()[1])
            cur.execute(f"SELECT COUNT(*) AS n, COUNT(DISTINCT `ASIN`) AS a, COUNT(DISTINCT `ASIN`, `Keyword`) AS k "
                        f"FROM {comp} WHERE `Year` = %s AND `week_num` = %s", (end.year, week_num))
            cmp = cur.fetchone()
            cur.execute(f"SELECT COUNT(*) AS d FROM (SELECT 1 FROM {comp} WHERE `Year` = %s AND `week_num` = %s "
                        "GROUP BY `ASIN`, `Keyword`, `Comp ASIN` HAVING COUNT(*) > 1) x", (end.year, week_num))
            cmp_dups = cur.fetchone()["d"]
            conn.rollback()

        state = spapi.load_state(spapi.state_file(settings.state_dir, start, end))
        pending = [b for b in state.get("batches", {}).values() if not b.get("document_id")]
        if state.get("written"):
            sqp_verdict = f"OK - written and verified {state['written']['at']}"
        elif state.get("manual"):
            sqp_verdict = f"OK - written by hand {state['manual']['at']} ({state['manual']['how']})"
        elif pending:
            sqp_verdict = f"WAITING - {len(pending)} of {len(state['batches'])} report(s) still in Amazon's queue"
        elif sqp["n"]:
            sqp_verdict = "OK - in the table (written before this server's records)"
        else:
            sqp_verdict = "NOT YET - not requested (the Monday cron run asks for it)"
        if sqp_dups:
            sqp_verdict += f"; WARNING {sqp_dups} duplicated (ASIN, query)"
        open_items += not sqp_verdict.startswith("OK") or bool(sqp_dups)

        plan = competitors.queries_for_week(settings, start, end, asins, top=100) if sqp["n"] else {}
        folder = settings.data_dir / "sellercentral" / end.isoformat()
        planned = sum(len(q) for q in plan.values())
        saved = sum(competitors.page_file(folder, a, q).is_file() for a, qs in plan.items() for q in qs)
        files = len(list(folder.glob("*.json"))) if folder.is_dir() else 0
        try:
            marker = json.loads(_loaded_marker(settings, end).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            marker = {}
        if not planned:
            cmp_verdict = "NOT YET - needs the week in 2024BrandAnalytics first"
        elif saved < planned:
            cmp_verdict = f"SCRAPING - {saved} of {planned} pages saved, {planned - saved} left"
        elif marker.get("loaded_pages", 0) < files:
            cmp_verdict = "SCRAPED - not all stored yet (the next --weekly-scrape stores it)"
        else:
            cmp_verdict = f"OK - all {planned} pages scraped and stored"
        if marker and cmp["n"] < marker.get("rows", 0):
            cmp_verdict += f"; WARNING table has {cmp['n']} rows, the last load had {marker['rows']}"
        if cmp_dups:
            cmp_verdict += f"; WARNING {cmp_dups} duplicated (ASIN, keyword, competitor)"
        open_items += not cmp_verdict.startswith("OK") or "WARNING" in cmp_verdict

        last_load = f", last stored {marker['at']} ({marker['loaded_pages']} pages)" if marker else ""
        lines += [
            f"{label} {end.year}  {start}..{end}",
            f"  2024BrandAnalytics (SP-API):  {sqp['n']} rows, {sqp['a']} of {len(asins)} ASINs, {sqp_dups} duplicates",
            f"    {sqp_verdict}",
            f"  BrandAnalytics_Competitor:    {cmp['n']} rows, {cmp['a']} ASINs, {cmp['k']} keywords, "
            f"{cmp_dups} duplicates{last_load}",
            f"    {cmp_verdict}",
            "",
        ]
    lines.append("All weeks complete." if not open_items else f"{open_items} item(s) not complete yet (see above).")
    print("\n".join(lines))
    return 0, f"status: {len(weeks)} week(s), {open_items} not complete"


def _loaded_marker(settings: Settings, end: dt.date) -> Path:
    return settings.state_dir / f"competitors_{end.isoformat()}.json"


def _loaded_pages(settings: Settings, end: dt.date) -> int:
    """How many saved pages of the week were stored in BrandAnalytics_Competitor by the last successful load."""
    import json

    try:
        return int(json.loads(_loaded_marker(settings, end).read_text(encoding="utf-8"))["loaded_pages"])
    except (OSError, ValueError, KeyError):
        return 0


def _competitors(args: argparse.Namespace, settings: Settings, logger) -> tuple[int, str]:
    """--sc-capture: save Search Query Details pages (a part at a time); --sc-load: put them in the table."""
    import json

    from app import competitors

    start, end = (parse_date(d) for d in (args.sc_capture or args.sc_load))
    validate_week(start, end)
    folder = settings.data_dir / "sellercentral" / end.isoformat()
    asins = load_asin_list(settings.asin_file).asins
    plan = competitors.queries_for_week(settings, start, end, asins, top=args.top)
    plan = {a: q for a, q in plan.items() if q}
    total = sum(len(q) for q in plan.values())

    if args.sc_capture:
        competitors.capture_week(settings, end, plan, folder, max_new=args.max_pages)
        saved = sum(competitors.page_file(folder, a, q).is_file() for a, qs in plan.items() for q in qs)
        left = total - saved
        print(f"{start}..{end}: {saved} of {total} pages saved (top {args.top} keywords, {len(plan)} ASINs); "
              f"{left} left" + (" - run --sc-capture again for the next part" if left else " - all done"))
        return 0, f"sc-capture {start}..{end} saved={saved}/{total} left={left}"

    pages, problems = [], []
    for path in sorted(folder.glob("*.json")):
        try:
            pages.append(competitors.parse_capture(json.loads(path.read_text(encoding="utf-8"))))
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{path.name}: {exc}")
    if problems:
        for line in problems[:30]:
            logger.error("%s", line)
        raise PipelineError(f"{len(problems)} saved page(s) could not be read; nothing loaded")
    with powerbi.connect(settings) as conn, conn.cursor() as cur:
        names = competitors.product_names(cur, settings, sorted({p["asin"] for p in pages}))
        conn.rollback()
    rows = competitors.week_rows(pages, start, end, names)
    if not rows:
        print(f"{start}..{end}: no saved pages to load")
        return 0, f"sc-load {start}..{end} nothing saved yet"
    problems = competitors.check_rows(rows, set(asins))
    if problems:
        for line in problems[:30]:
            logger.error("%s", line)
        raise PipelineError(f"{len(problems)} row problem(s); nothing loaded")
    result = competitors.load_week(settings, rows, end)
    _loaded_marker(settings, end).write_text(json.dumps({
        "loaded_pages": len(pages), "rows": len(rows), "inserted": result["inserted"],
        "at": f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}"}), encoding="utf-8")
    print(f"{start}..{end}: {len(pages)} pages, {len(rows)} rows -> {competitors.TABLE}: inserted {result['inserted']}, "
          f"already there {result['already_stored']}; table {result['total_before']} -> {result['total_after']} rows")
    return 0, f"sc-load {start}..{end} inserted={result['inserted']} already={result['already_stored']}"


def _week(args: argparse.Namespace, settings: Settings, logger) -> tuple[dt.date, dt.date]:
    """--period as given (a Sunday-Saturday week), or with --weekly the latest available one."""
    today = today_in(settings.timezone)
    if args.weekly:
        start, end = latest_completed_week(today, settings.availability_lag_days)
    else:
        start, end = parse_date(args.period[0]), parse_date(args.period[1])
        validate_week(start, end)
    warning = check_available(end, today, settings.availability_lag_days)
    if warning:
        logger.warning(warning)
    return start, end


def _asin_set(args: argparse.Namespace, settings: Settings, logger) -> AsinList:
    """The ASINs to request; --apply needs the unique count to equal EXPECTED_ASIN_COUNT."""
    asin_list = load_asin_list(settings.asin_file)
    for line in asin_list.summary(settings.expected_asin_count):
        logger.info("%s", line)
    problems = []
    if len(asin_list.asins) != settings.expected_asin_count:
        problems.append(f"{len(asin_list.asins)} unique ASINs in {asin_list.path.name}, "
                        f"EXPECTED_ASIN_COUNT is {settings.expected_asin_count}")
    if asin_list.invalid:
        problems.append(f"{len(asin_list.invalid)} unreadable line(s) in {asin_list.path.name}")
    for problem in problems:
        if args.apply:
            raise ConfigurationError(f"{problem}; make the ASIN set explicit before writing. Nothing was fetched.")
        logger.warning("%s (a dry run continues; --apply would stop)", problem)
    return asin_list


def _update_week(args: argparse.Namespace, settings: Settings, logger) -> tuple[int, str]:
    start, end = _week(args, settings, logger)
    state_file = spapi.state_file(settings.state_dir, start, end)
    previous = spapi.load_state(state_file)
    written, manual = previous.get("written"), previous.get("manual")
    if args.weekly and args.apply and written and not args.refetch:
        logger.info("%s..%s was already written on %s; nothing to do", start, end, written["at"])
        return 0, f"{start}..{end} already written"
    if args.weekly and args.apply and manual:
        logger.warning("%s..%s was written by hand (%s on %s); scheduled runs leave it alone: use --period",
                       start, end, manual["how"], manual["at"])
        return 0, f"{start}..{end} written by hand, left alone"
    asin_list = _asin_set(args, settings, logger)
    logger.info("requesting %s..%s, marketplace %s, %d ASINs in %d report(s)", start, end, settings.marketplace_id,
                len(asin_list.asins), len(batch_asins(asin_list.asins)))
    client = spapi.SpApiClient(settings)
    results = client.fetch_sqp(asin_list.asins, start, end, save_dir=settings.report_dir, state_file=state_file,
                               refetch=args.refetch, downloaded_only=args.downloaded_only)
    week = report.combine(results, start, end, settings.marketplace_id)
    left_out = sorted(set(asin_list.asins) - set(week.requested))
    if left_out:
        logger.warning("left out (report not downloaded yet, run again later): %d ASINs: %s", len(left_out),
                       " ".join(left_out))

    for line in week.summary():
        logger.info("%s", line)
    for item, reason in week.parsed.rejected[:50]:
        logger.error("rejected: %s / %r: %s", item.get("asin"), (item.get("searchQueryData") or {}).get("searchQuery"),
                     reason)
    for warning in [*week.parsed.warnings[:50], week.parsed.missing_summary()]:
        if warning:
            logger.warning("%s", warning)
    records_file = _save_records(week, settings)
    print("\n".join(["", *week.summary(), f"records: {records_file}"]))

    summary = (f"{start}..{end} reports={','.join(week.report_ids)} rows={len(week.parsed.records)} "
               f"rejected={len(week.parsed.rejected)} missing={len(week.missing)}")
    if week.parsed.rejected:
        logger.error("%d row(s) rejected: the week is not written (see above)", len(week.parsed.rejected))
        return 7, summary + " not written"

    if args.dry_run:
        plan = powerbi.plan_week(settings, week)
        plan_file = _save_plan(plan, week, settings)
        notes = [f"--no-insert: the {len(plan.inserts)} insert(s) will be skipped"] if args.no_insert else []
        print("\n".join([*plan.summary(), *notes, f"plan: {plan_file}",
                         f"dry run: nothing written to {settings.db_name}.{settings.db_table}"]))
        for line in plan.summary():
            logger.info("%s", line)
        return 0, f"{summary} dry-run insert={len(plan.inserts)} update={len(plan.updates)} repair={len(plan.repairs)}"

    insert = not args.no_insert
    plan = powerbi.write_week(settings, week, insert=insert)
    check = powerbi.plan_week(settings, week)  # read back: nothing may be left to change
    left = len(check.updates) + len(check.repairs) + (len(check.inserts) if insert else 0)
    if left:
        raise DatabaseError(f"{start}..{end} was written, but reading it back still shows {left} change(s); "
                            f"see {_save_plan(check, week, settings)}")
    inserted = len(plan.inserts) if insert else 0
    if args.no_insert or args.downloaded_only:  # a partial week by hand: the weekly runs must not complete it
        state = spapi.load_state(state_file)
        how = " ".join(flag for flag, on in (("--no-insert", args.no_insert),
                                             ("--downloaded-only", args.downloaded_only)) if on)
        state["manual"] = {"at": f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}", "how": how}
        spapi.save_state(state_file, state)
    if insert and not left_out:  # only a complete week counts as written for the weekly runs
        state = spapi.load_state(state_file)
        state["written"] = {"at": f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}", "inserted": inserted,
                            "updated": len(plan.updates), "repaired": len(plan.repairs),
                            "rows": len(week.parsed.records), "reports": week.report_ids}
        spapi.save_state(state_file, state)
    notes = [f"--no-insert: {len(plan.inserts)} new search queries not inserted"] if not insert else []
    notes += [f"left out until their report is downloaded: {len(left_out)} ASINs"] if left_out else []
    print("\n".join([*plan.summary(), *notes, f"written to {settings.db_name}.{settings.db_table}; read back: "
                     f"{check.existing_rows} rows for {check.reporting_date:%Y-%m-%d}, nothing left to change"]))
    return 0, (f"{summary} inserted={inserted} updated={len(plan.updates)} repaired={len(plan.repairs)} "
               f"unchanged={plan.unchanged}" + (f" left_out={len(left_out)}" if left_out else ""))


def _save_records(week: report.WeekReport, settings: Settings) -> Path:
    """The validated rows of the week as CSV, for reviewing a dry run."""
    path = settings.report_dir / f"sqp_{week.start}_{week.end}_records.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=canonical_fields())
        writer.writeheader()
        writer.writerows(week.parsed.records)
    return path


def _save_plan(plan: powerbi.WeekPlan, week: report.WeekReport, settings: Settings) -> Path:
    """Every planned change, one line per value: action, row id, ASIN, query, column, old, new."""
    path = settings.report_dir / f"sqp_{week.start}_{week.end}_plan.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["action", "id", "asin", "search_query", "column", "old", "new"])
        for row_id, changes, before in plan.updates:
            for column, value in changes.items():
                writer.writerow(["update", row_id, "", "", column, before[column], value])
        for row in plan.inserts:
            writer.writerow(["insert", "", row["asin"], row["search_query"], "", "", ""])
        for asin, query in plan.ambiguous:
            writer.writerow(["skip: several rows", "", asin, query, "", "", ""])
        for row_id, stored, values in plan.repairs:
            writer.writerow(["repair: broken row", row_id, "", values["search_query"], "search_query", stored,
                             values["search_query"]])
        for row_id, stored, amazon in plan.text_differs:
            writer.writerow(["unchanged: matched by numbers", row_id, "", amazon, "search_query", stored, "(kept)"])
    return path


if __name__ == "__main__":
    sys.exit(main())
