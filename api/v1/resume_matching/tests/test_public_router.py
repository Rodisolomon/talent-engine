"""End-to-end tests for the public resume-matching JSON API.

Covers:
  - Sync POST /match: happy path, validation, auth, usage logging
  - Async POST /match/async + GET /match/{job_id}: queue → run → completed
  - Auth: missing key, invalid key, revoked key
  - Pair-level error reporting

The BAML client is stubbed so tests don't need a real LLM. The DB engine
is overridden to point at SQLite in-memory.
"""

from __future__ import annotations

import asyncio
from typing import Dict, List, Tuple

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from v1.resume_matching import pipeline as pipeline_mod
from v1.resume_matching.public_schema import SCORE_WEIGHTS
from v1.resume_matching import public_router as public_router_mod
from v1.resume_matching.baml_client.types import MatchScore
from v1.resume_matching.public_router import router
from v1.resume_matching.storage import ApiKeyStore
from v1.resume_matching.storage.schema import metadata, v1_resume_matching_usage
from v1.routers.deps import get_engine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def api_key(engine) -> str:
    plaintext, _ = await ApiKeyStore(engine).create(name="test-key")
    return plaintext


@pytest_asyncio.fixture
async def app(engine):
    a = FastAPI()
    a.include_router(router)
    a.dependency_overrides[get_engine] = lambda: engine
    yield a


@pytest_asyncio.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c


@pytest.fixture(autouse=True)
def clear_async_jobs():
    """Each test starts with a fresh async-job store.

    The store is module-global so a leftover entry from a previous test
    could leak into a poll. Clearing both before and after keeps tests
    independent regardless of ordering.
    """
    public_router_mod._jobs._jobs.clear()
    yield
    public_router_mod._jobs._jobs.clear()


# ---------------------------------------------------------------------------
# BAML stub
# ---------------------------------------------------------------------------


def _score_obj(score: int) -> MatchScore:
    """Build a MatchScore whose five dimensions sum to `score`."""
    out, left = {}, score
    for field, cap in SCORE_WEIGHTS.items():
        out[field] = max(0, min(left, cap))
        left -= out[field]
    return MatchScore(
        **out,
        hard_fails=[], strengths=[f"匹配度 {score}"],
        gaps=[], reasoning=f"score={score}",
    )


class BamlStub:
    """Stub for `pipeline.b` — returns canned scores for ScoreMatch.

    Per-pair overrides via `score_overrides[(resume_name, job_company)]`.
    Default score is 50 so untargeted pairs still produce a result.
    """

    def __init__(self) -> None:
        self.score_overrides: Dict[Tuple[str, str], int] = {}
        self.score_errors: Dict[Tuple[str, str], Exception] = {}
        self.calls = 0
        self.score_latency = 0.0

    async def ScoreMatch(  # noqa: N802
        self, *, resume, job, today: str = "", baml_options=None,
    ) -> MatchScore:
        self.calls += 1
        if self.score_latency:
            await asyncio.sleep(self.score_latency)
        key = (resume.name, job.company)
        if key in self.score_errors:
            raise self.score_errors[key]
        return _score_obj(self.score_overrides.get(key, 50))


@pytest.fixture
def baml(monkeypatch) -> BamlStub:
    stub = BamlStub()
    monkeypatch.setattr(pipeline_mod, "b", stub)
    return stub


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _request(
    *,
    resumes: List[Tuple[str, str]],
    jobs: List[Tuple[str, str, str]],
) -> Dict:
    """Build a MatchRequest body. resumes=[(id,name)], jobs=[(id,company,position)]."""
    return {
        "resumes": [
            {"resume_id": rid, "resume": {"name": name}}
            for rid, name in resumes
        ],
        "jobs": [
            {"job_id": jid, "job": {"company": company, "position": position}}
            for jid, company, position in jobs
        ],
    }


async def _usage_rows(engine) -> List[Dict]:
    async with engine.connect() as conn:
        result = await conn.execute(select(v1_resume_matching_usage))
        return [dict(r._mapping) for r in result.fetchall()]


# ---------------------------------------------------------------------------
# Sync /match — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_match_returns_all_pairs(client, api_key, baml) -> None:
    body = _request(
        resumes=[("r1", "张三"), ("r2", "李四")],
        jobs=[("j1", "A公司", "P"), ("j2", "B公司", "P")],
    )
    baml.score_overrides = {
        ("张三", "A公司"): 90, ("张三", "B公司"): 60,
        ("李四", "A公司"): 70, ("李四", "B公司"): 80,
    }
    resp = await client.post(
        "/v1/resume-matching/match",
        json=body, headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "completed"
    # Flat array — 2×2 = 4 pairs, every (resume_id, job_id) combination present.
    pairs = {(m["resume_id"], m["job_id"]): m for m in data["matches"]}
    assert set(pairs) == {("r1", "j1"), ("r1", "j2"), ("r2", "j1"), ("r2", "j2")}
    assert pairs[("r1", "j1")]["score"] == 90
    assert pairs[("r2", "j2")]["score"] == 80
    assert data["errors"] == []
    assert data["stats"]["pairs_scored"] == 4
    assert data["stats"]["pairs_failed"] == 0


@pytest.mark.asyncio
async def test_sync_match_surfaces_per_pair_errors_alongside_successes(
    client, api_key, baml,
) -> None:
    # One pair raises — the other three should still come back. The
    # client gets a partial-success response with the failure surfaced
    # in `errors`, not a 500 for the whole batch.
    baml.score_errors[("张三", "B公司")] = RuntimeError("LLM timeout")
    body = _request(
        resumes=[("r1", "张三")],
        jobs=[("j1", "A公司", "P"), ("j2", "B公司", "P")],
    )
    resp = await client.post(
        "/v1/resume-matching/match",
        json=body, headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert {(m["resume_id"], m["job_id"]) for m in data["matches"]} == {("r1", "j1")}
    assert len(data["errors"]) == 1
    err = data["errors"][0]
    assert err["resume_id"] == "r1"
    assert err["job_id"] == "j2"
    assert "RuntimeError" in err["error"]
    assert data["stats"] == {"pairs_scored": 1, "pairs_failed": 1, "elapsed_ms": data["stats"]["elapsed_ms"]}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_match_rejects_empty_resumes(client, api_key, baml) -> None:
    resp = await client.post(
        "/v1/resume-matching/match",
        json={"resumes": [], "jobs": [{"job_id": "j", "job": {"company": "A", "position": "P"}}]},
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_sync_match_rejects_duplicate_ids(client, api_key, baml) -> None:
    # Duplicate resume_ids → 400 instead of silently dedup'ing. Silent
    # dedup would mean the client loses a result without an obvious error.
    body = _request(
        resumes=[("r1", "张三"), ("r1", "李四")],
        jobs=[("j1", "A", "P")],
    )
    resp = await client.post(
        "/v1/resume-matching/match",
        json=body, headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 400
    assert "resume_id" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_sync_match_caps_pair_count(client, api_key, baml, monkeypatch) -> None:
    # Lower the cap so the test isn't expensive — we just need to verify
    # the gate fires, not that 1000 pairs are actually rejected.
    monkeypatch.setattr(public_router_mod, "MAX_PAIRS", 4)
    body = _request(
        resumes=[("r1", "a"), ("r2", "b"), ("r3", "c")],
        jobs=[("j1", "X", "P"), ("j2", "Y", "P")],
    )
    resp = await client.post(
        "/v1/resume-matching/match",
        json=body, headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 413


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_match_rejects_missing_api_key(client, baml) -> None:
    body = _request(
        resumes=[("r1", "a")], jobs=[("j1", "X", "P")],
    )
    resp = await client.post("/v1/resume-matching/match", json=body)
    assert resp.status_code == 401
    assert "Missing" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_match_rejects_invalid_api_key(client, baml) -> None:
    body = _request(
        resumes=[("r1", "a")], jobs=[("j1", "X", "P")],
    )
    resp = await client.post(
        "/v1/resume-matching/match",
        json=body, headers={"X-API-Key": "mnk_not-a-real-key"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Usage logging
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_match_logs_usage_row_with_api_key_id(
    client, engine, api_key, baml,
) -> None:
    body = _request(
        resumes=[("r1", "张三")],
        jobs=[("j1", "A", "P"), ("j2", "B", "P")],
    )
    resp = await client.post(
        "/v1/resume-matching/match",
        json=body, headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 200

    rows = await _usage_rows(engine)
    assert len(rows) == 1
    row = rows[0]
    assert row["endpoint"] == "match"
    assert row["resume_count"] == 1
    assert row["job_count"] == 2
    assert row["pair_count"] == 2
    assert row["pairs_failed"] == 0
    assert row["status"] == "ok"
    assert row["api_key_id"] is not None
    # Stub doesn't populate the BAML Collector, so tokens default to 0 —
    # the column existence + clean default is what we verify here. The
    # next test exercises the populated path.
    assert row["input_tokens"] == 0
    assert row["output_tokens"] == 0


@pytest.mark.asyncio
async def test_sync_match_aggregates_token_usage_into_usage_row(
    client, engine, api_key, baml, monkeypatch,
) -> None:
    # Patch the helper that reads from the BAML Collector — stubbing the
    # Collector itself is awkward because BAML constructs it inside our
    # code path. A simple counter-driven stub gives each ScoreMatch call
    # distinct token counts so we can verify the router sums them all.
    counter = {"n": 0}

    def fake_tokens(_collector):
        counter["n"] += 1
        # Distinct values per call so a missing-pair bug would change the sum.
        return (100 * counter["n"], 50 * counter["n"])

    monkeypatch.setattr(pipeline_mod, "_collector_tokens", fake_tokens)

    body = _request(
        resumes=[("r1", "张三")],
        jobs=[("j1", "A", "P"), ("j2", "B", "P")],
    )
    resp = await client.post(
        "/v1/resume-matching/match",
        json=body, headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 200

    rows = await _usage_rows(engine)
    assert len(rows) == 1
    # Two ScoreMatch calls → 100+200 input, 50+100 output.
    assert rows[0]["input_tokens"] == 300
    assert rows[0]["output_tokens"] == 150


# ---------------------------------------------------------------------------
# Async + poll
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_match_returns_job_id_and_eventually_completes(
    client, api_key, baml,
) -> None:
    body = _request(
        resumes=[("r1", "张三")], jobs=[("j1", "A", "P"), ("j2", "B", "P")],
    )
    baml.score_overrides = {("张三", "A"): 90, ("张三", "B"): 70}

    resp = await client.post(
        "/v1/resume-matching/match/async",
        json=body, headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 202
    accepted = resp.json()
    assert accepted["status"] == "queued"
    job_id = accepted["job_id"]
    assert job_id.startswith("rmj_")

    # Drain the event loop until the worker finishes. Stub has no I/O so
    # the task completes within a couple of yields; bound the loop so a
    # bug doesn't hang the test.
    final = None
    for _ in range(50):
        await asyncio.sleep(0)
        poll = await client.get(
            f"/v1/resume-matching/match/{job_id}",
            headers={"X-API-Key": api_key},
        )
        assert poll.status_code == 200
        final = poll.json()
        if final["status"] in ("completed", "failed"):
            break
    assert final is not None
    assert final["status"] == "completed"
    assert {(m["resume_id"], m["job_id"]) for m in final["matches"]} == {
        ("r1", "j1"), ("r1", "j2"),
    }
    assert final["progress"]["pairs_done"] == 2
    assert final["progress"]["pairs_total"] == 2


@pytest.mark.asyncio
async def test_async_poll_returns_404_for_unknown_job(client, api_key) -> None:
    resp = await client.get(
        "/v1/resume-matching/match/rmj_does-not-exist",
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_async_poll_requires_api_key(client) -> None:
    resp = await client.get("/v1/resume-matching/match/rmj_anything")
    assert resp.status_code == 401
