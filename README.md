# Amazon SQP Weekly Updater

Requests Amazon's weekly **Search Query Performance** (Brand Analytics) report through the **SP-API** for the
US store's ASINs and writes it to the MySQL table **`powerbidb.2024BrandAnalytics`**, once a week.

- **What it fetches:** report type `GET_BRAND_ANALYTICS_SEARCH_QUERY_PERFORMANCE_REPORT`, period `WEEK`
  (Sunday–Saturday), up to 100 search queries per ASIN
- **Which account:** the SP-API app's refresh token, marketplace **Amazon.com** (`ATVPDKIKX0DER`)
- **Which ASINs:** [`config/asins.txt`](config/asins.txt): 65 lines copied from the table, **60 unique ASINs**
  (5 are listed twice under different names). These are exactly the 60 ASINs in the table (`--inspect`)
- **Where it goes:** `powerbidb.2024BrandAnalytics` on `13.126.54.249`: matching rows of the week are updated,
  new search queries inserted, **nothing is deleted**

---

## Contents

1. [How a run works](#how-a-run-works)
2. [Commands](#commands)
3. [Backfill a week by hand](#backfill-a-week-by-hand)
4. [Weekly schedule](#weekly-schedule)
5. [How a week is written](#how-a-week-is-written)
6. [Checking runs and fixing problems](#checking-runs-and-fixing-problems)
7. [Settings (.env)](#settings-env)
8. [Project files](#project-files)
9. [Technical details](#technical-details)

---

## How a run works

```
config/asins.txt
   │  1. the 60 unique ASINs (names are reference only, never written)
   ▼
SP-API (LWA refresh token, no AWS keys)
   │  2. one report per batch of ASINs (Amazon allows 200 characters of ASINs per report: 4 reports)
   │  3. wait for each report (can take hours), download the JSON              →  data/reports/
   ▼
4. check every batch (same week, marketplace, report type; bad rows rejected), combine them,
   list requested / returned / missing ASINs                                   →  ..._records.csv
   ▼
5. compare with the table's rows of that week: inserts, updates, unchanged     →  ..._plan.csv
   ▼
6. --apply: write in ONE transaction, read the week back (nothing may be left to change)
```

- A requested week is used **exactly as given**. If Amazon returns another week, the whole report is refused.
- Nothing is written unless **every** batch downloaded and passed: a failed report, a report with rejected rows
  (exit 7) or a week without a single row is never written as an empty week.
- **Amazon is slow:** a report can sit in Amazon's queue for hours. Every report ID is kept in
  `data/state/sqp_<start>_<end>.json`, so a run that is cut off (PC asleep, no internet, timeout) carries on with
  the **same** reports next time, and batches already downloaded are not requested again.
- Two runs never overlap: a run that starts while another is still going stops at once (exit 11).

---

## Commands

Run these from `C:\Users\Admin\amazon-sqp-weekly-updater`:

| What you want | Command |
| --- | --- |
| Check the table and its ASINs. **Read-only** | `.venv\Scripts\python.exe -B run.py --inspect` |
| Fetch one week and show what would change. **Writes nothing** | `.venv\Scripts\python.exe -B run.py --period 2026-10-11 2026-10-17 --dry-run` |
| Fetch one week and write it | `.venv\Scripts\python.exe -B run.py --period 2026-10-11 2026-10-17 --apply` |
| The latest available week, written (what the schedule runs) | `.venv\Scripts\python.exe -B run.py --weekly --apply` |
| The latest available week, preview only | `.venv\Scripts\python.exe -B run.py --weekly` |
| Ask Amazon again instead of reusing downloads | add `--refetch` |
| Write only the ASINs whose report is already downloaded (rest later) | add `--downloaded-only` (with `--period`) |
| Change existing rows only, insert nothing | add `--no-insert` (with `--period`) |
| All options | `.venv\Scripts\python.exe -B run.py --help` |
| Run the tests | `.venv\Scripts\python.exe -B -m pytest` |

- `--period` needs `--dry-run` or `--apply`. `--weekly` without either is a dry run.
- With `uv` (e.g. on a Linux server), `uv run python run.py ...` works the same way.

---

## Backfill a week by hand

Any Sunday–Saturday week, for example 11–17 October 2026:

```
.venv\Scripts\python.exe -B run.py --period 2026-10-11 2026-10-17 --dry-run
```

1. Read the summary, and if you like `data\reports\sqp_2026-10-11_2026-10-17_plan.csv` (every insert and every
   changed value, old → new).
2. Then write it:

```
.venv\Scripts\python.exe -B run.py --period 2026-10-11 2026-10-17 --apply
```

The `--apply` run reuses the reports the dry run downloaded, so it takes seconds. Running the same week again
changes nothing (all rows "unchanged"). A backfill is never repeated by the weekly schedule.

---

## Weekly schedule

**Windows PC (Task Scheduler):** register once, from the project folder:

```
powershell -ExecutionPolicy Bypass -File deploy\install_task.ps1
```

- Task **"Amazon SQP Weekly Updater"** runs `run.py --weekly --apply --quiet` **every Monday at 15:00** (India
  time), without a window, for the Sunday–Saturday week that ended two days before. It wakes the PC, needs the
  network, and starts late if the PC was off.
- **Why 15:00:** the week is decided in US Pacific time; Monday morning in India is still Sunday there.
  15:00 IST is already Monday in the US (`SQP_AVAILABILITY_LAG_DAYS=2`: Saturday + 2 days).
- **Backup runs Tuesday–Friday 15:00:** Amazon can keep a report in its queue for a day or more. A run waits
  up to 22 hours (`SQP_POLL_TIMEOUT_SECONDS`); a backup run carries on with the same reports. Once the week is
  written (`data/state/`), every later run that week stops at once, so normally only Monday does any work.
- Remove it with `powershell -ExecutionPolicy Bypass -File deploy\install_task.ps1 -Remove`.

**Linux server (cron):** the line is in `deploy/crontab.example`. Run the schedule in **one** place only.

### Competitor data (Seller Central, nightly)

`powerbidb.BrandAnalytics_Competitor` (existing table, its own format) gets the top 10 competitor ASINs of every
keyword of every ASIN, read from Brand Analytics → **Search Query Details** in Seller Central.

- Task **"Amazon SQP Competitors"** runs `run.py --sc-night --quiet` daily at **00:00, 03:00 and 06:00**
  (`deploy\install_competitors_task.ps1`). Each part opens up to 400 pages (~26 s each, ~3 hours) of the oldest
  week not fully saved since `SC_COMPETITORS_FROM` (2026-10-11), then loads that week's saved pages into the
  table; rows already there are skipped, nothing is updated or deleted.
- The keyword list of a week comes from `2024BrandAnalytics` (Monday's SQP update), so a new week starts the
  night after it is loaded. ~2,900 pages a week ≈ 2.5 nights.
- Needs the PC on and you signed in to Windows (screen may be locked). Uses the saved sign-in of
  `run.py --sc-login`; when it has expired the part stops **without** logging in (no OTP): sign in once again.
- Stops at once on a captcha, an "unusual activity" or "Access Required" page. Saved pages stay in
  `data\sellercentral\<week end>\`.
- By hand: `run.py --sc-capture START END [--max-pages N] [--top N]` and `run.py --sc-load START END`.

---

## How a week is written

The table (inspected 2026-10-24) has no unique key, so rows are matched by **(reporting_date, ASIN, search
query)**, the query compared without case and extra spaces:

| Situation | What happens |
| --- | --- |
| Row exists, Amazon's numbers differ | Only the changed columns are updated |
| Row exists, numbers equal | Nothing (counted as "unchanged") |
| Amazon has a query the table does not | Inserted, with the ASIN's name from the table, `week`, `year`, `Index` = `id` |
| Stored text has `?` for special characters (`24??13??car window shade` for `24”x13” car window shade`), every number equal | Matched by its numbers, **unchanged**; the stored text is kept |
| Row broken by an earlier load: score glued to the text (`car sun shade 25??96`), numbers one column to the left | **Repaired** in place with Amazon's text and values (no duplicate inserted) |
| The table has a row Amazon's report does not | **Kept** as it is |
| Amazon leaves a value empty, the table has one | The table's value is **kept** |
| The key is on several rows (an earlier double load) | **Skipped** and listed, never guessed |

- `reporting_date` = the week's **Saturday** at 00:00; `week` = `Week N` (N-th Saturday of the year, e.g.
  2026-10-17 = Week 42); `year` = the Saturday's year. Product names are never changed.
- Other weeks are never touched. Everything happens in **one transaction**: any error rolls the whole week back
  (exit 3, nothing changed). After writing, the week is read back and must show nothing left to change.
- Percentages are stored as Amazon sends them (`0.68` = 0.68 %), percentages and prices with 2 decimals, as in
  the existing rows.

---

## Checking runs and fixing problems

- `logs/cron.log`: one line per run, for example
  `2026-10-24 16:30:33 exit=0 --inspect | inspected`
- `logs/sqp_updater.log`: the details, report IDs and statuses (rotated daily, 30 days kept).

| Exit | Meaning | What to do |
| --- | --- | --- |
| 0 | Success | – |
| 1 | Unexpected error | Read `logs/sqp_updater.log` |
| 2 | A setting is missing or wrong | Read the message, fix `.env` |
| 3 | MySQL unreachable, or the write was rolled back (**nothing changed**) | Check the `DB_*` / `POWERBI_DB_*` settings and the message |
| 5 | Not a Sunday–Saturday week, or the week is not complete | Fix `--period`; `--weekly` picks a valid week itself |
| 6 | SP-API refused the request, the report failed, or it was not ready in time | Timeout: the next run resumes. HTTP 403: the app needs the **Brand Analytics** role. FATAL: often the week is not published yet |
| 7 | The report was for another week or marketplace, or had rejected rows | Nothing was written; the raw JSON is in `data/reports/` |
| 11 | Another run was still going | Nothing; the next run continues |

To start a week from scratch, delete its `data/state/sqp_<start>_<end>.json` (or use `--refetch`).

---

## Settings (`.env`)

Copy `.env.example` to `.env`. Keep `.env` private; it is never committed. Values still containing
`replace-me` count as not set.

| Setting | What it is |
| --- | --- |
| `SPAPI_LWA_CLIENT_ID`, `SPAPI_LWA_CLIENT_SECRET`, `SPAPI_LWA_REFRESH_TOKEN` | The SP-API app's Login with Amazon credentials. No AWS keys needed |
| `SPAPI_MARKETPLACE_ID` | `ATVPDKIKX0DER` (Amazon.com); the regional endpoint follows from it |
| `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`, `DB_TABLE` | MySQL (`13.126.54.249`, `powerbidb`, `2024BrandAnalytics`). The `POWERBI_DB_*` names of the daily-data project work too |
| `EXPECTED_ASIN_COUNT` | `60`; must equal the unique ASINs in `config/asins.txt` for `--apply` to run |
| `SQP_TIMEZONE` | `America/Los_Angeles`; decides which week is the latest completed one |
| `SQP_AVAILABILITY_LAG_DAYS` | `2`: days after Saturday before a week counts as available (Monday) |
| `SQP_POLL_INTERVAL_SECONDS`, `SQP_POLL_TIMEOUT_SECONDS` | `300`, `79200`: check every 5 minutes, wait up to 22 hours for Amazon |
| `SQP_MAX_RETRIES` | `6`: retries for throttled (429), failing (5xx) or unreachable SP-API calls |
| `SQP_DATA_DIR`, `SQP_LOG_DIR` | Move `data/` or `logs/` elsewhere (optional) |
| `LOG_LEVEL` | `INFO` (or `DEBUG` for more detail) |

---

## Project files

| Path | What it is |
| --- | --- |
| `run.py` | The command you run (all options: `--help`) |
| `config/asins.txt` | The ASINs to request (with their product names, for reference) |
| `app/asins.py` | Reads `config/asins.txt`: unique ASINs, repeated ones, count check |
| `app/spapi.py` | The SP-API part: access token, create report, wait, download, ASIN batches, retries, resume |
| `app/report.py` | Reads and checks the report JSON, combines the batches, missing ASINs |
| `app/powerbi.py` | MySQL: inspection, the week's plan (insert / update / unchanged), the transactional write |
| `app/schema.py` | Report fields, and which table column each one goes to |
| `app/schedule.py` | Sunday–Saturday weeks, latest available week, run lock |
| `app/config.py`, `app/exceptions.py`, `app/logging_config.py` | Settings, exit codes, logging |
| `data/reports/` | Raw report JSON as Amazon sent it; each week's `_records.csv` and `_plan.csv` |
| `data/state/` | Per week: report IDs, and when it was written |
| `deploy/install_task.ps1` | Registers the Windows scheduled task |
| `deploy/crontab.example` | The cron line for a Linux server |
| `logs/` | `cron.log` and `sqp_updater.log` |
| `tests/` | Automated tests (no Amazon or real database needed) |

---

## Technical details

<details>
<summary>Report fields and table columns</summary>

| Report field | Column | Report field | Column |
| --- | --- | --- | --- |
| searchQuery | search_query | totalCartAddCount | cart_adds_total_count |
| searchQueryScore | search_query_score | totalCartAddRate | cart_adds_rate_pct |
| searchQueryVolume | search_query_volume | asinCartAddCount | cart_adds_asin_count |
| totalQueryImpressionCount | impressions_total_count | asinCartAddShare | cart_adds_asin_share_pct |
| asinImpressionCount | impressions_asin_count | totalMedianCartAddPrice | cart_adds_price_median |
| asinImpressionShare | impressions_asin_share_pct | asinMedianCartAddPrice | cart_adds_asin_price_median |
| totalClickCount | clicks_total_count | total{SameDay,OneDay,TwoDay}ShippingCartAddCount | cart_adds_shipping_{same_day,1d,2d} |
| totalClickRate | clicks_rate_pct | totalPurchaseCount | purchases_total_count |
| asinClickCount | clicks_asin_count | totalPurchaseRate | purchases_rate_pct |
| asinClickShare | clicks_asin_share_pct | asinPurchaseCount | purchases_asin_count |
| totalMedianClickPrice | clicks_price_median | asinPurchaseShare | purchases_asin_share_pct |
| asinMedianClickPrice | clicks_asin_price_median | totalMedianPurchasePrice | purchases_price_median |
| total{SameDay,OneDay,TwoDay}ShippingClickCount | clicks_shipping_{same_day,1d,2d} | asinMedianPurchasePrice | purchases_asin_price_median |
| | | total{SameDay,OneDay,TwoDay}ShippingPurchaseCount | purchases_shipping_{same_day,1d,2d} |

- Not stored: the currency of the prices (anything but USD is refused), the marketplace (the table is US only).
- **Rows rejected** (listed, the week is not written): an ASIN that was not requested, a missing search query, a
  negative or fractional count, or a row without `searchQueryVolume` or `asinImpressionCount`.
- **Exact duplicates** in a report are dropped; **conflicting duplicates** refuse the whole report.

</details>

<details>
<summary>SP-API calls</summary>

1. `POST https://api.amazon.com/auth/o2/token` with the refresh token → access token (renewed before it expires).
2. `POST /reports/2021-06-30/reports` with `reportPeriod: WEEK` and up to 200 characters of ASINs (18 ASINs per
   report, so 60 ASINs make 4 reports).
3. `GET /reports/2021-06-30/reports/{id}` every `SQP_POLL_INTERVAL_SECONDS` until DONE. FATAL reports carry an
   error document, which is shown in the log.
4. `GET /reports/2021-06-30/documents/{id}` → a pre-signed URL; the (gzip) JSON is downloaded and saved.

HTTP 429 and 5xx, and network errors, are retried with exponential backoff, respecting `x-amzn-RateLimit-Limit`.

</details>
#   A m a z o n - S Q P - W e e k l y - U p d a t e r  
 