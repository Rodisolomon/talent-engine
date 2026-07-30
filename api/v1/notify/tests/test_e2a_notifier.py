"""Tests for the e2a per-call notifier.

The load-bearing properties are the two in the module docstring: it must
never fail a request, and it must never carry candidate data. Most of what
follows pins those down.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import pytest

from v1.notify import CallEvent, E2ANotifier, build_from_env, render_subject, render_text


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeResult:
    status: str = "sent"
    message_id: str = "msg_test"


class _FakeMessages:
    def __init__(self, *, fail: bool = False, status: str = "sent", delay: float = 0.0):
        self.sends: List[tuple[str, Dict[str, Any]]] = []
        self._fail = fail
        self._status = status
        self._delay = delay

    async def send(self, email: str, body: Dict[str, Any], **kwargs: Any) -> _FakeResult:
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._fail:
            raise RuntimeError("e2a is down")
        self.sends.append((email, body))
        return _FakeResult(status=self._status)


class _FakeClient:
    def __init__(self, **kwargs: Any) -> None:
        self.messages = _FakeMessages(**kwargs)
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def _event(**overrides: Any) -> CallEvent:
    base: Dict[str, Any] = dict(
        endpoint="match",
        method="POST",
        path="/v1/resume-matching/match",
        http_status=200,
        outcome="ok",
        elapsed_ms=4200,
        request_id="req_7f3a91",
        api_key_name="partner-acme",
        api_key_id=3,
        llm_provider="DeepSeek",
        counts={"resumes": 3, "jobs": 2, "pairs": 6},
    )
    base.update(overrides)
    return CallEvent(**base)


async def _make(client: Optional[_FakeClient] = None, **kwargs: Any) -> E2ANotifier:
    n = E2ANotifier(
        api_key="e2a_agt_test",
        agent_email="talent-engine@team.tokencanopy.com",
        recipients=["ops@example.com"],
        client=client or _FakeClient(),
        **kwargs,
    )
    await n.start()
    return n


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_subject_and_body_carry_metadata():
    ev = _event()
    assert render_subject(ev) == "[talent-engine] match — 3 resumes, 2 jobs, 6 pairs"
    body = render_text(ev)
    assert "POST /v1/resume-matching/match" in body
    assert "partner-acme" in body
    assert "req_7f3a91" in body
    assert "4.2s" in body
    assert "DeepSeek" in body


def test_failures_are_flagged_in_the_subject():
    ev = _event(outcome="error", http_status=500, error="TimeoutError: upstream")
    assert render_subject(ev).startswith("[talent-engine] FAILED match")
    assert "TimeoutError: upstream" in render_text(ev)


def test_poll_event_renders_job_state():
    ev = _event(
        endpoint="match_poll", method="GET", job_id="rmj_abc",
        note="running", counts={"pairs_done": 4, "pairs_total": 6},
        llm_provider=None, request_id=None,
    )
    assert "running" in render_subject(ev)
    body = render_text(ev)
    assert "rmj_abc" in body
    assert "4 pairs_done, 6 pairs_total" in body


def test_call_event_has_no_field_that_could_hold_candidate_data():
    """Guards the PII boundary — a new free-text field should fail here."""
    allowed = {
        "endpoint", "method", "path", "http_status", "outcome", "elapsed_ms",
        "request_id", "api_key_name", "api_key_id", "llm_provider",
        "client_ip", "job_id", "note", "counts", "error",
    }
    assert set(CallEvent.__dataclass_fields__) == allowed


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


async def test_notify_sends_one_email_per_event():
    client = _FakeClient()
    n = await _make(client)
    n.notify(_event())
    n.notify(_event(endpoint="parse"))
    await n.stop()

    assert len(client.messages.sends) == 2
    to_addr, body = client.messages.sends[0]
    assert to_addr == "talent-engine@team.tokencanopy.com"
    assert body["to"] == ["ops@example.com"]
    assert body["subject"].startswith("[talent-engine] match")
    assert "html" not in body           # plain text only — nothing to render
    assert n.stats["sent"] == 2


async def test_every_poll_produces_its_own_email():
    """The user asked for one email per call, polls included."""
    client = _FakeClient()
    n = await _make(client)
    for i in range(30):
        n.notify(_event(endpoint="match_poll", job_id="rmj_x", note="running"))
    await n.stop()
    assert len(client.messages.sends) == 30


async def test_send_failure_never_escapes():
    n = await _make(_FakeClient(fail=True))
    n.notify(_event())          # must not raise
    await n.stop()
    assert n.stats["failed"] == 1
    assert n.stats["sent"] == 0


async def test_pending_review_counts_as_delivered_and_is_not_retried():
    client = _FakeClient(status="pending_review")
    n = await _make(client)
    n.notify(_event())
    await n.stop()
    assert len(client.messages.sends) == 1     # exactly one attempt
    assert n.stats["sent"] == 1


async def test_terminal_failure_status_is_counted_not_retried():
    client = _FakeClient(status="failed")
    n = await _make(client)
    n.notify(_event())
    await n.stop()
    assert len(client.messages.sends) == 1
    assert n.stats == {"sent": 0, "failed": 1, "dropped": 0}


async def test_queue_sheds_instead_of_growing_without_limit():
    """A poll flood must cost bounded memory, not unbounded backlog."""
    client = _FakeClient(delay=5.0)            # workers stall
    n = await _make(client, queue_max=3, workers=1)
    for _ in range(50):
        n.notify(_event(endpoint="match_poll"))

    assert n.stats["dropped"] >= 45
    # Nothing raised into the caller, and memory stayed bounded.
    for task in n._workers:
        task.cancel()
    await asyncio.gather(*n._workers, return_exceptions=True)
    n._workers = []


async def test_notify_is_synchronous_and_returns_immediately():
    """The request path must never await mail delivery."""
    n = await _make(_FakeClient(delay=5.0))
    loop = asyncio.get_running_loop()
    start = loop.time()
    n.notify(_event())
    assert loop.time() - start < 0.05

    for task in n._workers:
        task.cancel()
    await asyncio.gather(*n._workers, return_exceptions=True)
    n._workers = []


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


async def test_disabled_when_unconfigured_and_notify_is_a_noop():
    n = E2ANotifier(api_key=None, agent_email=None, recipients=[])
    assert not n.enabled
    await n.start()
    n.notify(_event())          # must not raise
    await n.stop()


async def test_partial_config_stays_disabled():
    n = E2ANotifier(
        api_key="e2a_agt_test", agent_email="a@b.com", recipients=[],
    )
    assert not n.enabled


async def test_kill_switch_disables_a_fully_configured_notifier():
    n = E2ANotifier(
        api_key="e2a_agt_test",
        agent_email="a@b.com",
        recipients=["ops@example.com"],
        enabled=False,
    )
    assert not n.enabled


def test_build_from_env_reads_and_splits_recipients(monkeypatch):
    monkeypatch.setenv("E2A_API_KEY", "e2a_agt_test")
    monkeypatch.setenv("E2A_AGENT_EMAIL", "talent-engine@team.tokencanopy.com")
    monkeypatch.setenv("E2A_NOTIFY_TO", "a@example.com, b@example.com ")
    n = build_from_env()
    assert n.enabled
    assert n._recipients == ["a@example.com", "b@example.com"]


def test_malformed_int_config_falls_back_instead_of_raising(monkeypatch):
    """A typo'd tuning knob must not take the API down on every request."""
    monkeypatch.setenv("E2A_API_KEY", "e2a_agt_test")
    monkeypatch.setenv("E2A_AGENT_EMAIL", "talent-engine@team.tokencanopy.com")
    monkeypatch.setenv("E2A_NOTIFY_TO", "ops@example.com")
    monkeypatch.setenv("E2A_NOTIFY_QUEUE_MAX", "not-a-number")
    monkeypatch.setenv("E2A_NOTIFY_WORKERS", "")

    n = build_from_env()
    assert n.enabled
    assert n._queue_max == 1000
    assert n._worker_count == 2


def test_dispatch_survives_a_broken_notifier(monkeypatch):
    """notify_api_call is the last line of defence for the request path."""
    from v1.notify import e2a_notifier as mod

    class _Exploding:
        def notify(self, event):
            raise RuntimeError("boom")

    mod.set_notifier(_Exploding())      # type: ignore[arg-type]
    try:
        mod.notify_api_call(_event())   # must not raise
    finally:
        mod.set_notifier(None)


def test_build_from_env_disabled_without_recipients(monkeypatch):
    monkeypatch.setenv("E2A_API_KEY", "e2a_agt_test")
    monkeypatch.setenv("E2A_AGENT_EMAIL", "talent-engine@team.tokencanopy.com")
    monkeypatch.delenv("E2A_NOTIFY_TO", raising=False)
    assert not build_from_env().enabled
