# Amazon SQP Weekly Updater

Keeps the Power BI database up to date with Amazon **Brand Analytics** data for the EcoNour US store
(Amazon.com, 60 ASINs). It runs on a schedule and needs no manual work once it is set up.

| Data | Source | MySQL table (`powerbidb`) | Schedule |
| --- | --- | --- | --- |
| **Search Query Performance**: per ASIN and search query, the weekly funnel (impressions, clicks, cart adds, purchases) | Amazon **SP-API** report `GET_BRAND_ANALYTICS_SEARCH_QUERY_PERFORMANCE_REPORT` | `2024BrandAnalytics` | Weekly, Monday |
| **Competitors**: for each search query, the top 10 ASINs (title, brand, price, impressions, clicks, shares) | Seller Central → Brand Analytics → **Search Query Details** | `BrandAnalytics_Competitor` | Nightly, until the week is complete |

Both pipelines only add or correct data. **They never delete rows**, never touch other weeks, and never write
duplicates.

---

## Contents

1. [How it works](#how-it-works)
2. [Requirements](#requirements)
3. [Installation](#installation)
4. [Configuration](#configuration)
5. [Usage](#usage)
6. [Scheduling](#scheduling)
7. [How data is written](#how-data-is-written)
8. [Monitoring and troubleshooting](#monitoring-and-troubleshooting)
9. [Security and account safety](#security-and-account-safety)
10. [Project structure](#project-structure)
11. [Development](#development)
12. [Technical reference](#technical-reference)

---

## How it works

### 1. Weekly Search Query Performance (SP-API)

```
config/asins.txt (60 ASINs)
      │
      ▼
Amazon SP-API ── 4 reports per week (Amazon allows ~18 ASINs per report), requested together
      │          Amazon queues them; a run waits up to 22 h and later runs resume the same reports
      ▼
Validate ─────── same week, marketplace and report type; bad rows rejected; batches combined
      │
      ▼
Plan ─────────── compare with the week's rows already in 2024BrandAnalytics: insert / update / unchanged
      │
      ▼
Write ────────── one transaction, then read back: nothing may be left to change
```

- Weeks run **Sunday to Saturday**. A week counts as available **two days after it ends** (Monday, US Pacific).
- A failed, incomplete or empty report is **never** written as an empty week.

### 2. Nightly competitor data (Seller Central)

```
Keywords of the week (from 2024BrandAnalytics, up to 100 per ASIN, ~2,900 per week)
      │
      ▼
Seller Central ── one "Search Query Details" page per ASIN + keyword, read with a saved sign-in
      │           3 parts a night × 400 pages, ~26 s per page; continues where the last part stopped
      ▼
Load ──────────── new rows into BrandAnalytics_Competitor after each part (rows already there are skipped)
```

- A new week starts the night after Monday's SQP update and is complete in about **2.5 nights**.
- Scheduled runs **never log in**: when the saved session has expired they stop, so Amazon never sends an
  unexpected OTP. A person signs in again once.

---

## Requirements

| | |
| --- | --- |
| Python | 3.13, managed with [uv](https://docs.astral.sh/uv/) |
| Database | MySQL `powerbidb` with the user allowed to `SELECT`, `INSERT`, `UPDATE` (no `DELETE` needed) |
| Amazon SP-API | A Login with Amazon app with the **Brand Analytics** role, self-authorised for the seller account |
| Seller Central | A user with the permission **Brand Benefits → Amazon Brand Analytics** (competitor data only) |
| Linux server (competitors) | `xvfb` (virtual screen) and Chromium, installed through Playwright |

---

## Installation

### Linux server (recommended for the schedules)

```bash
git clone <repository-url> /data/cron/amazon-sqp-weekly-updater
cd /data/cron/amazon-sqp-weekly-updater

cp .env.example .env              # then fill in the values (see Configuration)
chmod 600 .env
mkdir -p logs

uv sync                                          # Python 3.13 and the packages
sudo apt install -y xvfb                         # virtual screen for the browser
uv run playwright install --with-deps chromium   # browser for Seller Central
```

Check the setup:

```bash
uv run python run.py --inspect                              # database: table, ASINs (read-only)
xvfb-run -a uv run python run.py --sc-login --terminal      # Seller Central sign-in, type the OTP once
xvfb-run -a uv run python run.py --sc-capture 2026-10-11 2026-10-17 --max-pages 5   # test: 5 pages
```

> The Seller Central sign-in is stored in `data/browser/sellercentral-profile/` on that machine. It cannot be
> copied between Windows and Linux; sign in once on each machine that runs the scraper.

### Windows (development, or running the schedules on a PC)

```powershell
cd C:\Users\Admin\amazon-sqp-weekly-updater
uv sync                                              # creates .venv with Python 3.13 and the packages
# without uv: python -m venv .venv
#             .venv\Scripts\python.exe -m pip install playwright pymysql python-dotenv requests tzdata pytest
copy .env.example .env                               # then fill in the values
.venv\Scripts\python.exe -B run.py --inspect
.venv\Scripts\python.exe -B run.py --sc-login        # opens Chrome; type the OTP, tick "Don't require OTP"
```

On Windows, run commands as `.venv\Scripts\python.exe -B run.py ...`; on Linux as `uv run python run.py ...`.

---

## Configuration

All settings live in `.env` (copied from `.env.example`). `.env` holds passwords and tokens: keep it private
(`chmod 600`) and **never commit it**. Values still containing `replace-me` count as not set.

### Amazon SP-API

| Setting | Description |
| --- | --- |
| `SPAPI_LWA_CLIENT_ID`, `SPAPI_LWA_CLIENT_SECRET` | Login with Amazon credentials of the SP-API app |
| `SPAPI_LWA_REFRESH_TOKEN` | Refresh token from authorising the app for the seller account (`Atzr|...`) |
| `SPAPI_MARKETPLACE_ID` | `ATVPDKIKX0DER` (Amazon.com). The regional endpoint follows from it |
| `SPAPI_ENDPOINT` | Optional override of the regional endpoint |

AWS keys and role ARNs are **not** needed: SP-API accepts LWA-only requests.

### Database

| Setting | Description |
| --- | --- |
| `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD` | MySQL server and login |
| `DB_NAME`, `DB_TABLE` | `powerbidb`, `2024BrandAnalytics` |

The `POWERBI_DB_*` names used by the daily-data project are accepted as well.

### Weekly SQP behaviour

| Setting | Default | Description |
| --- | --- | --- |
| `EXPECTED_ASIN_COUNT` | `60` | Must equal the unique ASINs in `config/asins.txt`, otherwise writing is refused |
| `SQP_TIMEZONE` | `America/Los_Angeles` | Decides which week is the latest completed one |
| `SQP_AVAILABILITY_LAG_DAYS` | `2` | Days after Saturday before a week counts as available |
| `SQP_POLL_INTERVAL_SECONDS` | `300` | How often a run checks Amazon's report status |
| `SQP_POLL_TIMEOUT_SECONDS` | `79200` | How long one run waits for Amazon (22 h); the next run resumes |
| `SQP_MAX_RETRIES` | `6` | Retries for throttled, failing or unreachable SP-API calls |

### Seller Central (competitor data)

| Setting | Default | Description |
| --- | --- | --- |
| `SELLERCENTRAL_EMAIL`, `SELLERCENTRAL_PASSWORD` | – | Seller Central user with Brand Analytics access (`AMAZON_EMAIL` / `AMAZON_PASSWORD` also work) |
| `SELLERCENTRAL_TOTP_SECRET` | – | Optional authenticator-app key to generate OTPs automatically (only with your team lead's approval) |
| `SC_COMPETITORS_FROM` | `2026-10-11` | Sunday of the oldest week the nightly scraper goes back to (it starts with the latest week) |
| `SC_BROWSER_CHANNEL` | `chrome` | `chrome` (Google Chrome installed) or `chromium` (Playwright's build; use on Linux) |
| `SC_HEADLESS` | `false` | `true` runs without a window. On Linux without a screen (cron, no `xvfb-run`) this happens by itself |

### General

| Setting | Default | Description |
| --- | --- | --- |
| `SQP_DATA_DIR`, `SQP_LOG_DIR` | `data/`, `logs/` | Optional other locations |
| `LOG_LEVEL` | `INFO` | `DEBUG` for more detail |

The ASIN list is [`config/asins.txt`](config/asins.txt): one `ASIN  product name` per line. Names are for
reference only and are never written; an ASIN listed twice is requested once.

---

## Usage

### Weekly SQP (SP-API → `2024BrandAnalytics`)

| Task | Command |
| --- | --- |
| Inspect the table and its ASINs (read-only) | `run.py --inspect` |
| Preview a week: fetch, validate, show what would change (writes nothing) | `run.py --period 2026-10-11 2026-10-17 --dry-run` |
| Write a week (backfill) | `run.py --period 2026-10-11 2026-10-17 --apply` |
| Latest available week, written (what the schedule runs) | `run.py --weekly --apply` |
| Ask Amazon again instead of reusing downloaded reports | add `--refetch` |
| Write only the batches already downloaded (rest later) | add `--downloaded-only` (with `--period`) |
| Change existing rows only, insert nothing | add `--no-insert` (with `--period`) |

A dry run saves `data/reports/sqp_<start>_<end>_records.csv` (the validated rows) and `..._plan.csv` (every
insert and every changed value, old → new) for review. The `--apply` run reuses the dry run's downloads.

### Competitors (Seller Central → `BrandAnalytics_Competitor`)

| Task | Command |
| --- | --- |
| Sign in once (Windows: Chrome window; server: OTP in the terminal) | `run.py --sc-login` / `run.py --sc-login --terminal` |
| Scheduled part (cron): web-scrape the latest unfinished week (then older ones), up to 400 pages, and store it | `run.py --weekly-scrape` (same as `--sc-night`) |
| Save pages of one week by hand (continues where it stopped) | `run.py --sc-capture 2026-10-11 2026-10-17 [--max-pages 400] [--top 100]` |
| Load a week's saved pages into the table | `run.py --sc-load 2026-10-11 2026-10-17` |
| Open one Seller Central page and save a screenshot (diagnostics) | `run.py --sc-inspect "<url>"` |

### Other

| Task | Command |
| --- | --- |
| All options | `run.py --help` |
| Console output for cron (warnings and errors only) | add `--quiet` |
| Run the tests | `python -m pytest` |

---

## Scheduling

Run each job in **one place only** (the server **or** a Windows PC), never both: they share the same Amazon
login and the same tables.

### Linux cron (recommended)

Check the server's time zone first with `timedatectl`, then add to `crontab -e` (full example in
[`deploy/crontab.example`](deploy/crontab.example)). Use the path that `which uv` shows.

**Server clock on IST (Asia/Kolkata):**

```cron
MAILTO=""
# Weekly SQP: Monday 15:00 IST (already Monday in the US); Tue–Fri are backups that stop at once when the week is written
0 15 * * 1-5 cd /data/cron/amazon-sqp-weekly-updater && /root/.local/bin/uv run --no-sync python run.py --weekly --apply --quiet >> logs/cron-output.log 2>&1

# Competitors: 3 parts a night, 00:00, 03:00, 06:00 IST (= US Pacific business hours)
0 0,3,6 * * * cd /data/cron/amazon-sqp-weekly-updater && flock -n logs/.sc-cron.lock timeout 3h15m xvfb-run -a /root/.local/bin/uv run --no-sync python run.py --weekly-scrape --quiet >> logs/cron-output.log 2>&1
```

**Server clock on UTC** (same times, IST = UTC + 5:30):

```cron
30 9 * * 1-5   cd /data/cron/amazon-sqp-weekly-updater && /root/.local/bin/uv run --no-sync python run.py --weekly --apply --quiet >> logs/cron-output.log 2>&1
30 0,18,21 * * * cd /data/cron/amazon-sqp-weekly-updater && flock -n logs/.sc-cron.lock timeout 3h15m xvfb-run -a /root/.local/bin/uv run --no-sync python run.py --weekly-scrape --quiet >> logs/cron-output.log 2>&1
```

`flock` keeps two scraper parts from overlapping, `timeout` ends a part that hangs, and `xvfb-run` gives the
browser a virtual screen.

### Windows Task Scheduler (alternative)

```powershell
powershell -ExecutionPolicy Bypass -File deploy\install_task.ps1               # weekly SQP, Monday 15:00 (+ Tue–Fri backups)
powershell -ExecutionPolicy Bypass -File deploy\install_competitors_task.ps1   # competitors, 00:00 / 03:00 / 06:00
```

Add `-Remove` to unregister. The PC must stay on (sleep: never) with the user signed in to Windows; a locked
screen is fine.

### Typical week

| When (IST) | What happens |
| --- | --- |
| Monday 15:00 | SQP reports for last week are requested; the week is written to `2024BrandAnalytics` as soon as Amazon delivers |
| Monday night → Thursday morning | Competitor pages for that week are scraped (3 parts a night) and loaded after each part |
| Tuesday–Friday 15:00 | SQP backup runs: only work if Amazon was still processing on Monday |

---

## How data is written

### `2024BrandAnalytics`

The table has no unique key, so rows are matched by **(reporting_date, ASIN, search query)**, the query
compared without case and extra spaces.

| Situation | Result |
| --- | --- |
| Row exists, Amazon's numbers differ | Only the changed columns are updated |
| Row exists, numbers equal | Unchanged |
| New search query | Inserted with the ASIN's existing name, `week`, `year`, `Index` = `id` |
| Stored text has `?` for special characters, every number equal | Matched by its numbers; stored text kept |
| Row damaged by an earlier load (score glued to the text, numbers shifted one column) | Repaired in place |
| Row in the table but not in Amazon's report | Kept |
| Amazon leaves a value empty | The table's value is kept |
| Key found on several rows | Skipped and listed, never guessed |

- `reporting_date` = the week's Saturday at 00:00; `week` = `Week N` (N-th Saturday of the year); `year` = the
  Saturday's year. Product names are never changed.
- Everything happens in **one transaction**; any error rolls the whole week back. The week is read back
  afterwards and must show nothing left to change.
- Shares and rates are stored as percentages with 2 decimals (`0.68` = 0.68 %), as in the existing rows.

### `BrandAnalytics_Competitor`

The existing table and its format are kept: one row per competitor ASIN.

| Column | Value |
| --- | --- |
| `Week`, `week_num`, `Year` | `Week 42`, `42`, `2026` |
| `Date Range` | `Oct 11 to Oct 17` |
| `ASIN`, `Name` | Our ASIN and its existing product name |
| `Keyword` | Search query |
| `Comp ASIN`, `Title`, `Brand`, `Price` | Competitor ASIN, product title, brand, median price |
| `Imp`, `Clicks`, `imp_share`, `click_share` | Competitor's impressions, clicks and shares (%) |
| `Index`, `id` | Continued from the last rows |

- A row whose **(week, ASIN, keyword, competitor ASIN)** is already stored is skipped, so loads can be
  repeated safely. Existing rows are never updated or deleted.
- For broad keywords Amazon's top results can be videos (e.g. "Cars"); their brand and price are empty.

---

## Monitoring and troubleshooting

| Log | Content |
| --- | --- |
| `logs/cron.log` | One line per run: time, exit code, command, summary (rows written, pages left, …) |
| `logs/sqp_updater.log` | Full details: report IDs and statuses, pages, warnings (rotated daily, 30 days kept) |
| `logs/cron-output.log` | Console output of cron runs |

### Exit codes

| Code | Meaning | What to do |
| --- | --- | --- |
| 0 | Success | – |
| 1 | Unexpected error | Read `logs/sqp_updater.log` |
| 2 | A setting is missing or wrong | Read the message; fix `.env` |
| 3 | MySQL unreachable, or a write was rolled back (**nothing changed**) | Check the `DB_*` settings and the message |
| 4 | Seller Central: sign-in needed, or the page is not as expected | Run `run.py --sc-login` (OTP once); see the screenshot in `data/screenshots/` |
| 5 | Not a Sunday–Saturday week, or the week is not complete | Fix `--period`; `--weekly` picks a valid week itself |
| 6 | SP-API request failed, or a report was not ready in time | Timeout: the next run resumes. HTTP 403: the app needs the Brand Analytics role |
| 7 | Report for another week/marketplace, or with rejected rows | Nothing written; the raw JSON is in `data/reports/` |
| 11 | Another run was still going | Nothing; the next run continues |

### Common situations

| Symptom | Cause and fix |
| --- | --- |
| SQP week takes days to arrive | Amazon processes SQP reports for the whole account in **one queue** (~75 a day). Another program requesting many SQP reports with the same SP-API app delays everything. Find and stop it; the updater resumes on its own |
| Competitor part stops with "sign-in expired" | Run `run.py --sc-login` (Linux: `--sc-login --terminal`) once; the next part continues |
| "Access Required" in Seller Central | The Seller Central user lacks **Brand Benefits → Amazon Brand Analytics**; an admin grants it under Settings → User Permissions |
| New weeks not visible in Power BI | The data is in MySQL; refresh the Power BI dataset (full refresh if it uses incremental refresh on `id`/`Index`) |
| Read-back reports tiny price differences above $10,000 | MySQL `FLOAT` keeps ~6–7 digits; handled automatically (values compared at that precision) |
| Start a week over | Delete `data/state/sqp_<start>_<end>.json` or use `--refetch` |

---

## Security and account safety

- **Secrets** live only in `.env` (never committed). The Seller Central session in `data/browser/` gives access
  to the seller account: keep the `data/` folder private.
- **Amazon's terms do not allow automated tools on Seller Central.** The competitor scraper only reads pages, at
  a person-like pace (~26 s per page), and stops at once on a captcha, an "unusual activity" or an
  "Access Required" page. A risk to the seller account remains; keep the volume and pace as configured and
  agree on any change with your team lead.
- The SP-API part uses Amazon's official API and carries no such risk.

---

## Project structure

| Path | Purpose |
| --- | --- |
| `run.py` | Command-line entry point (`--help` lists every option) |
| `app/spapi.py` | SP-API: access token, reports, polling, download, retries, resume |
| `app/report.py` | Validates the report JSON, combines batches, finds missing ASINs |
| `app/powerbi.py` | MySQL: inspection, the week's plan, the transactional write for `2024BrandAnalytics` |
| `app/competitors.py` | Search Query Details: keyword plan, page capture, parsing, loading `BrandAnalytics_Competitor` |
| `app/sellercentral.py` | Seller Central browser: saved sign-in, page inspection |
| `app/schema.py` | Report fields and their table columns |
| `app/schedule.py` | Sunday–Saturday weeks, latest available week, run lock |
| `app/asins.py` | Reads `config/asins.txt` |
| `app/config.py`, `app/exceptions.py`, `app/logging_config.py` | Settings, exit codes, logging |
| `config/asins.txt` | The 60 ASINs with their product names |
| `deploy/crontab.example` | Cron lines for the Linux server |
| `deploy/install_task.ps1`, `deploy/install_competitors_task.ps1` | Windows Task Scheduler setup |
| `data/` (not in git) | Downloaded reports, run state, saved pages, browser session, screenshots |
| `logs/` (not in git) | Run history and logs |
| `tests/` | Automated tests (no Amazon or real database needed) |

---

## Development

```bash
uv sync                      # installs the packages, including the test tools
uv run python -m pytest      # runs the whole test suite (Amazon and MySQL are faked)
```

The tests cover ASIN handling, week rules, report parsing, missing ASINs, failed and slow reports, resume,
the MySQL plan and write (updates, inserts, repairs, duplicate prevention, rollback) and the command line.

---

## Technical reference

<details>
<summary>SP-API report fields → <code>2024BrandAnalytics</code> columns</summary>

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

Prices in a currency other than USD are refused. Rows with an unrequested ASIN, a missing search query, a
negative or fractional count, or no `searchQueryVolume` / `asinImpressionCount` are rejected and the week is not
written. Exact duplicates are dropped; conflicting duplicates refuse the report.

</details>

<details>
<summary>SP-API calls</summary>

1. `POST https://api.amazon.com/auth/o2/token` with the refresh token → access token (renewed before it expires).
2. `POST /reports/2021-06-30/reports` with `reportPeriod: WEEK` and up to 200 characters of ASINs (4 reports
   for 60 ASINs), all created before waiting.
3. `GET /reports/2021-06-30/reports/{id}` every `SQP_POLL_INTERVAL_SECONDS` until `DONE`. `FATAL` reports carry
   an error document, which is logged.
4. `GET /reports/2021-06-30/documents/{id}` → pre-signed URL; the (gzip) JSON is downloaded to `data/reports/`.

HTTP 429 and 5xx responses and network errors are retried with exponential backoff, respecting
`x-amzn-RateLimit-Limit`. Report IDs are kept in `data/state/`, so an interrupted run resumes the same reports.

</details>

<details>
<summary>Search Query Details page</summary>

URL per ASIN, keyword and week:
`https://sellercentral.amazon.com/brand-analytics/dashboard/query-detail?view-id=query-detail-asin-view&asin=<ASIN>&search-term-freeform=<keyword>&reporting-range=weekly&weekly-week=<Saturday>&country-id=us`

Each page is saved as JSON in `data/sellercentral/<Saturday>/` before it is parsed, so a load can be repeated
or checked without opening Seller Central again.

</details>
