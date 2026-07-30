"""Integration tests: does every API call actually emit one notification?

Runs against the real `main.app` — middleware stack included — because the
interesting failure modes live in the wiring, not the notifier: a handler
that forgets to emit, a fallback middleware that double-sends, or a
middleware ordering that loses the request id.
"""

from __future__ import annotations

import asyncio
from typing import Dict, List, Tuple

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

import main as main_mod
from v1.notify import CallEvent, set_notifier
from v1.resume_matching import pipeline as pipeline_mod
from v1.resume_matching import public_router as public_router_mod
from v1.resume_matching.baml_client.types import MatchScore
from v1.resume_matching.storage import ApiKeyStore
from v1.resume_matching.storage.schema import metadata
from v1.routers.deps import get_engine


class _RecordingNotifier:
    """Stands in for the singleton; records instead of mailing."""

    def __init__(self) -> None:
        self.events: List[CallEvent] = []
        self.enabled = True

    def notify(self, event: CallEvent) -> None:
        self.events.append(event)

    def by_endpoint(self, name: str) -> List[CallEvent]:
        return [e for e in self.events if e.endpoint == name]


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def api_key(engine) -> str:
    plaintext, _ = await ApiKeyStore(engine).create(name="partner-acme")
    return plaintext


@pytest.fixture
def notifier():
    n = _RecordingNotifier()
    set_notifier(n)                     # type: ignore[arg-type]
    yield n
    set_notifier(None)


@pytest_asyncio.fixture
async def client(engine):
    main_mod.app.dependency_overrides[get_engine] = lambda: engine
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main_mod.app),
        base_url="http://test",
    ) as c:
        yield c
    main_mod.app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def clear_async_jobs():
    public_router_mod._jobs._jobs.clear()
    yield
    public_router_mod._jobs._jobs.clear()


class _BamlStub:
    async def ScoreMatch(self, *, resume, job, baml_options=None) -> MatchScore:  # noqa: N802
        return MatchScore(
            score=50, verdict="可推荐", hard_fails=[],
            strengths=[], gaps=[], reasoning="stub",
        )


@pytest.fixture
def baml(monkeypatch):
    monkeypatch.setattr(pipeline_mod, "b", _BamlStub())


def _body() -> Dict:
    return {
        "resumes": [{"resume_id": "r1", "resume": {"name": "张伟"}}],
        "jobs": [{"job_id": "j1", "job": {"company": "Acme", "position": "后端"}}],
    }


# ---------------------------------------------------------------------------


async def test_sync_match_emits_exactly_one_event(client, api_key, notifier, baml):
    r = await client.post(
        "/v1/resume-matching/match",
        json=_body(),
        headers={"X-API-Key": api_key},
    )
    assert r.status_code == 200
    assert len(notifier.events) == 1, "handler + fallback must not both fire"

    ev = notifier.events[0]
    assert ev.endpoint == "match"
    assert ev.http_status == 200
    assert ev.outcome == "ok"
    assert ev.api_key_name == "partner-acme"
    assert ev.counts == {"resumes": 1, "jobs": 1, "pairs": 1, "failed": 0}
    assert ev.request_id, "request id middleware must run before the handler"


async def test_missing_api_key_still_notifies_via_fallback(client, notifier):
    r = await client.post("/v1/resume-matching/match", json=_body())
    assert r.status_code == 401
    assert len(notifier.events) == 1

    ev = notifier.events[0]
    assert ev.endpoint == "unhandled"
    assert ev.http_status == 401
    assert ev.outcome == "error"
    assert ev.request_id


async def test_validation_rejection_notifies_via_fallback(client, api_key, notifier):
    r = await client.post(
        "/v1/resume-matching/match",
        json={"resumes": [], "jobs": []},
        headers={"X-API-Key": api_key},
    )
    assert r.status_code == 400
    assert len(notifier.events) == 1
    assert notifier.events[0].endpoint == "unhandled"
    assert notifier.events[0].http_status == 400


async def test_health_is_not_notified(client, notifier):
    await client.get("/health")
    assert notifier.events == []


async def test_async_accept_and_every_poll_emit_events(client, api_key, notifier, baml):
    r = await client.post(
        "/v1/resume-matching/match/async",
        json=_body(),
        headers={"X-API-Key": api_key},
    )
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    accepted = notifier.by_endpoint("match_async")
    assert len(accepted) == 1
    assert accepted[0].http_status == 202
    assert accepted[0].job_id == job_id

    # Poll repeatedly — the user asked for one email per call, so each poll
    # gets its own event even though the payload barely changes.
    for _ in range(5):
        pr = await client.get(
            f"/v1/resume-matching/match/{job_id}",
            headers={"X-API-Key": api_key},
        )
        assert pr.status_code == 200
        await asyncio.sleep(0)

    polls = notifier.by_endpoint("match_poll")
    assert len(polls) == 5
    assert all(p.job_id == job_id for p in polls)
    assert all(p.method == "GET" for p in polls)


async def test_poll_of_unknown_job_notifies_as_error(client, api_key, notifier):
    r = await client.get(
        "/v1/resume-matching/match/rmj_nope",
        headers={"X-API-Key": api_key},
    )
    assert r.status_code == 404

    polls = notifier.by_endpoint("match_poll")
    assert len(polls) == 1
    assert polls[0].http_status == 404
    assert polls[0].outcome == "error"
    assert polls[0].note == "not_found"


async def test_notifier_failure_does_not_break_the_request(client, api_key, baml):
    class _Exploding:
        enabled = True

        def notify(self, event):
            raise RuntimeError("notifier is broken")

    set_notifier(_Exploding())          # type: ignore[arg-type]
    try:
        r = await client.post(
            "/v1/resume-matching/match",
            json=_body(),
            headers={"X-API-Key": api_key},
        )
        assert r.status_code == 200, "a broken notifier must not fail the API"
    finally:
        set_notifier(None)
