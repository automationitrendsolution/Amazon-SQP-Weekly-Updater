"""SpApiClient against a fake HTTP session: report flow, retries, failed and slow reports."""
from __future__ import annotations

import dataclasses
import gzip
import json
from datetime import date

import pytest

from app import config
from app.spapi import ReportFailed, ReportTimeout, SpApiClient
from tests.conftest import make_item, make_report

START, END = date(2026, 10, 11), date(2026, 10, 17)


class FakeResponse:
    def __init__(self, status=200, body=None, content=None, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.content = content if content is not None else (json.dumps(body).encode() if body is not None else b"")
        self.text = self.content.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.content)


class FakeSession:
    """Answers by (method, URL suffix); a list answers successive calls in order."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def request(self, method, url, timeout=None, **kwargs):
        self.calls.append((method, url))
        for (m, suffix), answer in self.routes.items():
            if m == method and url.endswith(suffix):
                return answer.pop(0) if isinstance(answer, list) else answer
        raise AssertionError(f"unexpected {method} {url}")


@pytest.fixture
def settings(monkeypatch):
    for name, value in (("SPAPI_LWA_CLIENT_ID", "id"), ("SPAPI_LWA_CLIENT_SECRET", "secret"),
                        ("SPAPI_LWA_REFRESH_TOKEN", "Atzr|token"), ("SPAPI_MARKETPLACE_ID", "ATVPDKIKX0DER"),
                        ("SQP_MAX_RETRIES", "2"), ("SQP_POLL_TIMEOUT_SECONDS", "0")):
        monkeypatch.setenv(name, value)
    return config.load_settings()


TOKEN = FakeResponse(body={"access_token": "token", "expires_in": 3600})


def test_report_is_created_polled_and_downloaded(settings, tmp_path):
    payload = make_report([make_item()])
    session = FakeSession({
        ("POST", "/auth/o2/token"): TOKEN,
        ("POST", "/reports"): FakeResponse(202, {"reportId": "R1"}),
        ("GET", "/reports/R1"): [FakeResponse(body={"processingStatus": "IN_PROGRESS"}),
                                 FakeResponse(body={"processingStatus": "DONE", "reportDocumentId": "D1"})],
        ("GET", "/documents/D1"): FakeResponse(body={"url": "https://s3.example/doc", "compressionAlgorithm": "GZIP"}),
        ("GET", "https://s3.example/doc"): FakeResponse(content=gzip.compress(json.dumps(payload).encode())),
    })
    settings = dataclasses.replace(settings, poll_timeout_seconds=60)
    [result] = SpApiClient(settings, session, sleep=lambda s: None).fetch_sqp(["B0TESTAAAA"], START, END, tmp_path)
    assert (result.report_id, result.document_id, result.payload) == ("R1", "D1", payload)
    assert result.raw_path.read_bytes() == json.dumps(payload).encode()


def test_fatal_report_fails_with_amazons_reason(settings):
    session = FakeSession({
        ("POST", "/auth/o2/token"): TOKEN,
        ("GET", "/reports/R1"): FakeResponse(body={"processingStatus": "FATAL", "reportDocumentId": "E1"}),
        ("GET", "/documents/E1"): FakeResponse(body={"url": "https://s3.example/err"}),
        ("GET", "https://s3.example/err"): FakeResponse(content=b'{"errorDetails":"week not available"}'),
    })
    with pytest.raises(ReportFailed, match="week not available"):
        SpApiClient(settings, session, sleep=lambda s: None).wait_for_report("R1")


def test_report_still_running_at_the_deadline_times_out(settings):
    session = FakeSession({("POST", "/auth/o2/token"): TOKEN,
                           ("GET", "/reports/R1"): FakeResponse(body={"processingStatus": "IN_QUEUE"})})
    with pytest.raises(ReportTimeout):
        SpApiClient(settings, session, sleep=lambda s: None).wait_for_report("R1")


def test_throttled_calls_are_retried(settings):
    waits = []
    session = FakeSession({
        ("POST", "/auth/o2/token"): TOKEN,
        ("POST", "/reports"): [FakeResponse(429, {"errors": []}), FakeResponse(202, {"reportId": "R2"})],
    })
    assert SpApiClient(settings, session, sleep=waits.append).create_report(["B0TESTAAAA"], START, END) == "R2"
    assert len(waits) == 1


def _done_routes(report_id, payload):
    return {
        ("POST", "/auth/o2/token"): TOKEN,
        ("GET", f"/reports/{report_id}"): FakeResponse(body={"processingStatus": "DONE", "reportDocumentId": "D9"}),
        ("GET", "/documents/D9"): FakeResponse(body={"url": "https://s3.example/d9"}),
        ("GET", "https://s3.example/d9"): FakeResponse(content=json.dumps(payload).encode()),
    }


def test_an_interrupted_week_resumes_the_same_report(settings, tmp_path):
    from app.spapi import load_state, save_state
    state = tmp_path / "state.json"
    save_state(state, {"batches": {"B0TESTAAAA": {"report_id": "R7"}}})
    session = FakeSession(_done_routes("R7", make_report([make_item()])))  # no POST /reports route
    [result] = SpApiClient(settings, session, sleep=lambda s: None).fetch_sqp(["B0TESTAAAA"], START, END,
                                                                              tmp_path, state)
    assert result.report_id == "R7" and all(m == "GET" or "token" in u for m, u in session.calls)
    assert load_state(state)["batches"]["B0TESTAAAA"]["raw"] == "sqp_2026-10-11_2026-10-17_R7.json"


def test_a_downloaded_batch_is_not_requested_again(settings, tmp_path):
    payload = make_report([make_item()])
    (tmp_path / "sqp_2026-10-11_2026-10-17_R5.json").write_text(json.dumps(payload), encoding="utf-8")
    session = FakeSession({})  # any HTTP call fails the test
    [result] = SpApiClient(settings, session).fetch_sqp(["B0TESTAAAA"], START, END, tmp_path, tmp_path / "s.json")
    assert (result.report_id, result.payload) == ("R5", payload) and session.calls == []


def test_a_fatal_report_is_forgotten_so_the_next_run_asks_again(settings, tmp_path):
    from app.spapi import load_state, save_state
    state = tmp_path / "state.json"
    save_state(state, {"batches": {"B0TESTAAAA": {"report_id": "R8"}}})
    session = FakeSession({("POST", "/auth/o2/token"): TOKEN,
                           ("GET", "/reports/R8"): FakeResponse(body={"processingStatus": "CANCELLED"})})
    with pytest.raises(ReportFailed):
        SpApiClient(settings, session, sleep=lambda s: None).fetch_sqp(["B0TESTAAAA"], START, END, tmp_path, state)
    assert load_state(state)["batches"] == {}


def test_downloaded_only_leaves_pending_batches_out_without_calling_amazon(settings, tmp_path):
    from app.spapi import save_state
    asins = [f"B0{i:08d}" for i in range(20)]  # two batches: 18 + 2
    payload = make_report([make_item(asin=asins[0])])
    payload["reportSpecification"]["reportOptions"]["asin"] = " ".join(asins[:18])
    (tmp_path / "sqp_2026-10-11_2026-10-17_R1.json").write_text(json.dumps(payload), encoding="utf-8")
    state = tmp_path / "state.json"
    save_state(state, {"batches": {" ".join(asins[18:]): {"report_id": "R2"}}})
    session = FakeSession({})  # any HTTP call fails the test
    results = SpApiClient(settings, session).fetch_sqp(asins, START, END, tmp_path, state, downloaded_only=True)
    assert [r.report_id for r in results] == ["R1"] and session.calls == []


def test_all_reports_of_a_week_are_requested_before_waiting(settings, tmp_path):
    asins = [f"B0{i:08d}" for i in range(20)]  # two batches
    order = []
    created = iter(["R1", "R2"])

    class Client(SpApiClient):
        def create_report(self, batch, start, end):
            order.append("create")
            return next(created)

        def wait_for_report(self, report_id, deadline=None):
            order.append(f"wait {report_id}")
            return "D-" + report_id

        def download_document(self, document_id):
            return json.dumps(make_report([make_item(asin=asins[0])])).encode()

    results = Client(settings, FakeSession({})).fetch_sqp(asins, START, END, tmp_path, tmp_path / "s.json")
    assert order == ["create", "create", "wait R1", "wait R2"]
    assert [r.report_id for r in results] == ["R1", "R2"]
