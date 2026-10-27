"""Settings from environment variables (the project's .env is loaded first).

``SPAPI_*``: the SP-API app and marketplace; ``DB_*``: the MySQL table; ``SQP_*``: which
week is due and how long to wait for Amazon. Error messages name variables, never their values.
"""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from .exceptions import ConfigurationError

PROJECT_ROOT = Path(__file__).resolve().parent.parent

REPORT_TYPE = "GET_BRAND_ANALYTICS_SEARCH_QUERY_PERFORMANCE_REPORT"
# The week is decided in this time zone (Amazon.com's).
DEFAULT_TIMEZONE = "America/Los_Angeles"

# Marketplace ID -> SP-API regional endpoint.
MARKETPLACE_ENDPOINTS = {
    # North America
    "ATVPDKIKX0DER": "https://sellingpartnerapi-na.amazon.com",  # US
    "A2EUQ1WTGCTBG2": "https://sellingpartnerapi-na.amazon.com",  # CA
    "A1AM78C64UM0Y8": "https://sellingpartnerapi-na.amazon.com",  # MX
    "A2Q3Y263D00KWC": "https://sellingpartnerapi-na.amazon.com",  # BR
    # Europe / Middle East / India
    "A1F83G8C2ARO7P": "https://sellingpartnerapi-eu.amazon.com",  # UK
    "A1PA6795UKMFR9": "https://sellingpartnerapi-eu.amazon.com",  # DE
    "A13V1IB3VIYZZH": "https://sellingpartnerapi-eu.amazon.com",  # FR
    "APJ6JRA9NG5V4": "https://sellingpartnerapi-eu.amazon.com",  # IT
    "A1RKKUPIHCS9HS": "https://sellingpartnerapi-eu.amazon.com",  # ES
    "A1805IZSGTT6HS": "https://sellingpartnerapi-eu.amazon.com",  # NL
    "A2NODRKZP88ZB9": "https://sellingpartnerapi-eu.amazon.com",  # SE
    "A1C3SOZRARQ6R3": "https://sellingpartnerapi-eu.amazon.com",  # PL
    "A33AVAJ2PDY3EV": "https://sellingpartnerapi-eu.amazon.com",  # TR
    "A2VIGQ35RCS4UG": "https://sellingpartnerapi-eu.amazon.com",  # AE
    "A17E79C6D8DWNP": "https://sellingpartnerapi-eu.amazon.com",  # SA
    "A21TJRUUN4KGV": "https://sellingpartnerapi-eu.amazon.com",  # IN
    # Far East
    "A1VC38T7YXB528": "https://sellingpartnerapi-fe.amazon.com",  # JP
    "A39IBJ37TRP1C6": "https://sellingpartnerapi-fe.amazon.com",  # AU
    "A19VAU5U5O7RUS": "https://sellingpartnerapi-fe.amazon.com",  # SG
}


def load_env(path: Path | None = None) -> None:
    """Load .env without overriding variables already set in the shell."""
    load_dotenv(path or PROJECT_ROOT / ".env", override=False)


def is_placeholder(value: str) -> bool:
    return "replace-me" in value or "xxxxxxxx" in value


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or not value.strip() or is_placeholder(value):
        return default
    return value.strip()


def _db_env(suffix: str, default: str | None = None) -> str | None:
    """DB_<suffix>, else POWERBI_DB_<suffix> (the names the amazon_daily_data project uses)."""
    return _env(f"DB_{suffix}") or _env(f"POWERBI_DB_{suffix}", default)


def _env_date(name: str, default: dt.date) -> dt.date:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return dt.date.fromisoformat(raw)
    except ValueError:
        raise ConfigurationError(f"{name} must be a date (YYYY-MM-DD)") from None


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigurationError(f"{name} must be an integer") from None


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    log_dir: Path
    log_level: str
    # SP-API / Login with Amazon
    lwa_client_id: str | None = None
    lwa_client_secret: str | None = field(default=None, repr=False)
    lwa_refresh_token: str | None = field(default=None, repr=False)
    marketplace_id: str | None = None
    sp_api_endpoint: str | None = None
    # MySQL
    db_host: str | None = None
    db_port: int = 3306
    db_user: str | None = None
    db_password: str | None = field(default=None, repr=False)
    db_name: str = "powerbidb"
    db_table: str = "2024BrandAnalytics"
    # Behaviour
    expected_asin_count: int = 62
    timezone: ZoneInfo = ZoneInfo(DEFAULT_TIMEZONE)
    availability_lag_days: int = 2
    poll_interval_seconds: int = 300
    poll_timeout_seconds: int = 22 * 60 * 60
    max_retries: int = 6
    # Seller Central sign-in (app/sellercentral.py)
    sc_email: str | None = field(default=None, repr=False)
    sc_password: str | None = field(default=None, repr=False)
    sc_totp_secret: str | None = field(default=None, repr=False)
    # First week (its Sunday) the nightly Search Query Details scraping covers.
    sc_competitors_from: dt.date = dt.date(2026, 10, 11)
    # Browser for Seller Central: "chrome" (Google Chrome installed) or "chromium" (Playwright's own build);
    # headless (no window) for a server without a display, unless it runs under xvfb-run.
    sc_browser_channel: str = "chrome"
    sc_headless: bool = False

    @property
    def report_dir(self) -> Path:
        """Raw report JSON as Amazon sent it."""
        return self.data_dir / "reports"

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    @property
    def asin_file(self) -> Path:
        return PROJECT_ROOT / "config" / "asins.txt"

    def ensure_directories(self) -> None:
        for path in (self.report_dir, self.state_dir, self.log_dir):
            path.mkdir(parents=True, exist_ok=True)

    def require_db(self) -> None:
        missing = [name for name, value in (("DB_HOST", self.db_host),
                                            ("DB_USER", self.db_user),
                                            ("DB_PASSWORD", self.db_password)) if not value]
        if missing:
            raise ConfigurationError("Missing MySQL settings: " + ", ".join(missing) + " (or POWERBI_DB_*)")

    def require_spapi(self) -> None:
        missing = [name for name, value in (("SPAPI_LWA_CLIENT_ID", self.lwa_client_id),
                                            ("SPAPI_LWA_CLIENT_SECRET", self.lwa_client_secret),
                                            ("SPAPI_LWA_REFRESH_TOKEN", self.lwa_refresh_token),
                                            ("SPAPI_MARKETPLACE_ID", self.marketplace_id)) if not value]
        if missing:
            raise ConfigurationError("Missing SP-API settings: " + ", ".join(missing))
        if not self.sp_api_endpoint:
            raise ConfigurationError(f"Unknown marketplace {self.marketplace_id!r}; set SPAPI_ENDPOINT explicitly")


def load_settings(env_file: Path | None = None) -> Settings:
    load_env(env_file)
    zone = _env("SQP_TIMEZONE", DEFAULT_TIMEZONE)
    try:
        timezone = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigurationError("SQP_TIMEZONE is not a known time zone (e.g. America/Los_Angeles)") from None
    marketplace_id = _env("SPAPI_MARKETPLACE_ID")
    endpoint = _env("SPAPI_ENDPOINT") or MARKETPLACE_ENDPOINTS.get(marketplace_id or "")
    return Settings(
        data_dir=Path(_env("SQP_DATA_DIR", str(PROJECT_ROOT / "data"))),
        log_dir=Path(_env("SQP_LOG_DIR", str(PROJECT_ROOT / "logs"))),
        log_level=_env("LOG_LEVEL", "INFO").upper(),
        lwa_client_id=_env("SPAPI_LWA_CLIENT_ID"),
        lwa_client_secret=_env("SPAPI_LWA_CLIENT_SECRET"),
        lwa_refresh_token=_env("SPAPI_LWA_REFRESH_TOKEN"),
        marketplace_id=marketplace_id,
        sp_api_endpoint=endpoint.rstrip("/") if endpoint else None,
        db_host=_db_env("HOST"),
        db_port=_env_int("DB_PORT", 3306) if _env("DB_PORT") else _env_int("POWERBI_DB_PORT", 3306),
        db_user=_db_env("USER"),
        db_password=_db_env("PASSWORD"),
        db_name=_db_env("NAME", "powerbidb"),
        db_table=_db_env("TABLE", "2024BrandAnalytics"),
        expected_asin_count=_env_int("EXPECTED_ASIN_COUNT", 62),
        timezone=timezone,
        availability_lag_days=_env_int("SQP_AVAILABILITY_LAG_DAYS", 2),
        poll_interval_seconds=_env_int("SQP_POLL_INTERVAL_SECONDS", 300),
        poll_timeout_seconds=_env_int("SQP_POLL_TIMEOUT_SECONDS", 22 * 60 * 60),
        max_retries=_env_int("SQP_MAX_RETRIES", 6),
        sc_email=_env("SELLERCENTRAL_EMAIL") or _env("AMAZON_EMAIL"),
        sc_password=_env("SELLERCENTRAL_PASSWORD") or _env("AMAZON_PASSWORD"),
        sc_totp_secret=_env("SELLERCENTRAL_TOTP_SECRET"),
        sc_competitors_from=_env_date("SC_COMPETITORS_FROM", dt.date(2026, 10, 11)),
        sc_browser_channel=_env("SC_BROWSER_CHANNEL", "chrome").lower(),
        sc_headless=_env("SC_HEADLESS", "false").lower() in ("1", "true", "yes"),
    )
