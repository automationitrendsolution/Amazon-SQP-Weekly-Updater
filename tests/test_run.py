"""run.py without Amazon or MySQL: options, exit codes, the run history line."""
from __future__ import annotations

import datetime as dt

import pytest

import run
from app import spapi
from app.spapi import ReportResult
from tests.conftest import make_item, make_report


def parse(argv):
    parser = run.build_parser()
    args = parser.parse_args(argv)
    run.validate_args(parser, args)
    return args


def test_period_requires_explicit_mode():
    with pytest.raises(SystemExit):
        parse(["--period", "2026-10-11", "2026-10-17"])


def test_dry_run_and_apply_are_exclusive():
    with pytest.raises(SystemExit):
        parse(["--period", "2026-10-11", "2026-10-17", "--dry-run", "--apply"])


def test_inspect_cannot_write():
    with pytest.raises(SystemExit):
        parse(["--inspect", "--apply"])


def test_weekly_defaults_to_dry_run():
    args = parse(["--weekly"])
    assert args.dry_run and not args.apply


def test_modes_are_exclusive():
    with pytest.raises(SystemExit):
        parse(["--inspect", "--weekly"])


def test_dry_run_flags():
    args = parse(["--period", "2026-10-11", "2026-10-17", "--dry-run"])
    assert args.dry_run and not args.apply


def test_inspect_without_mysql_settings_is_a_configuration_error():
    assert run.main(["--inspect"]) == 2


def test_bad_timezone_is_a_configuration_error(monkeypatch):
    monkeypatch.setenv("SQP_TIMEZONE", "Mars/Olympus")
    assert run.main(["--inspect"]) == 2


def test_a_run_writes_one_history_line(tmp_path):
    run.main(["--inspect"])
    lines = (tmp_path / "logs" / "cron.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and "exit=2" in lines[0]


# --------------------------------------------------------------------------- fetching a week (fake SP-API)

PERIOD = ["--period", "2026-10-11", "2026-10-17"]


@pytest.fixture
def fake_amazon(monkeypatch, fake_table):
    """SpApiClient that answers every batch with one row per ASIN (or ``rows`` for the first ASIN only)."""
    calls = {"batches": [], "rows": None}

    class FakeClient:
        def __init__(self, settings):
            self.settings = settings

        def fetch_sqp(self, asins, start, end, save_dir=None, **kwargs):
            calls["batches"].append(asins)
            only = asins[:1] if calls["rows"] == "first-asin-only" else asins
            items = [make_item(asin=a, start=str(start), end=str(end)) for a in only]
            if calls["rows"] == "bad-row":
                items.append(make_item(asin=asins[0], query="bad", start=str(start), end=str(end),
                                       clickData__asinClickCount=-3))
            return [ReportResult(f"R{len(calls['batches'])}", "D1", asins, make_report(items, str(start), str(end)),
                                 None)]

    monkeypatch.setattr(spapi, "SpApiClient", FakeClient)
    monkeypatch.setenv("SPAPI_MARKETPLACE_ID", "ATVPDKIKX0DER")
    return calls


def test_dry_run_fetches_all_60_asins_and_saves_the_rows(fake_amazon, project_dirs, capsys):
    assert run.main(PERIOD + ["--dry-run"]) == 0
    assert len(fake_amazon["batches"][0]) == 60
    out = capsys.readouterr().out
    assert "60 requested, 60 returned, 0 missing" in out and "dry run: nothing written" in out
    assert (project_dirs / "reports" / "sqp_2026-10-11_2026-10-17_records.csv").read_text().count("\n") == 61


def test_missing_asins_are_reported(fake_amazon, capsys):
    fake_amazon["rows"] = "first-asin-only"
    assert run.main(PERIOD + ["--dry-run"]) == 0
    assert "60 requested, 1 returned, 59 missing" in capsys.readouterr().out


def test_apply_stops_before_fetching_while_the_asin_count_is_not_the_expected_one(fake_amazon):
    assert run.main(PERIOD + ["--apply"]) == 2
    assert fake_amazon["batches"] == []


def test_rejected_rows_stop_the_week(fake_amazon, monkeypatch):
    monkeypatch.setenv("EXPECTED_ASIN_COUNT", "60")
    fake_amazon["rows"] = "bad-row"
    assert run.main(PERIOD + ["--apply"]) == 7


def test_a_monday_start_is_refused_before_anything_is_fetched(fake_amazon):
    assert run.main(["--period", "2026-10-12", "2026-10-18", "--dry-run"]) == 5
    assert fake_amazon["batches"] == []


def test_weekly_asks_for_the_latest_available_week(fake_amazon, monkeypatch, capsys):
    monkeypatch.setattr(run, "today_in", lambda tz: dt.date(2026, 10, 28))
    assert run.main(["--weekly"]) == 0
    assert "week 2026-10-18..2026-10-24" in capsys.readouterr().out


def test_dry_run_shows_the_plan_and_writes_nothing(fake_amazon, fake_table, project_dirs, capsys):
    assert run.main(PERIOD + ["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "plan: 60 insert, 0 update" in out and fake_table.rows == []
    assert (project_dirs / "reports" / "sqp_2026-10-11_2026-10-17_plan.csv").read_text().count("insert") == 60


def test_apply_writes_reads_back_and_weekly_then_skips_the_week(fake_amazon, fake_table, monkeypatch, capsys):
    monkeypatch.setenv("EXPECTED_ASIN_COUNT", "60")
    monkeypatch.setattr(run, "today_in", lambda tz: dt.date(2026, 10, 21))  # latest week: 11-17 Oct
    assert run.main(["--weekly", "--apply"]) == 0
    assert len(fake_table.rows) == 60 and "nothing left to change" in capsys.readouterr().out
    assert run.main(["--weekly", "--apply"]) == 0  # second scheduled run that week
    assert len(fake_amazon["batches"]) == 1 and len(fake_table.rows) == 60


def test_scheduled_runs_leave_a_week_written_by_hand_without_inserts_alone(fake_amazon, fake_table, monkeypatch,
                                                                           capsys):
    monkeypatch.setenv("EXPECTED_ASIN_COUNT", "60")
    assert run.main(PERIOD + ["--apply", "--no-insert"]) == 0
    monkeypatch.setattr(run, "today_in", lambda tz: dt.date(2026, 10, 20))  # latest week: 11-17 Oct
    assert run.main(["--weekly", "--apply"]) == 0
    assert fake_table.rows == [] and len(fake_amazon["batches"]) == 1  # nothing inserted, Amazon not asked again


def test_a_manual_rerun_of_a_written_week_is_idempotent(fake_amazon, fake_table, monkeypatch, capsys):
    monkeypatch.setenv("EXPECTED_ASIN_COUNT", "60")
    assert run.main(PERIOD + ["--apply"]) == 0
    assert run.main(PERIOD + ["--apply"]) == 0
    assert len(fake_table.rows) == 60 and "plan: 0 insert, 0 update, 0 repair, 60 unchanged" in capsys.readouterr().out


def test_powerbi_db_names_from_the_daily_project_are_accepted(monkeypatch):
    monkeypatch.setenv("POWERBI_DB_HOST", "203.0.113.10")
    monkeypatch.setenv("POWERBI_DB_USER", "someone")
    monkeypatch.setenv("POWERBI_DB_PASSWORD", "not-a-real-password")
    settings = run.load_settings()
    assert (settings.db_host, settings.db_user, settings.db_table) == ("203.0.113.10", "someone", "2024BrandAnalytics")


def test_weekly_scrape_is_the_cron_name_of_sc_night(monkeypatch):
    called = []
    monkeypatch.setattr(run, "_competitors_night", lambda args, settings, logger: called.append(args.sc_night) or (0, "ok"))
    assert run.main(["--weekly-scrape"]) == 0
    assert called == [True]


def test_weekly_scrape_cannot_be_combined_with_apply():
    with pytest.raises(SystemExit):
        parse(["--weekly-scrape", "--apply"])


def test_weekly_scrape_stores_a_scraped_week_that_was_not_stored_yet(monkeypatch, tmp_path):
    # A week whose pages are all saved but whose last load failed is stored by the next run.
    settings = run.load_settings()
    calls = []
    monkeypatch.setattr(run, "_competitors", lambda args, s, logger: calls.append(args.sc_load) or (0, "loaded"))
    monkeypatch.setattr(run, "_loaded_pages", lambda s, end: 0)

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *exc): pass
        def execute(self, *a): pass
        def fetchone(self): return {"d": dt.datetime(2026, 10, 17)}

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *exc): pass
        def cursor(self): return Cursor()
        def rollback(self): pass

    import contextlib
    monkeypatch.setattr(run.powerbi, "connect", lambda s, read_only=True: contextlib.nullcontext(Conn()))
    from app import competitors
    monkeypatch.setattr(competitors, "queries_for_week", lambda *a, **k: {"B0TESTAAAA": ["q"]})
    folder = settings.data_dir / "sellercentral" / "2026-10-17"
    folder.mkdir(parents=True)
    (folder / "B0TESTAAAA_q.json").write_text('{"search_query": "q"}', encoding="utf-8")
    assert run.main(["--weekly-scrape"]) == 0
    assert calls == [["2026-10-11", "2026-10-17"]]
