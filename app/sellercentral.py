"""Seller Central in a real Chrome window, with a saved sign-in (data/browser/sellercentral-profile/).

Seller Central requires 2-step verification. ``login`` fills the email and password from
.env (SELLERCENTRAL_EMAIL / SELLERCENTRAL_PASSWORD); the person at the PC types the OTP and
ticks "Don't require OTP on this browser", which this Chrome profile then remembers. Only if
SELLERCENTRAL_TOTP_SECRET is set (an authenticator-app key) is the code generated here.
Passwords and codes are never logged.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import struct
import time
from pathlib import Path

from .config import Settings
from .exceptions import ConfigurationError, PipelineError
from .logging_config import get_logger

log = get_logger("sellercentral")

HOME_URL = "https://sellercentral.amazon.com/home"
SIGN_IN_TIMEOUT_SECONDS = 300


class SellerCentralError(PipelineError):
    """Seller Central sign-in needed, or its page is not as expected."""

    exit_code = 4


def _profile_dir(settings: Settings) -> Path:
    return settings.data_dir / "browser" / "sellercentral-profile"


def _open(settings: Settings, headless: bool = False):
    """Chrome with this project's own profile (not the person's everyday Chrome profile)."""
    from playwright.sync_api import sync_playwright

    profile = _profile_dir(settings)
    profile.mkdir(parents=True, exist_ok=True)
    playwright = sync_playwright().start()
    try:
        context = playwright.chromium.launch_persistent_context(
            str(profile), channel=None if settings.sc_browser_channel == "chromium" else settings.sc_browser_channel,
            headless=headless or settings.sc_headless, accept_downloads=True,
            viewport={"width": 1440, "height": 900}, args=["--disable-blink-features=AutomationControlled"])
    except Exception:
        playwright.stop()
        raise
    return playwright, context


def _close(context, playwright) -> None:
    """Close Chrome; if the person already closed the window, there is nothing left to close."""
    try:
        context.close()
    except Exception:
        pass
    playwright.stop()


def _totp(secret: str, at: float | None = None) -> str:
    """RFC 6238 code (30 s, 6 digits, SHA-1), as an authenticator app shows it."""
    key = base64.b32decode(secret.replace(" ", "").upper() + "=" * (-len(secret.replace(" ", "")) % 8))
    counter = int((at or time.time()) // 30)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    return f"{(struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def _signed_in(page) -> bool:
    """On a Seller Central page that is not Amazon's sign-in / OTP form (judged by the path: after
    signing in, the home page's address can still carry "signin" in its query string)."""
    from urllib.parse import urlsplit

    for candidate in page.context.pages:
        parts = urlsplit(candidate.url)
        if (parts.hostname or "").endswith("sellercentral.amazon.com") and not parts.path.startswith("/ap/") \
                and "signin" not in parts.path.lower():
            return True
    return False


def _fill_if_present(page, selector: str, value: str, timeout_ms: int = 4000) -> bool:
    try:
        page.wait_for_selector(selector, state="visible", timeout=timeout_ms)
    except Exception:
        return False
    page.fill(selector, value)
    return True


def _click_if_present(page, selector: str, timeout_ms: int = 3000) -> bool:
    try:
        page.wait_for_selector(selector, state="visible", timeout=timeout_ms)
    except Exception:
        return False
    page.click(selector)
    return True


def sign_in(settings: Settings, page, wait_for_person: bool) -> None:
    """Fill the sign-in form if Amazon shows it; OTP comes from the person (or the TOTP key)."""
    if _signed_in(page):
        return
    if not (settings.sc_email and settings.sc_password):
        raise ConfigurationError("set SELLERCENTRAL_EMAIL and SELLERCENTRAL_PASSWORD in .env")
    if _fill_if_present(page, "#ap_email", settings.sc_email):
        _click_if_present(page, "#continue")
    if _fill_if_present(page, "#ap_password", settings.sc_password):
        _click_if_present(page, "input[name=rememberMe]", 1000)
        _click_if_present(page, "#signInSubmit")
    if settings.sc_totp_secret and _fill_if_present(page, "#auth-mfa-otpcode", _totp(settings.sc_totp_secret)):
        _click_if_present(page, "#auth-mfa-remember-device", 1000)
        _click_if_present(page, "#auth-signin-button")
    deadline = time.monotonic() + (SIGN_IN_TIMEOUT_SECONDS if wait_for_person else 20)
    if wait_for_person and not _signed_in(page):
        print("In the Chrome window: enter the OTP, tick \"Don't require OTP on this browser\", and choose the "
              f"account / marketplace (United States) if asked. Waiting up to {SIGN_IN_TIMEOUT_SECONDS // 60} minutes...")
    while not _signed_in(page):
        if page.is_closed():
            raise SellerCentralError("the Chrome window was closed before Seller Central showed its home page; "
                                     "run --sc-login again and leave the window open")
        if time.monotonic() > deadline:
            where = page.url.split("?")[0]
            shot = settings.data_dir / "screenshots" / f"sellercentral_signin_{dt.datetime.now():%Y%m%d_%H%M%S}.png"
            try:
                shot.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(shot))
            except Exception:
                shot = None
            raise SellerCentralError(f"Seller Central sign-in not completed (still on {where}); screenshot: {shot}")
        time.sleep(1)


def login(settings: Settings, terminal: bool = False) -> str:
    """One-time sign-in; the browser profile keeps it. terminal: no window (a server): the OTP is
    typed in this terminal, and "Don't require OTP on this browser" is ticked."""
    playwright, context = _open(settings, headless=terminal)
    try:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(HOME_URL, wait_until="domcontentloaded")
        if terminal and not _signed_in(page):
            _terminal_sign_in(settings, page)
        sign_in(settings, page, wait_for_person=True)
        page.wait_for_load_state("domcontentloaded")
        log.info("Seller Central signed in (%s)", page.url.split("?")[0])
        return page.url
    finally:
        _close(context, playwright)


def _settle(page, timeout_ms: int = 30000) -> None:
    """Wait until Seller Central stops loading (redirects, account selection, the page's scripts)."""
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        pass
    time.sleep(2)


def _terminal_sign_in(settings: Settings, page) -> None:
    """Email and password from .env; the OTP from the person at the terminal; the account picker by name."""
    if not (settings.sc_email and settings.sc_password):
        raise ConfigurationError("set SELLERCENTRAL_EMAIL and SELLERCENTRAL_PASSWORD (or AMAZON_*) in .env")
    if _fill_if_present(page, "#ap_email", settings.sc_email, 15000):
        _click_if_present(page, "#continue")
    if _fill_if_present(page, "#ap_password", settings.sc_password, 15000):
        _click_if_present(page, "input[name=rememberMe]", 1000)
        _click_if_present(page, "#signInSubmit")
    for _ in range(3):
        if not _fill_if_present(page, "#auth-mfa-otpcode", "", 15000):
            break
        code = input("Seller Central OTP (sent to your phone / authenticator): ").strip()
        page.fill("#auth-mfa-otpcode", code)
        _click_if_present(page, "#auth-mfa-remember-device", 1000)
        _click_if_present(page, "#auth-signin-button")
        _settle(page)
        if "/ap/mfa" not in page.url:
            break
        print("That code was not accepted; try again.")
    _settle(page)
    if "account-switcher" in page.url or page.locator("text=Select an account").count():
        try:  # Econour / United States, as in the browser
            page.get_by_text("Econour", exact=False).first.click()
            page.get_by_text("United States", exact=False).first.click()
            _click_if_present(page, "button:has-text('Select account')", 5000)
        except Exception:
            print("Could not choose the account by itself; the screenshot shows the page.")
    if not _signed_in(page):
        shot = settings.data_dir / "screenshots" / f"sellercentral_terminal_signin_{dt.datetime.now():%Y%m%d_%H%M%S}.png"
        shot.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(shot))
        raise SellerCentralError(f"sign-in did not reach Seller Central (on {page.url.split('?')[0]}); screenshot: {shot}")


def inspect(settings: Settings, url: str) -> tuple[Path, Path]:
    """Open ``url`` with the saved sign-in; save a full-page screenshot and the page's buttons and links."""
    playwright, context = _open(settings, headless=False)
    try:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(url, wait_until="domcontentloaded")
        sign_in(settings, page, wait_for_person=False)
        _settle(page)
        # The first page of a fresh browser goes through the account / marketplace selection and
        # lands on Home; once that is done, the link opens.
        if page.url.split("?")[0] != url.split("?")[0]:
            page.goto(url, wait_until="domcontentloaded")
            _settle(page)
        try:  # the page's own content, not just the frame with a loading spinner
            page.wait_for_function("""() => /Search Query (Details|Performance)|Access Required|Brand Analytics/
                                          .test(document.body.innerText)""", timeout=45000)
        except Exception:
            pass
        time.sleep(3)
        stamp = f"{dt.datetime.now():%Y%m%d_%H%M%S}"
        folder = settings.data_dir / "screenshots"
        folder.mkdir(parents=True, exist_ok=True)
        shot = folder / f"sellercentral_{stamp}.png"
        page.screenshot(path=str(shot), full_page=True)
        controls = page.evaluate("""() => [...document.querySelectorAll(
            'button, a, [role=button], kat-button, kat-link, kat-dropdown, kat-tab, select, input')]
            .map(e => ({tag: e.tagName.toLowerCase(), id: e.id || null,
                        text: (e.innerText || e.getAttribute('label') || e.getAttribute('aria-label')
                               || e.value || '').trim().slice(0, 80),
                        href: e.getAttribute('href'), visible: !!(e.offsetWidth || e.offsetHeight)}))
            .filter(c => c.text || c.id)""")
        listing = folder / f"sellercentral_{stamp}_controls.json"
        listing.write_text(json.dumps({"url": page.url, "title": page.title(), "controls": controls}, indent=1),
                           encoding="utf-8")
        log.info("Seller Central page saved: %s, %s", shot.name, listing.name)
        return shot, listing
    finally:
        _close(context, playwright)
