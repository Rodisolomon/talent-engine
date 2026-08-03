"""Tests for the public-API Pydantic schema and BAML translators.

These guard the shape contract with the WeChat mini program: any rename or
removal here is a breaking change for live clients. Translation tests pin
the public ↔ BAML mapping so a future BAML schema tweak that drops a
field without updating the translator fails loudly.
"""

from __future__ import annotations

import pytest

from v1.resume_matching.baml_client.types import (
    Education as BamlEducation,
    Experience as BamlExperience,
    Job as BamlJob,
    MatchScore as BamlMatchScore,
    Resume as BamlResume,
)
from v1.resume_matching.public_schema import (
    PublicEducation,
    apply_deterministic_rules,
    PublicExperience,
    PublicJob,
    PublicResume,
    from_baml_score,
    to_baml_job,
    to_baml_resume,
)


# ---------------------------------------------------------------------------
# Optional fields default sensibly
# ---------------------------------------------------------------------------


def test_public_resume_minimal_payload_validates() -> None:
    # Mini program may have extracted only a name. Everything else
    # must be optional so the request validates.
    r = PublicResume(name="张三")
    assert r.education == []
    assert r.experience == []
    assert r.skills == []
    assert r.raw_text == ""


def test_public_job_requires_company_and_position() -> None:
    # Without company/position there's nothing to match against — surface
    # this as a Pydantic ValidationError, not a confusing pipeline 500.
    with pytest.raises(Exception):
        PublicJob(position="护士")  # type: ignore[call-arg]
    with pytest.raises(Exception):
        PublicJob(company="A公司")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Translation: public → BAML
# ---------------------------------------------------------------------------


def test_to_baml_resume_round_trips_all_fields() -> None:
    public = PublicResume(
        name="张三", gender="男", birth_year=2000, age=24,
        phone="13800000000", email="zhang@x.com", hometown="北京",
        education=[PublicEducation(
            school="北大", degree="本科", major="CS",
            start="2018.9", end="2022.6", gpa_or_rank="前10%",
        )],
        experience=[PublicExperience(
            organization="阿里", title="实习生",
            start="2021.6", end="2021.9", description="搬砖",
        )],
        certifications=["计算机二级"], skills=["Python"], languages=["英语"],
        self_evaluation="勤奋", raw_text="原始文本",
    )
    baml = to_baml_resume(public)
    assert isinstance(baml, BamlResume)
    assert baml.name == "张三"
    assert baml.gender == "男"
    assert baml.raw_text == "原始文本"
    assert isinstance(baml.education[0], BamlEducation)
    assert baml.education[0].school == "北大"
    assert baml.education[0].gpa_or_rank == "前10%"
    assert isinstance(baml.experience[0], BamlExperience)
    assert baml.experience[0].organization == "阿里"
    assert baml.skills == ["Python"]


def test_to_baml_job_translates_chinese_locale_strings() -> None:
    public = PublicJob(
        company="A公司", position="护士",
        location="济南", education_min="专科",
        majors_preferred=["护理学"], certifications_required=["护士证"],
        gender_preference="不限",
        salary_min=5000, salary_max=8000,
        benefits=["五险一金"],
        raw_text="招聘单位：A公司...",
    )
    baml = to_baml_job(public)
    assert isinstance(baml, BamlJob)
    assert baml.company == "A公司"
    assert baml.position == "护士"
    assert baml.location == "济南"
    assert baml.majors_preferred == ["护理学"]
    assert baml.certifications_required == ["护士证"]
    assert baml.benefits == ["五险一金"]
    assert baml.raw_text.startswith("招聘单位")


def test_to_baml_resume_handles_empty_optional_lists() -> None:
    # The mini program will frequently send a resume with no certifications,
    # no languages, etc. The BAML class wants `list[str]`, not `None`.
    baml = to_baml_resume(PublicResume(name="王五"))
    assert baml.certifications == []
    assert baml.skills == []
    assert baml.languages == []
    assert baml.education == []
    assert baml.experience == []


# ---------------------------------------------------------------------------
# Translation: BAML score → public match item
# ---------------------------------------------------------------------------


def test_from_baml_score_carries_full_reasoning() -> None:
    score = BamlMatchScore(
        score_experience=32, score_skills=15, score_education=20,
        score_age=10, score_other=10,
        hard_fails=[],
        strengths=["学历匹配", "专业相关"],
        gaps=["缺少行业经验"],
        reasoning="整体不错，建议安排面试。",
    )
    item = from_baml_score(resume_id="r1", job_id="j1", score=score)
    assert item.resume_id == "r1"
    assert item.job_id == "j1"
    # 87 = 32+15+20+10+10, summed here rather than taken from the model,
    # and 87 >= 85 so the verdict is derived too — the model supplies neither.
    assert item.score == 87
    assert item.verdict == "强烈推荐"
    assert item.score_experience == 32
    assert item.strengths == ["学历匹配", "专业相关"]
    assert item.gaps == ["缺少行业经验"]
    assert item.reasoning.startswith("整体")


# ---------------------------------------------------------------------------
# Deterministic overrides — rules Python enforces instead of trusting the model
# ---------------------------------------------------------------------------


def _score(education_points: int) -> BamlMatchScore:
    return BamlMatchScore(
        score_experience=0, score_skills=0, score_education=education_points,
        score_age=0, score_other=0,
        hard_fails=[], strengths=[], gaps=[], reasoning="",
    )


def _job(education_min, age_min=None, age_max=None):
    return BamlJob(
        company="c", position="p", education_min=education_min, age_min=age_min,
        age_max=age_max, majors_preferred=[], experience_years_min=None,
        gender_preference=None, height_min_cm=None, certifications_required=[],
        image_requirements=None, duties=[], salary_min=None, salary_max=None,
        work_schedule=None, location="", benefits=[], raw_text="",
    )


def _resume(education, birth_year=None, age=None):
    return BamlResume(
        name=None, gender=None, birth_year=birth_year, age=age, phone=None,
        email=None, hometown=None, education=education, experience=[],
        certifications=[], skills=[], languages=[], self_evaluation=None,
        raw_text="",
    )


def _edu(degree):
    return BamlEducation(school="s", degree=degree, major=None, start=None,
                         end=None, gpa_or_rank=None)


def test_education_unstated_against_a_requirement_scores_zero() -> None:
    """The model awards full marks here in most runs; Python overrides it."""
    score = _score(20)
    apply_deterministic_rules(score, _resume([]), _job("中专"), "2026-08-02")
    assert score.score_education == 0


def test_stated_degree_is_left_alone() -> None:
    score = _score(20)
    apply_deterministic_rules(score, _resume([_edu("专科")]), _job("中专"), "2026-08-02")
    assert score.score_education == 20


def test_age_is_computed_not_trusted() -> None:
    """A 40-year-old against a 45 cap; the model scored this 5/10 in ~30% of runs."""
    score = _score(0)
    score.score_age = 5
    apply_deterministic_rules(
        score, _resume([], birth_year=1985), _job(None, age_max=45), "2026-08-02"
    )
    assert score.score_age == 10


def test_age_just_over_the_cap_keeps_the_partial_band() -> None:
    score = _score(0)
    apply_deterministic_rules(
        score, _resume([], birth_year=1979), _job(None, age_max=45), "2026-08-02"
    )
    assert score.score_age == 5   # 47 is 2 over
