"""Authenticated text-bundle → JSON job-description parsing.

  POST /v1/job/parse — multipart upload of .txt files, JSON response

Each uploaded file is split on `招聘单位` headers and parsed chunk-by-chunk
in parallel; one upload may yield multiple parsed jobs. If no `招聘单位`
header is found the whole file is sent to the list-parse fallback so
unconventional layouts still produce output.

Auth + usage logging share the resume-matching API key store: a single
key works for `/v1/resume/parse`, `/v1/job/parse`, and the match endpoints.

Sync only. Bundle parsing is bounded by max 10 files × 200 KB and
~PARSE_CONCURRENCY chunks in flight; typical batches finish in 5–30 s.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncEngine

from v1.notify import CallEvent, notify_once
from v1.resume_matching.auth import require_api_key
from v1.resume_matching.rate_limit import enforce_rate_limit
from v1.resume_matching.baml_client.async_client import b
from v1.resume_matching.llm_call import with_timeout_retry
from v1.resume_matching.llm_config import resolve_llm_provider
from v1.resume_matching.pipeline import _collector_tokens, _split_jd_text
from v1.resume_matching.public_schema import from_baml_job
from v1.resume_matching.storage import ApiKeyRecord, UsageRecord, UsageStore
from v1.job.parse_schema import (
    ParseJobErrorItem,
    ParseJobResponse,
    ParseJobResultItem,
    ParseJobStats,
)
from v1.routers.deps import get_engine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/job", tags=["job"])


# Defensive caps. Per-file size is much smaller than resume PDFs because
# JD bundles are plain text. Parsing 30 chunks in parallel is comfortable
# under WeChat's default 60s request timeout.
MAX_FILES = 10
MAX_TEXT_BYTES = 200 * 1024
MAX_CHUNKS_PER_REQUEST = 100        # hard cap: prevent runaway bundles from blowing the LLM quota
PARSE_CONCURRENCY = 10


async def _parse_chunk(
    *,
    filename: str,
    chunk_index: int,
    text: str,
    sem: asyncio.Semaphore,
    llm_provider: str,
) -> tuple[Optional[ParseJobResultItem], Optional[ParseJobErrorItem], int, int]:
    """Parse one JD chunk with a per-call BAML Collector.

    Returns (success_item, error_item, input_tokens, output_tokens). Exactly
    one of the first two is non-None.
    """
    from baml_py import Collector

    collector = Collector(name="job-parse-single")
    async with sem:
        try:
            baml_job = await with_timeout_retry(
                lambda: b.ParseSingleJob(
                    text=text,
                    baml_options={"client": llm_provider, "collector": collector},
                ),
                label=f"ParseSingleJob[{filename}#{chunk_index}]",
            )
            in_tok, out_tok = _collector_tokens(collector)
            return (
                ParseJobResultItem(
                    job_id=f"{filename}#{chunk_index}",
                    filename=filename,
                    chunk_index=chunk_index,
                    job=from_baml_job(baml_job),
                ),
                None,
                in_tok,
                out_tok,
            )
        except Exception as e:
            logger.exception("ParseSingleJob failed for %s chunk %d", filename, chunk_index)
            in_tok, out_tok = _collector_tokens(collector)
            return (
                None,
                ParseJobErrorItem(
                    filename=filename,
                    chunk_index=chunk_index,
                    error=f"{type(e).__name__}: {e}",
                ),
                in_tok,
                out_tok,
            )


async def _parse_bundle_fallback(
    *,
    filename: str,
    text: str,
    llm_provider: str,
) -> tuple[List[ParseJobResultItem], List[ParseJobErrorItem], int, int]:
    """Single LLM call that returns a list of jobs — used when no `招聘单位`
    header is found.

    No per-chunk error granularity here: either the call returns a list or
    we record a single file-level error. The split-and-parse-each path is
    preferred whenever headers are present.
    """
    from baml_py import Collector

    collector = Collector(name="job-parse-list")
    try:
        baml_jobs = await with_timeout_retry(
            lambda: b.ParseJobDescriptions(
                text=text,
                baml_options={"client": llm_provider, "collector": collector},
            ),
            label=f"ParseJobDescriptions[{filename}]",
        )
        in_tok, out_tok = _collector_tokens(collector)
        parsed = [
            ParseJobResultItem(
                job_id=f"{filename}#{i}",
                filename=filename,
                chunk_index=i,
                job=from_baml_job(j),
            )
            for i, j in enumerate(baml_jobs)
        ]
        return parsed, [], in_tok, out_tok
    except Exception as e:
        logger.exception("ParseJobDescriptions fallback failed for %s", filename)
        in_tok, out_tok = _collector_tokens(collector)
        return [], [
            ParseJobErrorItem(
                filename=filename,
                chunk_index=0,
                error=f"{type(e).__name__}: {e}",
            ),
        ], in_tok, out_tok


def _client_ip(request: Request) -> Optional[str]:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


@router.post("/parse", response_model=ParseJobResponse)
async def parse_endpoint(
    request: Request,
    jds: List[UploadFile] = File(
        ...,
        description="One or more .txt files. Each may contain a single JD or a bundle separated by `招聘单位` headers.",
    ),
    api_key: ApiKeyRecord = Depends(enforce_rate_limit),
    engine: AsyncEngine = Depends(get_engine),
) -> ParseJobResponse:
    """Parse JD text bundles into the structured `Job` shape consumed by /match.

    Per-chunk failures (bad LLM extraction on a single JD) appear in
    `errors[]` with status 200; only request-level failures return 4xx/5xx.
    """
    if not jds:
        raise HTTPException(400, "jds must be non-empty")
    if len(jds) > MAX_FILES:
        raise HTTPException(413, f"max {MAX_FILES} files per request")

    started = time.perf_counter()

    # Read + size-validate every file before kicking off any LLM work.
    files: List[tuple[str, str]] = []
    for upload in jds:
        data = await upload.read()
        if len(data) > MAX_TEXT_BYTES:
            raise HTTPException(
                413, f"{upload.filename}: file exceeds {MAX_TEXT_BYTES // 1024} KB"
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as e:
            raise HTTPException(
                400, f"{upload.filename}: file must be UTF-8 ({e})"
            )
        files.append((upload.filename or "unnamed.txt", text))

    # Split each file. Files with no `招聘单位` header go through the
    # list-fallback path; others go through the parallel-chunk path.
    chunked: List[tuple[str, int, str]] = []                  # (filename, chunk_index, text)
    fallback_files: List[tuple[str, str]] = []                # (filename, text)
    for filename, text in files:
        chunks = _split_jd_text(text)
        if not chunks:
            fallback_files.append((filename, text))
            continue
        for i, chunk in enumerate(chunks):
            chunked.append((filename, i, chunk))

    total_chunks = len(chunked) + len(fallback_files)         # fallback files count as 1 unit each
    if total_chunks > MAX_CHUNKS_PER_REQUEST:
        raise HTTPException(
            413,
            f"too many JD chunks: {total_chunks} > {MAX_CHUNKS_PER_REQUEST}",
        )

    sem = asyncio.Semaphore(PARSE_CONCURRENCY)
    llm_provider = resolve_llm_provider()
    chunk_results = asyncio.gather(
        *[_parse_chunk(filename=fn, chunk_index=ci, text=t, sem=sem, llm_provider=llm_provider)
          for fn, ci, t in chunked]
    )
    fallback_results = asyncio.gather(
        *[_parse_bundle_fallback(filename=fn, text=t, llm_provider=llm_provider) for fn, t in fallback_files]
    )
    chunk_out, fallback_out = await asyncio.gather(chunk_results, fallback_results)

    parsed: List[ParseJobResultItem] = []
    errors: List[ParseJobErrorItem] = []
    total_in = 0
    total_out = 0

    for ok, err, in_tok, out_tok in chunk_out:
        if ok is not None:
            parsed.append(ok)
        if err is not None:
            errors.append(err)
        total_in += in_tok
        total_out += out_tok

    for fb_parsed, fb_errors, in_tok, out_tok in fallback_out:
        parsed.extend(fb_parsed)
        errors.extend(fb_errors)
        total_in += in_tok
        total_out += out_tok

    elapsed_ms = int((time.perf_counter() - started) * 1000)

    # Usage log — overload pair_count/pairs_failed as chunks-attempted/chunks-failed
    # so the existing dashboard queries on the usage table keep working.
    await UsageStore(engine).log(UsageRecord(
        api_key_id=api_key.id,
        endpoint="parse_job",
        resume_count=0,
        job_count=len(files),
        pair_count=total_chunks,
        pairs_failed=len(errors),
        input_tokens=total_in,
        output_tokens=total_out,
        elapsed_ms=elapsed_ms,
        status="ok",
        error=None,
        client_ip=_client_ip(request),
    ))

    notify_once(request, CallEvent(
        endpoint="parse_job",
        method="POST",
        path="/v1/job/parse",
        http_status=200,
        outcome="error" if errors else "ok",
        elapsed_ms=elapsed_ms,
        request_id=getattr(request.state, "request_id", None),
        api_key_name=api_key.name,
        api_key_id=api_key.id,
        llm_provider=llm_provider,
        client_ip=_client_ip(request),
        counts={
            "files": len(files),
            "chunks": total_chunks,
            "parsed": len(parsed),
            "failed": len(errors),
        },
        error=f"{len(errors)} of {total_chunks} chunk(s) failed to parse" if errors else None,
    ))

    return ParseJobResponse(
        status="completed",
        parsed=parsed,
        errors=errors,
        stats=ParseJobStats(
            files_received=len(files),
            chunks_detected=total_chunks,
            jobs_parsed=len(parsed),
            jobs_failed=len(errors),
            elapsed_ms=elapsed_ms,
            input_tokens=total_in,
            output_tokens=total_out,
        ),
    )
