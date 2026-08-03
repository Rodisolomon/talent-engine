"""Run the full resume-matching pipeline against the desktop sample set.

Usage (from repo root):
    cd api && .venv/bin/python -m v1.resume_matching.scripts.demo

Reads resume PDFs + JD txt files from Desktop/岗位说明以及简历/, runs the
pipeline, and prints a markdown report. Writes the report to
v1/resume_matching/scripts/out/report.md.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

# Allow `python -m v1.resume_matching.scripts.demo` from api/ as cwd.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from v1.resume_matching.pipeline import JobInput, MatchReport, ResumeInput, match_all
from v1.resume_matching.public_schema import total_score, verdict_for

DATA_DIR = Path.home() / "Desktop" / "岗位说明以及简历"
OUT_DIR = Path(__file__).resolve().parent / "out"


def _load_inputs() -> tuple[list[ResumeInput], list[JobInput]]:
    resume_dir = DATA_DIR / "简历"
    if not resume_dir.exists():
        raise SystemExit(f"Resume dir not found: {resume_dir}")

    resumes: list[ResumeInput] = []
    for p in sorted(resume_dir.glob("*.pdf")):
        resumes.append(ResumeInput(filename=p.name, pdf_bytes=p.read_bytes()))

    jobs: list[JobInput] = []
    for p in sorted(DATA_DIR.glob("岗位说明-*.txt")):
        jobs.append(JobInput(filename=p.name, text=p.read_text(encoding="utf-8")))

    return resumes, jobs


def _render_markdown(report: MatchReport) -> str:
    lines: list[str] = []
    lines.append("# 简历匹配报告")
    lines.append("")
    lines.append(f"- 简历总数: {len(report.resumes)}")
    lines.append(f"- 岗位总数（解析后）: {len(report.jobs)}")
    lines.append("")

    lines.append("## 岗位库")
    for i, jp in enumerate(report.jobs):
        j = jp.job
        lines.append(f"**[{i}] {j.company} — {j.position}** （源: {jp.source_filename}）")
        parts = []
        if j.education_min: parts.append(f"学历≥{j.education_min}")
        if j.age_min or j.age_max: parts.append(f"年龄 {j.age_min or ''}-{j.age_max or ''}")
        if j.majors_preferred: parts.append(f"专业: {', '.join(j.majors_preferred)}")
        if j.certifications_required: parts.append(f"必需证书: {', '.join(j.certifications_required)}")
        if j.location: parts.append(f"地点: {j.location}")
        if j.salary_min or j.salary_max:
            parts.append(f"薪资: {j.salary_min or '?'}-{j.salary_max or '?'}")
        lines.append(f"  - {' / '.join(parts)}")
    lines.append("")

    lines.append("## 按简历看 Top-3 推荐")
    for rr in report.resumes:
        if rr.parse_error:
            lines.append(f"### ❌ {rr.filename}")
            lines.append(f"解析失败: {rr.parse_error}")
            lines.append("")
            continue
        name = rr.resume.name if rr.resume else "?"
        lines.append(f"### {rr.filename}  — {name}")
        r = rr.resume
        if r:
            edu = r.education[0] if r.education else None
            if edu:
                lines.append(f"- 教育: {edu.school} / {edu.degree or '?'} / {edu.major or '?'}")
            if r.certifications:
                lines.append(f"- 证书: {', '.join(r.certifications[:5])}")
        lines.append("")
        if not rr.top_matches:
            lines.append("_无匹配结果_")
            lines.append("")
            continue
        for rank, m in enumerate(rr.top_matches, 1):
            jp = report.jobs[m.job_index]
            j = jp.job
            s = m.score
            total = total_score(s)
            lines.append(
                f"**#{rank}  {j.company} — {j.position}  "
                f"(score: {total}, {verdict_for(total, list(s.hard_fails))})**"
            )
            lines.append(f"- 推理: {s.reasoning}")
            if s.hard_fails:
                lines.append(f"- ⚠️ 硬不合: {'; '.join(s.hard_fails)}")
            if s.strengths:
                lines.append(f"- ✅ 优势: {'; '.join(s.strengths)}")
            if s.gaps:
                lines.append(f"- 📋 差距（可作为简历指导点）: {'; '.join(s.gaps)}")
            lines.append("")
    return "\n".join(lines)


async def main() -> None:
    resumes, jobs = _load_inputs()
    print(f"Loaded {len(resumes)} resumes, {len(jobs)} JD files")
    print(f"Running matcher (this will make roughly {len(resumes)} parse + {len(jobs)} parse + N×M score LLM calls)...")

    t0 = time.perf_counter()
    report = await match_all(resumes=resumes, jobs=jobs)
    elapsed = time.perf_counter() - t0
    print(f"Done in {elapsed:.1f}s")

    md = _render_markdown(report)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / "report.md"
    out_path.write_text(md, encoding="utf-8")
    print(f"Report: {out_path}")

    # Also dump raw matrix for debugging
    import json
    raw = {
        "jobs": [
            {
                "source": jp.source_filename,
                "company": jp.job.company,
                "position": jp.job.position,
            }
            for jp in report.jobs
        ],
        "resumes": [
            {
                "filename": rr.filename,
                "parse_error": rr.parse_error,
                "name": rr.resume.name if rr.resume else None,
                "top_matches": [
                    {
                        "job_index": m.job_index,
                        "score": total_score(m.score),
                        "verdict": verdict_for(
                            total_score(m.score), list(m.score.hard_fails)
                        ),
                        "hard_fails": m.score.hard_fails,
                        "gaps": m.score.gaps,
                    }
                    for m in rr.top_matches
                ],
            }
            for rr in report.resumes
        ],
    }
    (OUT_DIR / "report.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Raw: {OUT_DIR / 'report.json'}")


if __name__ == "__main__":
    asyncio.run(main())
