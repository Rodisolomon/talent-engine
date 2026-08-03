"""Public API schema for the resume-matching endpoint.

These Pydantic models are the stable contract between the WeChat mini
program (and any other API consumer) and the resume-matching service.

We intentionally mirror the BAML internal classes in `baml_src/schemas.baml`
field-for-field today, but version them independently — future BAML
prompt-engineering changes (renaming a field for clarity, adding a new
internal-only signal) must not become breaking changes for live mini
program clients. The translators below are the only place where the
internal and public shapes touch.

Naming: snake_case throughout, Chinese-locale string values
(verdicts like "强烈推荐") preserved verbatim. The mini program is
expected to render these directly.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------


class PublicEducation(BaseModel):
    school: str
    degree: Optional[str] = None
    major: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None
    gpa_or_rank: Optional[str] = None


class PublicExperience(BaseModel):
    organization: str
    title: str
    start: Optional[str] = None
    end: Optional[str] = None
    description: str = ""


class PublicResume(BaseModel):
    """Parsed resume payload supplied by the client.

    All fields except `education` / `experience` lists default to None so
    the mini program can omit anything it couldn't extract. `raw_text` is
    optional because some clients won't have the original document text;
    when omitted the matcher loses a useful context signal but still
    works against the structured fields.
    """
    name: Optional[str] = None
    gender: Optional[str] = None
    birth_year: Optional[int] = None
    age: Optional[int] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    hometown: Optional[str] = None
    education: List[PublicEducation] = Field(default_factory=list)
    experience: List[PublicExperience] = Field(default_factory=list)
    certifications: List[str] = Field(default_factory=list)
    skills: List[str] = Field(default_factory=list)
    languages: List[str] = Field(default_factory=list)
    self_evaluation: Optional[str] = None
    raw_text: str = ""


# ---------------------------------------------------------------------------
# Job
# ---------------------------------------------------------------------------


class PublicJob(BaseModel):
    """Parsed job description payload supplied by the client.

    `company`, `position`, `location` are required because the matcher
    can't produce useful output without at least job identity + location.
    Everything else is optional — JD postings vary wildly in what they
    spell out.
    """
    company: str
    position: str
    location: str = ""
    education_min: Optional[str] = None
    age_min: Optional[int] = None
    age_max: Optional[int] = None
    majors_preferred: List[str] = Field(default_factory=list)
    experience_years_min: Optional[int] = None
    gender_preference: Optional[str] = None
    height_min_cm: Optional[int] = None
    certifications_required: List[str] = Field(default_factory=list)
    image_requirements: Optional[str] = None
    duties: List[str] = Field(default_factory=list)
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    work_schedule: Optional[str] = None
    benefits: List[str] = Field(default_factory=list)
    raw_text: str = ""


# ---------------------------------------------------------------------------
# Request / response wrappers
# ---------------------------------------------------------------------------


class ResumeItem(BaseModel):
    resume_id: str
    resume: PublicResume


class JobItem(BaseModel):
    job_id: str
    job: PublicJob


class MatchOptions(BaseModel):
    concurrency: Optional[int] = None  # server clamps; None = use server default


class MatchRequest(BaseModel):
    resumes: List[ResumeItem]
    jobs: List[JobItem]
    options: MatchOptions = Field(default_factory=MatchOptions)


# Per-dimension caps. These ARE the weighting — change them here and the
# weighting changes, without touching a prompt. Keys match BAML MatchScore.
SCORE_WEIGHTS: dict[str, int] = {
    "score_experience": 40,
    "score_skills": 20,
    "score_education": 20,
    "score_age": 10,
    "score_other": 10,
}

# Total → verdict, highest band first. Bands sit ~5pt below the pre-weighting
# thresholds because earned-point scoring can't reach the old top of range.
VERDICT_BANDS: tuple[tuple[int, str], ...] = ((85, "强烈推荐"), (70, "可推荐"), (55, "勉强"))
VERDICT_LOWEST = "不推荐"


def score_components(baml) -> dict[str, int]:
    """Per-dimension scores, clamped to their caps.

    Clamping is defensive: nothing stops the model returning 45 out of 40,
    and an out-of-range dimension would silently inflate the total.
    """
    return {
        field: max(0, min(int(getattr(baml, field, 0) or 0), cap))
        for field, cap in SCORE_WEIGHTS.items()
    }


def total_score(baml) -> int:
    """Authoritative total — summed here, never taken from the model.

    Asking an LLM to add five numbers is asking for the one thing it is
    worst at: deepseek-chat disagreed with its own stated breakdown in 7 of
    8 checkable runs, always low by 6-22 points.
    """
    return sum(score_components(baml).values())


def verdict_for(total: int, hard_fails: List[str]) -> str:
    """Verdict is a pure function of total + hard_fails, so Python owns it."""
    if hard_fails:
        return VERDICT_LOWEST
    for threshold, label in VERDICT_BANDS:
        if total >= threshold:
            return label
    return VERDICT_LOWEST


class MatchResultItem(BaseModel):
    """One (resume, job) score.

    `score` and `verdict` are derived server-side from the five dimensions;
    the model supplies only the per-dimension numbers. The dimensions are
    exposed so the client can show a breakdown and so the operator can audit
    that the weighting was actually applied.
    """
    resume_id: str
    job_id: str
    score: int
    verdict: str
    score_experience: int
    score_skills: int
    score_education: int
    score_age: int
    score_other: int
    hard_fails: List[str]
    strengths: List[str]
    gaps: List[str]
    reasoning: str


class MatchErrorItem(BaseModel):
    """Per-pair (or per-resume) failure surfaced to the client.

    Distinct from `matches` so the client can render successes immediately
    and degrade per-error rather than nuking the whole result.
    """
    resume_id: str
    job_id: Optional[str] = None      # None when the failure is resume-wide (e.g. invalid input)
    error: str


class MatchStats(BaseModel):
    pairs_scored: int
    pairs_failed: int
    elapsed_ms: int


class MatchResponse(BaseModel):
    """Response body for the sync `/match` endpoint.

    Sync always returns `status="completed"` — pipeline-level failures
    surface as HTTP 5xx, not as a "failed"-status body. Per-pair failures
    appear in `errors[]` alongside successful `matches[]`. Async polls
    use the separate `AsyncPollResponse` shape (which does support
    `status="failed"`).
    """
    status: str
    matches: List[MatchResultItem] = Field(default_factory=list)
    errors: List[MatchErrorItem] = Field(default_factory=list)
    stats: Optional[MatchStats] = None


# ---------------------------------------------------------------------------
# Async-specific responses
# ---------------------------------------------------------------------------


class AsyncJobAccepted(BaseModel):
    """POST /match/async response — server has accepted and queued the job."""
    job_id: str
    status: str = "queued"


class AsyncJobProgress(BaseModel):
    pairs_done: int
    pairs_total: int


class AsyncPollResponse(BaseModel):
    """GET /match/{job_id} response.

    Shape evolves with status:
      - queued / running: progress populated, matches empty
      - completed: matches + stats populated
      - failed: error populated
    """
    job_id: str
    status: str                                     # queued | running | completed | failed
    progress: Optional[AsyncJobProgress] = None
    matches: List[MatchResultItem] = Field(default_factory=list)
    errors: List[MatchErrorItem] = Field(default_factory=list)
    stats: Optional[MatchStats] = None
    error: Optional[str] = None                     # populated when status == "failed"


# ---------------------------------------------------------------------------
# Translators — public ↔ BAML
# ---------------------------------------------------------------------------
#
# These are the only places that import from `baml_client.types`. Keeping
# the conversion centralized means a BAML schema change only ripples
# through this file, not the router or the public schema.


def to_baml_resume(public: PublicResume):
    """Build a BAML Resume from a PublicResume.

    Imported lazily so test harnesses that monkey-patch the BAML client can
    do so before any module-level resolution.
    """
    from v1.resume_matching.baml_client.types import (
        Education as BamlEducation,
        Experience as BamlExperience,
        Resume as BamlResume,
    )
    return BamlResume(
        name=public.name,
        gender=public.gender,
        birth_year=public.birth_year,
        age=public.age,
        phone=public.phone,
        email=public.email,
        hometown=public.hometown,
        education=[
            BamlEducation(
                school=e.school, degree=e.degree, major=e.major,
                start=e.start, end=e.end, gpa_or_rank=e.gpa_or_rank,
            )
            for e in public.education
        ],
        experience=[
            BamlExperience(
                organization=e.organization, title=e.title,
                start=e.start, end=e.end, description=e.description,
            )
            for e in public.experience
        ],
        certifications=list(public.certifications),
        skills=list(public.skills),
        languages=list(public.languages),
        self_evaluation=public.self_evaluation,
        raw_text=public.raw_text,
    )


def to_baml_job(public: PublicJob):
    from v1.resume_matching.baml_client.types import Job as BamlJob
    return BamlJob(
        company=public.company,
        position=public.position,
        education_min=public.education_min,
        age_min=public.age_min,
        age_max=public.age_max,
        majors_preferred=list(public.majors_preferred),
        experience_years_min=public.experience_years_min,
        gender_preference=public.gender_preference,
        height_min_cm=public.height_min_cm,
        certifications_required=list(public.certifications_required),
        image_requirements=public.image_requirements,
        duties=list(public.duties),
        salary_min=public.salary_min,
        salary_max=public.salary_max,
        work_schedule=public.work_schedule,
        location=public.location,
        benefits=list(public.benefits),
        raw_text=public.raw_text,
    )


def from_baml_score(*, resume_id: str, job_id: str, score) -> MatchResultItem:
    hard_fails = list(score.hard_fails)
    components = score_components(score)
    total = sum(components.values())
    return MatchResultItem(
        resume_id=resume_id,
        job_id=job_id,
        score=total,
        verdict=verdict_for(total, hard_fails),
        **components,
        hard_fails=hard_fails,
        strengths=list(score.strengths),
        gaps=list(score.gaps),
        reasoning=score.reasoning,
    )


# Reverse translators — BAML → public. Used by tests and operator scripts
# that parse with the internal BAML pipeline and then exercise the public
# API path with the resulting structured data.


def from_baml_resume(baml) -> PublicResume:
    return PublicResume(
        name=baml.name,
        gender=baml.gender,
        birth_year=baml.birth_year,
        age=baml.age,
        phone=baml.phone,
        email=baml.email,
        hometown=baml.hometown,
        education=[
            PublicEducation(
                school=e.school, degree=e.degree, major=e.major,
                start=e.start, end=e.end, gpa_or_rank=e.gpa_or_rank,
            )
            for e in baml.education
        ],
        experience=[
            PublicExperience(
                organization=e.organization, title=e.title,
                start=e.start, end=e.end, description=e.description,
            )
            for e in baml.experience
        ],
        certifications=list(baml.certifications),
        skills=list(baml.skills),
        languages=list(baml.languages),
        self_evaluation=baml.self_evaluation,
        raw_text=baml.raw_text,
    )


def from_baml_job(baml) -> PublicJob:
    return PublicJob(
        company=baml.company,
        position=baml.position,
        location=baml.location,
        education_min=baml.education_min,
        age_min=baml.age_min,
        age_max=baml.age_max,
        majors_preferred=list(baml.majors_preferred),
        experience_years_min=baml.experience_years_min,
        gender_preference=baml.gender_preference,
        height_min_cm=baml.height_min_cm,
        certifications_required=list(baml.certifications_required),
        image_requirements=baml.image_requirements,
        duties=list(baml.duties),
        salary_min=baml.salary_min,
        salary_max=baml.salary_max,
        work_schedule=baml.work_schedule,
        benefits=list(baml.benefits),
        raw_text=baml.raw_text,
    )
