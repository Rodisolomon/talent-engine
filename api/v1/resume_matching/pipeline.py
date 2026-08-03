"""Resume matching orchestration.

One entry point, `match_all`, that:
  1. Parses N resume PDFs + M job description text blobs in parallel.
  2. Scores every (resume × job) pair in parallel, respecting a concurrency cap.
  3. Returns a structured report with top-K jobs per resume.

The module has no I/O beyond BAML calls — the router and demo script are
responsible for reading files and writing output.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from baml_py import Collector, Pdf

from v1.resume_matching.baml_client.async_client import b
from v1.resume_matching.baml_client.types import Job, MatchScore, Resume
from v1.resume_matching.llm_call import with_timeout_retry
from v1.resume_matching.llm_config import resolve_llm_provider
from v1.resume_matching.public_schema import total_score, verdict_for

# Progress event callback signature. The router wires this to an SSE queue so
# the frontend can render live counts. Keep callback non-awaiting-critical:
# pipeline does not wait for delivery beyond the coroutine's own completion.
ProgressFn = Callable[[Dict[str, Any]], Awaitable[None]]

logger = logging.getLogger(__name__)

# Default concurrency cap for LLM scoring calls. Gemini 2.5 Flash on Vertex
# tolerates well above 10 for this workload; 25 is a comfortable headroom
# below the per-minute quota and gives ~2.5× throughput over the old default.
# Parses (resume + JD) are not capped by this — they're bounded naturally by
# input count.
DEFAULT_CONCURRENCY = 25
DEFAULT_TOP_K = 3


@dataclass
class ResumeInput:
    filename: str
    pdf_bytes: bytes


@dataclass
class JobInput:
    filename: str
    text: str


@dataclass
class Match:
    job_index: int          # index into report.jobs
    score: MatchScore


@dataclass
class ResumeReport:
    filename: str
    resume: Optional[Resume]
    parse_error: Optional[str] = None
    top_matches: List[Match] = field(default_factory=list)


@dataclass
class JobParse:
    source_filename: str    # which JD file this job came from
    job: Job


@dataclass
class MatchReport:
    resumes: List[ResumeReport]
    jobs: List[JobParse]    # flattened: one entry per parsed job across all JD files


# ---------------------------------------------------------------------------


async def _parse_resume(item: ResumeInput) -> tuple[str, Optional[Resume], Optional[str]]:
    """Extract PDF text + parse via BAML.

    Routes through the runtime-selected provider (`LLM_PROVIDER` env var)
    so the same code works for Gemini, Qwen, Hunyuan, DeepSeek. PDF text
    extraction happens in Python via pypdf — see
    `v1.resume.pdf_text.extract_pdf_text` for the rationale.
    """
    from v1.resume.pdf_text import PdfExtractionError, extract_pdf_text

    try:
        # See parse_router.py for the rationale — pypdf blocks the loop on
        # multi-MB PDFs. Off to a worker thread.
        text = await asyncio.to_thread(extract_pdf_text, item.pdf_bytes)
    except PdfExtractionError as e:
        return item.filename, None, f"PdfExtractionError: {e}"
    provider = resolve_llm_provider()
    try:
        resume = await with_timeout_retry(
            lambda: b.ParseResume(text=text, baml_options={"client": provider}),
            label=f"ParseResume[{item.filename}]",
        )
        return item.filename, resume, None
    except Exception as e:
        logger.exception("ParseResume failed for %s", item.filename)
        return item.filename, None, f"{type(e).__name__}: {e}"


# Match a line that starts with the 招聘单位 keyword, tolerating whitespace and
# optional 【】 wrapping before it. We only split on line-start occurrences so
# an incidental mention of the phrase inside a job body doesn't split it.
_JOB_HEADER_RE = re.compile(r"(?m)^\s*【?\s*招聘单位")


async def _parse_jobs(item: JobInput) -> tuple[str, List[Job], Optional[str]]:
    """Split on 招聘单位 headers, then parse each chunk alone in parallel.

    Every real JD the client ships today uses the 招聘单位 header exactly
    once per job, so a regex split is deterministic, instant, and avoids
    the list-extraction failure modes (dropped / merged / hallucinated
    fields) that would appear with a single-call LLM parse on a bundled
    file. Each single-job chunk then goes through ParseSingleJob, which
    sees exactly one posting and has nothing to confuse.

    If the regex finds zero 招聘单位 headers — e.g. a JD that uses
    different terminology — we fall back to the single-call ParseJobDescriptions
    so the pipeline still produces something instead of silently dropping
    the whole file.
    """
    text = item.text
    provider = resolve_llm_provider()
    try:
        chunks = _split_jd_text(text)
        if not chunks:
            # No 招聘单位 header detected. Either the file is empty or it
            # uses an unexpected header style — let the LLM try to parse
            # the whole thing as a list.
            logger.warning(
                "No 招聘单位 headers in %s — falling back to list parse",
                item.filename,
            )
            jobs = await with_timeout_retry(
                lambda: b.ParseJobDescriptions(text=text, baml_options={"client": provider}),
                label=f"ParseJobDescriptions[{item.filename}]",
            )
            return item.filename, jobs, None

        parsed = await asyncio.gather(
            *[
                with_timeout_retry(
                    lambda c=c: b.ParseSingleJob(text=c, baml_options={"client": provider}),
                    label=f"ParseSingleJob[{item.filename}]",
                )
                for c in chunks
            ],
            return_exceptions=True,
        )
        jobs: List[Job] = []
        for result in parsed:
            if isinstance(result, BaseException):
                logger.warning("ParseSingleJob failed for %s chunk: %s", item.filename, result)
                continue
            jobs.append(result)  # type: ignore[arg-type]
        return item.filename, jobs, None
    except Exception as e:
        logger.exception("Job parsing failed for %s", item.filename)
        return item.filename, [], f"{type(e).__name__}: {e}"


def _split_jd_text(text: str) -> List[str]:
    """Slice `text` into one chunk per 招聘单位 header.

    Anything before the first header (preamble, file-level title) is
    discarded. Empty / whitespace-only chunks are dropped so trailing
    newlines at the end of the file don't produce a ghost job.
    """
    matches = list(_JOB_HEADER_RE.finditer(text))
    if not matches:
        return []
    starts = [m.start() for m in matches]
    ends = starts[1:] + [len(text)]
    chunks = [text[s:e].strip() for s, e in zip(starts, ends)]
    return [c for c in chunks if c]


def _today() -> str:
    """Reference date handed to ScoreMatch.

    Without it the model derives age from `birth_year` against whatever year
    it assumes the present to be — observed computing "约39岁" for a 1985
    birth year, i.e. anchored to 2024. Harmless far from an age cap, wrong
    for anyone near one.
    """
    return datetime.now(timezone.utc).date().isoformat()


async def _score(
    resume: Resume,
    job: Job,
    sem: asyncio.Semaphore,
    provider: str,
    today: str,
) -> MatchScore:
    async with sem:
        return await with_timeout_retry(
            lambda: b.ScoreMatch(
                resume=resume, job=job, today=today,
                baml_options={"client": provider},
            ),
            label="ScoreMatch",
        )


# ---------------------------------------------------------------------------
# Pre-parsed scoring — entry point for the public JSON API.
#
# Callers that already have parsed Resume / Job objects (e.g. the WeChat
# mini program does its own PDF parsing client-side) skip the parse stages
# of `match_all` entirely and call `score_pairs` directly.
# ---------------------------------------------------------------------------


@dataclass
class PairScore:
    resume_id: str
    job_id: str
    score: Optional[MatchScore]
    error: Optional[str]                # type+message string when scoring failed
    # Per-call BAML token usage. Aggregated by callers for usage logging /
    # cost attribution. Stay 0 if BAML didn't report usage (e.g. test stubs
    # that don't populate the Collector, or a usage-fetch exception).
    input_tokens: int = 0
    output_tokens: int = 0


PairProgressFn = Callable[[int, int], Awaitable[None]]
"""Callback signature: (pairs_done, pairs_total) — both monotonically increasing."""


async def score_pairs(
    *,
    pairs: List[tuple[str, str, Resume, Job]],
    concurrency: int = DEFAULT_CONCURRENCY,
    on_progress: Optional[PairProgressFn] = None,
) -> List[PairScore]:
    """Score every (resume, job) pair concurrently, capped by `concurrency`.

    Each `pairs` entry is `(resume_id, job_id, Resume, Job)`. The returned
    list is parallel — same length, same order — with either a MatchScore
    or an error string per element.

    Failures don't propagate: the LLM is flaky enough that one bad pair
    shouldn't sink the whole request. Callers surface the per-pair error
    string in the public response.
    """
    sem = asyncio.Semaphore(max(1, concurrency))
    total = len(pairs)
    counter = {"done": 0}
    # Resolve once per request — reading os.getenv on every pair adds nothing
    # but makes behaviour non-monotonic if someone flips the env mid-batch.
    provider = resolve_llm_provider()
    today = _today()

    async def _emit() -> None:
        if on_progress is None:
            return
        try:
            await on_progress(counter["done"], total)
        except Exception:
            logger.debug("on_progress callback raised", exc_info=True)

    async def _one(resume_id: str, job_id: str, resume: Resume, job: Job) -> PairScore:
        # One Collector per call — BAML populates `usage` after the call
        # completes. Fetching usage is best-effort: a malformed response or
        # provider-side failure may leave it empty, in which case we attribute
        # 0 tokens rather than raise.
        collector = Collector(name="resume-matching-score")
        try:
            async with sem:
                score = await with_timeout_retry(
                    lambda: b.ScoreMatch(
                        resume=resume, job=job, today=today,
                        baml_options={"client": provider, "collector": collector},
                    ),
                    label="ScoreMatch",
                )
            in_tok, out_tok = _collector_tokens(collector)
            counter["done"] += 1
            await _emit()
            return PairScore(
                resume_id=resume_id, job_id=job_id, score=score, error=None,
                input_tokens=in_tok, output_tokens=out_tok,
            )
        except BaseException as e:
            in_tok, out_tok = _collector_tokens(collector)
            counter["done"] += 1
            await _emit()
            return PairScore(
                resume_id=resume_id,
                job_id=job_id,
                score=None,
                error=f"{type(e).__name__}: {e}",
                input_tokens=in_tok, output_tokens=out_tok,
            )

    return await asyncio.gather(*[_one(*p) for p in pairs])


def _collector_tokens(collector) -> tuple[int, int]:
    """Read (input, output) token counts from a BAML Collector, defensively.

    BAML's `collector.usage` returns `Usage(input_tokens, output_tokens)`
    after a call completes. Either field may be None depending on the
    provider response. Wrap the whole access in try/except so a usage
    glitch never breaks scoring — the request still succeeds, we just
    attribute 0 tokens.

    Module-level so tests can monkey-patch it to simulate provider usage.
    """
    try:
        usage = collector.usage
        return int(usage.input_tokens or 0), int(usage.output_tokens or 0)
    except Exception:
        return 0, 0


def _b64(data: bytes) -> str:
    import base64
    return base64.b64encode(data).decode("ascii")


# ---------------------------------------------------------------------------


async def match_all(
    resumes: List[ResumeInput],
    jobs: List[JobInput],
    *,
    top_k: int = DEFAULT_TOP_K,
    concurrency: int = DEFAULT_CONCURRENCY,
    on_progress: Optional[ProgressFn] = None,
) -> MatchReport:
    """Parse resumes + jobs, then score each resume against the full job list.

    Pipelining: scoring of a given resume starts the moment that resume's
    parse completes, independent of other resumes. We still need all JDs
    parsed before any scoring (top-k requires the full job universe), so we
    await the JD parses up-front — but resume parses and scoring overlap.

    Failed parses: resumes with parse errors are returned with parse_error
    and no top_matches. Hard-failing match pairs still appear in the ranking
    so the UI can surface the gap analysis (see ScoreMatch prompt).

    `on_progress` — if provided, fires at each meaningful milestone with a
    dict payload (keys: type, counts). Never raises into the pipeline; the
    caller is expected to swallow exceptions in its own callback.
    """
    sem = asyncio.Semaphore(concurrency)
    # Resolve once per run, same reasoning as score_pairs — flipping the env
    # mid-batch shouldn't make half the pairs go to a different provider.
    provider = resolve_llm_provider()
    today = _today()

    async def _emit(event: Dict[str, Any]) -> None:
        if on_progress is None:
            return
        try:
            await on_progress(event)
        except Exception:
            logger.debug("on_progress callback raised", exc_info=True)

    total_jds = len(jobs)
    total_resumes = len(resumes)
    await _emit({
        "type": "start",
        "resumes_total": total_resumes,
        "jd_files_total": total_jds,
    })

    # Kick off every parse immediately — resumes and JDs run concurrently
    # rather than one group after the other.
    jd_tasks = [asyncio.create_task(_parse_jobs(j)) for j in jobs]
    resume_tasks = [asyncio.create_task(_parse_resume(r)) for r in resumes]

    # Await JDs via as_completed so we can emit progress per file finish.
    # Scoring can't start until ALL JDs are parsed (need the job universe
    # for top-k), so we still gate on completion of every JD task.
    job_list: List[JobParse] = []
    jd_done = 0
    for fut in asyncio.as_completed(jd_tasks):
        source_filename, parsed_jobs, err = await fut
        jd_done += 1
        if err:
            logger.warning("JD parse error for %s: %s", source_filename, err)
        else:
            for job in parsed_jobs:
                job_list.append(JobParse(source_filename=source_filename, job=job))
        await _emit({
            "type": "jd_parsed",
            "filename": source_filename,
            "jobs_added": 0 if err else len(parsed_jobs),
            "jd_files_done": jd_done,
            "jd_files_total": total_jds,
            "jobs_total": len(job_list),
            "error": err,
        })
    await _emit({
        "type": "jds_ready",
        "jobs_total": len(job_list),
    })

    # Per-resume pipeline: await my own parse, then fan out scoring against
    # job_list. All resumes run this concurrently, so a fast-parsing resume
    # starts scoring while a slow one is still parsing.
    resume_counter = {"parsed": 0, "scored": 0}

    async def _match_one(resume_task: asyncio.Task) -> ResumeReport:
        filename, resume, err = await resume_task
        resume_counter["parsed"] += 1
        # Don't include resume.name — PII landing in shared log aggregators
        # would force a Data Processing Agreement with every partner. Filename
        # is already a partner-supplied identifier (often desensitized).
        await _emit({
            "type": "resume_parsed",
            "filename": filename,
            "resumes_parsed": resume_counter["parsed"],
            "resumes_total": total_resumes,
            "error": err,
        })
        if err or resume is None:
            resume_counter["scored"] += 1
            await _emit({
                "type": "resume_scored",
                "filename": filename,
                "resumes_scored": resume_counter["scored"],
                "resumes_total": total_resumes,
                "skipped": True,
            })
            return ResumeReport(filename=filename, resume=None, parse_error=err)
        if not job_list:
            resume_counter["scored"] += 1
            await _emit({
                "type": "resume_scored",
                "filename": filename,
                "resumes_scored": resume_counter["scored"],
                "resumes_total": total_resumes,
                "skipped": True,
            })
            return ResumeReport(filename=filename, resume=resume, top_matches=[])

        score_coros = [_score(resume, jp.job, sem, provider, today) for jp in job_list]
        results = await asyncio.gather(*score_coros, return_exceptions=True)

        matches: List[Match] = []
        for j_idx, res in enumerate(results):
            if isinstance(res, BaseException):
                logger.warning("ScoreMatch failed for %s x job %d: %s", filename, j_idx, res)
                continue
            matches.append(Match(job_index=j_idx, score=res))  # type: ignore[arg-type]
        matches.sort(key=lambda m: total_score(m.score), reverse=True)
        resume_counter["scored"] += 1
        top = matches[:top_k]
        top_total = total_score(top[0].score) if top else None
        await _emit({
            "type": "resume_scored",
            "filename": filename,
            "resumes_scored": resume_counter["scored"],
            "resumes_total": total_resumes,
            "top_score": top_total,
            "top_verdict": (
                verdict_for(top_total, list(top[0].score.hard_fails)) if top else None
            ),
        })
        return ResumeReport(
            filename=filename,
            resume=resume,
            top_matches=top,
        )

    resume_reports = await asyncio.gather(*[_match_one(t) for t in resume_tasks])
    return MatchReport(resumes=list(resume_reports), jobs=job_list)
