"""SP-API client for the Brand Analytics Search Query Performance report.

Authentication is LWA-only (refresh token -> access token); SP-API no longer
requires AWS SigV4 signing. Reports API flow: createReport -> poll getReport
-> getReportDocument -> download (gzip) JSON, saved to data/reports/.
"""
from __future__ import annotations

import gzip
import json
import random
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Iterable

import requests

from .config import REPORT_TYPE, Settings
from .exceptions import SpApiError
from .logging_config import get_logger

log = get_logger("spapi")

LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"
REPORTS_PATH = "/reports/2021-06-30"
ASIN_OPTION_MAX_CHARS = 200
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
USER_AGENT = "amazon-sqp-weekly-updater/1.0 (Language=Python)"


class ReportFailed(SpApiError):
    """Amazon finished the report with CANCELLED or FATAL status."""

    def __init__(self, report_id: str, status: str, detail: str | None = None):
        self.report_id = report_id
        self.status = status
        self.detail = detail
        msg = f"Report {report_id} ended with status {status}"
        super().__init__(f"{msg}: {detail}" if detail else msg)


class ReportTimeout(SpApiError):
    """Amazon did not finish the report within SQP_POLL_TIMEOUT_SECONDS."""


@dataclass
class ReportResult:
    report_id: str
    document_id: str
    asins: list[str]
    payload: dict
    raw_path: Path | None


def batch_asins(asins: Iterable[str], max_chars: int = ASIN_OPTION_MAX_CHARS) -> list[list[str]]:
    """Split ASINs into groups whose space-joined length fits Amazon's option limit."""
    batches: list[list[str]] = []
    current: list[str] = []
    length = 0
    for asin in asins:
        if len(asin) > max_chars:
            raise ValueError(f"ASIN {asin!r} is longer than the {max_chars}-character limit")
        added = len(asin) + (1 if current else 0)
        if current and length + added > max_chars:
            batches.append(current)
            current, length, added = [], 0, len(asin)
        current.append(asin)
        length += added
    if current:
        batches.append(current)
    return batches


def backoff_delay(attempt: int, base: float = 2.0, cap: float = 300.0, rate_limit: float | None = None) -> float:
    delay = min(cap, base * (2**attempt)) * (0.5 + random.random() / 2)
    if rate_limit and rate_limit > 0:
        delay = max(delay, 1.0 / rate_limit)
    return delay


class SpApiClient:
    def __init__(self, settings: Settings, session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        settings.require_spapi()
        self.settings = settings
        self.session = session or requests.Session()
        self.sleep = sleep
        self._token: str | None = None
        self._token_expires = 0.0

    # ----------------------------------------------------------------------- auth

    def _access_token(self, force: bool = False) -> str:
        if not force and self._token and time.time() < self._token_expires - 60:
            return self._token
        resp = self._send_with_retry(
            "POST",
            LWA_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.settings.lwa_refresh_token,
                "client_id": self.settings.lwa_client_id,
                "client_secret": self.settings.lwa_client_secret,
            },
        )
        if resp.status_code != 200:
            raise SpApiError(f"LWA token request failed ({resp.status_code}): {resp.text[:500]}")
        body = resp.json()
        self._token = body["access_token"]
        self._token_expires = time.time() + int(body.get("expires_in", 3600))
        return self._token

    # ----------------------------------------------------------------------- transport

    def _send_with_retry(self, method: str, url: str, **kwargs) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(self.settings.max_retries + 1):
            try:
                resp = self.session.request(method, url, timeout=60, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = exc
                delay = backoff_delay(attempt)
                log.warning("%s %s failed (%s); retry %d in %.0fs", method, url, exc, attempt + 1, delay)
                self.sleep(delay)
                continue
            if resp.status_code in RETRYABLE_STATUS and attempt < self.settings.max_retries:
                rate = resp.headers.get("x-amzn-RateLimit-Limit")
                delay = backoff_delay(attempt, rate_limit=float(rate) if rate else None)
                log.warning("%s %s -> HTTP %d (request id %s); retry %d in %.0fs", method, url, resp.status_code,
                            resp.headers.get("x-amzn-RequestId"), attempt + 1, delay)
                self.sleep(delay)
                continue
            return resp
        raise SpApiError(f"{method} {url} failed after retries: {last_error}")

    def _api(self, method: str, path: str, **kwargs) -> dict:
        url = self.settings.sp_api_endpoint + path
        for refreshed in (False, True):
            headers = {
                "x-amz-access-token": self._access_token(force=refreshed),
                "user-agent": USER_AGENT,
                "content-type": "application/json",
            }
            resp = self._send_with_retry(method, url, headers=headers, **kwargs)
            if resp.status_code in (401, 403) and not refreshed and "expired" in resp.text.lower():
                continue
            break
        if resp.status_code >= 400:
            hint = ""
            if resp.status_code == 403:
                hint = " (check the app has the Brand Analytics role and the refresh token is for this account)"
            raise SpApiError(f"{method} {path} -> HTTP {resp.status_code}{hint}: {resp.text[:1000]}")
        return resp.json() if resp.content else {}

    # ----------------------------------------------------------------------- reports

    def create_report(self, asins: list[str], start: date, end: date) -> str:
        option = " ".join(asins)
        if len(option) > ASIN_OPTION_MAX_CHARS:
            raise ValueError(f"ASIN option is {len(option)} chars; limit is {ASIN_OPTION_MAX_CHARS}")
        body = {
            "reportType": REPORT_TYPE,
            "marketplaceIds": [self.settings.marketplace_id],
            "dataStartTime": start.isoformat(),
            "dataEndTime": end.isoformat(),
            "reportOptions": {"reportPeriod": "WEEK", "asin": option},
        }
        result = self._api("POST", f"{REPORTS_PATH}/reports", data=json.dumps(body))
        report_id = result["reportId"]
        log.info("Created report %s for %d ASINs, %s..%s", report_id, len(asins), start, end)
        return report_id

    def wait_for_report(self, report_id: str, deadline: float | None = None) -> str:
        """Poll until DONE and return the reportDocumentId (``deadline``: a time.monotonic() value)."""
        if deadline is None:
            deadline = time.monotonic() + self.settings.poll_timeout_seconds
        while True:
            report = self._api("GET", f"{REPORTS_PATH}/reports/{report_id}")
            status = report.get("processingStatus")
            log.info("Report %s status %s", report_id, status)
            if status == "DONE":
                return report["reportDocumentId"]
            if status in ("CANCELLED", "FATAL"):
                detail = None
                if report.get("reportDocumentId"):
                    # FATAL reports usually carry an error document explaining why.
                    try:
                        detail = self.download_document(report["reportDocumentId"]).decode("utf-8", "replace")[:2000]
                    except SpApiError as exc:
                        detail = f"(could not fetch error document: {exc})"
                raise ReportFailed(report_id, status, detail)
            if time.monotonic() >= deadline:
                raise ReportTimeout(f"Report {report_id} still {status} after {self.settings.poll_timeout_seconds}s")
            self.sleep(self.settings.poll_interval_seconds)

    def download_document(self, document_id: str) -> bytes:
        meta = self._api("GET", f"{REPORTS_PATH}/documents/{document_id}")
        # Pre-signed S3 URL: must not carry SP-API headers.
        resp = self._send_with_retry("GET", meta["url"])
        if resp.status_code != 200:
            raise SpApiError(f"Document {document_id} download failed: HTTP {resp.status_code}")
        data = resp.content
        if meta.get("compressionAlgorithm") == "GZIP":
            data = gzip.decompress(data)
        return data

    def fetch_sqp(self, asins: list[str], start: date, end: date, save_dir: Path | None = None,
                  state_file: Path | None = None, refetch: bool = False,
                  downloaded_only: bool = False) -> list[ReportResult]:
        """Request, wait for and download one report per ASIN batch.

        Amazon can keep an SQP report queued for hours, so a run can be cut off. With
        ``state_file`` each batch's report ID is recorded as soon as it is created: the next
        run polls that report instead of queueing a new one, and a batch already downloaded
        to ``save_dir`` is read from there. ``refetch`` ignores both and asks Amazon again.
        ``downloaded_only`` returns the downloaded batches and leaves the others out, without
        calling Amazon.
        """
        state = load_state(state_file) if state_file and not refetch else {}
        state.setdefault("batches", {})
        results: list[ReportResult | None] = []
        pending: list[tuple[int, list[str], str, dict]] = []  # (position, batch, report_id, entry)
        # First every batch gets its report, all requested at once: Amazon queues them together,
        # so the run waits about as long as for one report, not four times as long.
        for batch in batch_asins(asins):
            key = " ".join(batch)
            entry = state["batches"].setdefault(key, {})
            cached = None if refetch else _cached_report(save_dir, start, end, batch, entry)
            if cached:
                log.info("Using downloaded report %s for %d ASINs (%s)", cached.report_id, len(batch),
                         cached.raw_path.name)
                results.append(cached)
                continue
            if downloaded_only:
                log.warning("Left out: %d ASINs not downloaded yet (%s)", len(batch),
                            f"report {entry['report_id']} pending" if entry.get("report_id") else "not requested")
                continue
            report_id = entry.get("report_id")
            if report_id:
                log.info("Resuming report %s for %d ASINs, %s..%s", report_id, len(batch), start, end)
            else:
                report_id = self.create_report(batch, start, end)
                entry.clear()
                entry["report_id"] = report_id
                _save_batch(state_file, key, entry)
            pending.append((len(results), batch, report_id, entry))
            results.append(None)

        deadline = time.monotonic() + self.settings.poll_timeout_seconds
        for position, batch, report_id, entry in pending:
            key = " ".join(batch)
            try:
                document_id = self.wait_for_report(report_id, deadline)
            except ReportFailed:
                _save_batch(state_file, key, None)  # the next run asks Amazon again
                raise
            raw = self.download_document(document_id)
            raw_path = None
            if save_dir:
                save_dir.mkdir(parents=True, exist_ok=True)
                raw_path = save_dir / f"sqp_{start}_{end}_{report_id}.json"
                raw_path.write_bytes(raw)
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise SpApiError(f"Report {report_id} document is not valid JSON: {exc}") from exc
            log.info("Downloaded report %s (document %s, %d bytes)", report_id, document_id, len(raw))
            entry.update(document_id=document_id, raw=raw_path.name if raw_path else None)
            _save_batch(state_file, key, entry)
            results[position] = ReportResult(report_id, document_id, batch, payload, raw_path)
        return [r for r in results if r is not None]


# --------------------------------------------------------------------------- run state


def state_file(state_dir: Path, start: date, end: date) -> Path:
    return state_dir / f"sqp_{start}_{end}.json"


def load_state(path: Path | None) -> dict:
    if path is None or not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SpApiError(f"cannot read run state {path}: {exc}; delete it to start the week again") from None


def save_state(path: Path | None, state: dict) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


def _save_batch(path: Path | None, key: str, entry: dict | None) -> None:
    """Record one batch in the week's state file, keeping whatever else is in it now (another run may
    have marked the week written or done by hand since this run read it). None forgets the batch."""
    if path is None:
        return
    state = load_state(path)
    batches = state.setdefault("batches", {})
    if entry is None:
        batches.pop(key, None)
    else:
        batches[key] = dict(entry)
    save_state(path, state)


def _cached_report(save_dir: Path | None, start: date, end: date, batch: list[str], entry: dict) -> ReportResult | None:
    """A downloaded report of exactly this week and these ASINs, if there is one."""
    if save_dir is None or not save_dir.is_dir():
        return None
    named = save_dir / entry["raw"] if entry.get("raw") else None
    candidates = [named] if named and named.is_file() else sorted(save_dir.glob(f"sqp_{start}_{end}_*.json"))
    for path in candidates:
        try:
            payload = json.loads(path.read_bytes())
        except (OSError, json.JSONDecodeError):
            continue
        spec = payload.get("reportSpecification") or {}
        asked = set(((spec.get("reportOptions") or {}).get("asin") or "").split())
        if (asked == set(batch) and str(spec.get("dataStartTime", ""))[:10] == start.isoformat()
                and str(spec.get("dataEndTime", ""))[:10] == end.isoformat()):
            report_id = path.stem.rsplit("_", 1)[-1]
            return ReportResult(report_id, entry.get("document_id", ""), batch, payload, path)
    return None
