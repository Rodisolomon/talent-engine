"""Tests for the match_all pipeline — top-K, parallelism, progress, errors.

We stub the BAML async client (`v1.resume_matching.pipeline.b`) so these
tests are hermetic (no network, no real LLM) and fast. Each stubbed method
sleeps briefly to mimic latency — this lets us verify the pipeline really
does overlap resume parse with JD parse and per-resume scoring, rather than
running everything serially.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Dict, List

import pytest

from v1.resume_matching import pipeline as pipeline_mod
from v1.resume_matching.public_schema import SCORE_WEIGHTS, total_score
from v1.resume_matching.baml_client.types import (
    Education,
    Experience,
    Job,
    MatchScore,
    Resume,
)
from v1.resume_matching.pipeline import (
    JobInput,
    ResumeInput,
    match_all,
)


# ---------------------------------------------------------------------------
# BAML stub — a drop-in for `pipeline.b` (the async client singleton).
# ---------------------------------------------------------------------------


def _fake_resume(name: str) -> Resume:
    return Resume(
        name=name, gender=None, birth_year=None, age=None,
        phone=None, email=None, hometown=None,
        education=[Education(
            school="S", degree="本科", major="CS",
            start=None, end=None, gpa_or_rank=None,
        )],
        experience=[Experience(organization="X", title="Y", start=None, end=None, description="")],
        certifications=[], skills=[], languages=[],
        self_evaluation=None, raw_text=f"{name} raw",
    )


def _fake_job(company: str, position: str, raw: str = "招聘单位：...") -> Job:
    return Job(
        company=company, position=position,
        education_min=None, age_min=None, age_max=None,
        majors_preferred=[], experience_years_min=None,
        gender_preference=None, height_min_cm=None,
        certifications_required=[], image_requirements=None,
        duties=[], salary_min=None, salary_max=None,
        work_schedule=None, location="", benefits=[],
        raw_text=raw,
    )


def _split(total: int) -> dict[str, int]:
    """Spread a desired total across the five dimensions, respecting caps."""
    out, left = {}, total
    for field, cap in SCORE_WEIGHTS.items():
        out[field] = max(0, min(left, cap))
        left -= out[field]
    return out


def _fake_score(score: int) -> MatchScore:
    return MatchScore(
        **_split(score),
        hard_fails=[], strengths=[f"match at {score}"],
        gaps=[], reasoning="",
    )


@dataclass
class BamlStub:
    """Minimal BAML client stub.

    Each method sleeps briefly to give the event loop something to overlap —
    that's how we can observe parallelism from the outside. Custom behaviour
    (raise for a specific resume, return specific scores) is driven by
    instance-level dicts the test sets up.
    """
    resume_latency: float = 0.02
    jd_latency: float = 0.02
    score_latency: float = 0.01
    # filename -> Exception, to simulate parse failures on specific resumes
    resume_errors: Dict[str, Exception] = None  # type: ignore[assignment]
    # company/position -> score, to pin per-job score values
    score_overrides: Dict[tuple, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.resume_errors is None:
            self.resume_errors = {}
        if self.score_overrides is None:
            self.score_overrides = {}
        self.calls: Dict[str, int] = {"ParseResume": 0, "ParseSingleJob": 0, "ScoreMatch": 0, "ParseJobDescriptions": 0}
        # Record per-call start timestamps for parallelism assertions.
        self.starts: Dict[str, List[float]] = {
            "ParseResume": [], "ParseSingleJob": [], "ScoreMatch": [],
        }

    async def ParseResume(self, *, text: str = "", resume_pdf=None, baml_options=None) -> Resume:  # noqa: N802 — BAML naming
        self.calls["ParseResume"] += 1
        self.starts["ParseResume"].append(time.perf_counter())
        # Tagging works whether the caller passes `text` (production path) or
        # `resume_pdf` (test fixture's tagged_parse — uses an object that
        # carries `_test_filename`).
        fname = (
            getattr(resume_pdf, "_test_filename", None)
            if resume_pdf is not None
            else (text or "unknown")
        )
        if fname in self.resume_errors:
            await asyncio.sleep(self.resume_latency)
            raise self.resume_errors[fname]
        await asyncio.sleep(self.resume_latency)
        return _fake_resume(fname)

    async def ParseSingleJob(self, *, text: str, baml_options=None) -> Job:  # noqa: N802
        self.calls["ParseSingleJob"] += 1
        self.starts["ParseSingleJob"].append(time.perf_counter())
        await asyncio.sleep(self.jd_latency)
        # Parse company/position off the first two lines of the chunk.
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        company = lines[0].split("：", 1)[-1] if lines else "?"
        position = lines[1].split("：", 1)[-1] if len(lines) > 1 else "?"
        return _fake_job(company=company, position=position, raw=text)

    async def ParseJobDescriptions(self, *, text: str, baml_options=None) -> List[Job]:  # noqa: N802
        # Fallback path — only exercised if the regex splitter returns nothing.
        self.calls["ParseJobDescriptions"] += 1
        await asyncio.sleep(self.jd_latency)
        return []

    async def ScoreMatch(  # noqa: N802
        self, *, resume: Resume, job: Job, today: str = "", baml_options=None,
    ) -> MatchScore:
        self.calls["ScoreMatch"] += 1
        self.starts["ScoreMatch"].append(time.perf_counter())
        await asyncio.sleep(self.score_latency)
        key = (resume.name, job.company, job.position)
        if key in self.score_overrides:
            return _fake_score(self.score_overrides[key])
        return _fake_score(50)


@pytest.fixture
def baml(monkeypatch) -> BamlStub:
    stub = BamlStub()
    monkeypatch.setattr(pipeline_mod, "b", stub)
    # Also stub out Pdf.from_base64 so _parse_resume can tag the filename
    # onto the returned object and the ParseResume stub can read it back.
    class FakePdf:
        def __init__(self, filename: str) -> None:
            self._test_filename = filename

    def fake_from_base64(data: str) -> FakePdf:  # noqa: ARG001
        return FakePdf("__will_be_overwritten__")

    # Wrap _parse_resume to tag each Pdf with its filename before calling BAML.
    orig_parse = pipeline_mod._parse_resume

    async def tagged_parse(item):
        # Instead of patching baml_py.Pdf globally, sub in our own tagging
        # path: build a stand-in object that the BamlStub.ParseResume reads
        # to look up whether this filename should error.
        class Tagged:
            _test_filename = item.filename
        try:
            resume = await stub.ParseResume(resume_pdf=Tagged())
            return item.filename, resume, None
        except Exception as e:
            return item.filename, None, f"{type(e).__name__}: {e}"

    monkeypatch.setattr(pipeline_mod, "_parse_resume", tagged_parse)
    return stub


# ---------------------------------------------------------------------------
# Happy path + top-K ordering
# ---------------------------------------------------------------------------


def _resume(name: str) -> ResumeInput:
    return ResumeInput(filename=name, pdf_bytes=b"x")


def _jd(filename: str, company: str, position: str, count: int = 1) -> JobInput:
    chunks = [f"招聘单位：{company}-{i}\n招聘岗位：{position}-{i}" for i in range(count)]
    return JobInput(filename=filename, text="\n\n".join(chunks))


@pytest.mark.asyncio
async def test_returns_top_k_ranked_by_score(baml) -> None:
    # Set up specific scores so we can predict the ranking.
    baml.score_overrides = {
        ("zhang", "A-0", "P-0"): 95,
        ("zhang", "B-0", "P-0"): 60,
        ("zhang", "C-0", "P-0"): 80,
        ("zhang", "D-0", "P-0"): 70,
    }
    report = await match_all(
        resumes=[_resume("zhang")],
        jobs=[
            _jd("jd.txt", "A", "P"),
            _jd("jd.txt", "B", "P"),
            _jd("jd.txt", "C", "P"),
            _jd("jd.txt", "D", "P"),
        ],
        top_k=3,
    )
    assert len(report.resumes) == 1
    top = report.resumes[0].top_matches
    assert [total_score(m.score) for m in top] == [95, 80, 70]
    # Same order implied by the job_index pointing back into jobs[].
    companies = [report.jobs[m.job_index].job.company for m in top]
    assert companies == ["A-0", "C-0", "D-0"]


@pytest.mark.asyncio
async def test_parse_error_surfaces_without_blocking_other_resumes(baml) -> None:
    baml.resume_errors = {"broken.pdf": RuntimeError("corrupt")}
    report = await match_all(
        resumes=[_resume("broken.pdf"), _resume("zhang")],
        jobs=[_jd("jd.txt", "A", "P")],
        top_k=3,
    )
    by_file = {r.filename: r for r in report.resumes}
    assert by_file["broken.pdf"].resume is None
    assert "RuntimeError" in (by_file["broken.pdf"].parse_error or "")
    assert by_file["broken.pdf"].top_matches == []
    # The healthy resume must still be scored.
    assert by_file["zhang"].resume is not None
    assert len(by_file["zhang"].top_matches) == 1


@pytest.mark.asyncio
async def test_empty_inputs_produce_empty_report(baml) -> None:
    report = await match_all(resumes=[], jobs=[])
    assert report.resumes == []
    assert report.jobs == []


@pytest.mark.asyncio
async def test_no_jobs_means_no_matches_but_resumes_still_parsed(baml) -> None:
    # When split + fallback both produce zero jobs, resumes parse normally
    # but top_matches is empty rather than erroring.
    report = await match_all(
        resumes=[_resume("zhang")],
        jobs=[JobInput(filename="empty.txt", text="no jobs here")],
    )
    assert report.jobs == []
    assert len(report.resumes) == 1
    assert report.resumes[0].resume is not None
    assert report.resumes[0].top_matches == []


# ---------------------------------------------------------------------------
# Progress callback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_progress_callback_fires_expected_event_types(baml) -> None:
    events: List[Dict[str, Any]] = []

    async def record(ev: Dict[str, Any]) -> None:
        events.append(ev)

    await match_all(
        resumes=[_resume("r1"), _resume("r2")],
        jobs=[_jd("jd.txt", "A", "P")],
        on_progress=record,
    )

    types_in_order = [e["type"] for e in events]
    # Expected spine: start → jd_parsed → jds_ready → 2× (resume_parsed + resume_scored)
    assert types_in_order[0] == "start"
    assert "jd_parsed" in types_in_order
    # jds_ready must come after all jd_parsed events.
    jds_ready_idx = types_in_order.index("jds_ready")
    jd_parsed_indices = [i for i, t in enumerate(types_in_order) if t == "jd_parsed"]
    assert all(i < jds_ready_idx for i in jd_parsed_indices)
    # Each resume produces exactly one parsed + one scored event.
    assert types_in_order.count("resume_parsed") == 2
    assert types_in_order.count("resume_scored") == 2

    # And the totals on the start event should match inputs.
    assert events[0]["resumes_total"] == 2
    assert events[0]["jd_files_total"] == 1


@pytest.mark.asyncio
async def test_progress_callback_errors_do_not_break_pipeline(baml) -> None:
    async def bad(ev: Dict[str, Any]) -> None:
        raise RuntimeError("callback exploded")

    # Pipeline swallows callback errors — the run must still complete.
    report = await match_all(
        resumes=[_resume("r1")],
        jobs=[_jd("jd.txt", "A", "P")],
        on_progress=bad,
    )
    assert len(report.resumes) == 1
    assert report.resumes[0].resume is not None


# ---------------------------------------------------------------------------
# Parallelism — we can't time-measure absolute latency reliably in CI, but
# we can check that JDs and resumes start within overlapping windows.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_and_jd_parses_start_concurrently(baml) -> None:
    # Make resume parses slow so the test window is measurable.
    baml.resume_latency = 0.1
    baml.jd_latency = 0.01
    await match_all(
        resumes=[_resume("r1"), _resume("r2"), _resume("r3")],
        jobs=[_jd("jd.txt", "A", "P")],
    )
    # Each ParseResume start and the (single) ParseSingleJob start should
    # all land within a small window — they were scheduled via
    # asyncio.create_task before any blocking await in match_all. If
    # resume parsing waited for JDs (old sequential path), resumes would
    # start >0.01s after the first JD call, not right alongside it.
    all_starts = sorted(baml.starts["ParseResume"] + baml.starts["ParseSingleJob"])
    spread = all_starts[-1] - all_starts[0]
    assert spread < 0.05, f"expected concurrent scheduling, spread={spread:.3f}s"


@pytest.mark.asyncio
async def test_scoring_fans_out_per_resume(baml) -> None:
    # With 2 resumes × 3 jobs we expect exactly 6 ScoreMatch calls.
    await match_all(
        resumes=[_resume("r1"), _resume("r2")],
        jobs=[_jd("jd.txt", "A", "P", count=3)],
    )
    assert baml.calls["ScoreMatch"] == 6
    assert baml.calls["ParseSingleJob"] == 3
    assert baml.calls["ParseResume"] == 2
