# Project: talent-engine

FastAPI service for resume parsing + JD-to-resume matching, backed by BAML-driven LLM calls. Deployed to mainland China cloud infrastructure (UCloud, or 微信云托管 for mini-program partners).

## Architecture at a glance

Five public endpoints, three subsystems:

```
┌─────────────────────┐
│ /v1/resume/parse    │──┐
│ /v1/job/parse       │  │   X-API-Key auth → enforce_rate_limit
│ /v1/resume-matching │  │   │
│   /match            │  │   ▼
│   /match/async      │  ├─→ pipeline.score_pairs / parse_one
│   /match/{job_id}   │  │   │
└─────────────────────┘  │   ▼  with_timeout_retry (LLM_CALL_TIMEOUT_SEC, one retry)
                         │   ▼
                         │   BAML client (Default → Gemini/Qwen/Hunyuan/DeepSeek)
                         │       selected by LLM_PROVIDER env var
                         │
                         ├─→ UsageStore.log(...)  → MySQL v1_resume_matching_usage
                         │   ApiKeyStore.lookup() → MySQL v1_resume_matching_api_keys
                         │
                         └─→ notify_once(CallEvent) → bounded queue → e2a SDK
                             (one email per call; metadata only)
```

Key boundaries:

- **`public_schema.py`** is the stable partner contract. `baml_client/types` is the internal shape. The translators in `public_schema.py` are the *only* place the two touch — BAML prompt-engineering changes don't ripple into partner code.
- **`pipeline.py`** is pure orchestration. Routers handle HTTP / validation / auth / usage logging; the pipeline only does parse + score over BAML.
- **`baml_src/*.baml`** is the source of truth for LLM prompts. `baml_client/` is generated (gitignored). Rebuild after editing: `cd v1/resume_matching && baml-cli generate`. The Dockerfile re-runs this during image build, so prod always reflects the committed `.baml`.
- **JD splitting** (`pipeline._split_jd_text`) is regex-based on the `招聘单位` header — deterministic and tested ([test_split_jd.py](api/v1/resume_matching/tests/test_split_jd.py)). Don't replace with an LLM call: bundled-file extraction is exactly where LLMs drop/merge/hallucinate jobs.

## Per-call email notifications

`v1/notify/` mails one metadata-only summary per `/v1/*` call through the
e2a SDK. Two invariants, both load-bearing:

- **It can't fail a request.** `notify()` is synchronous, non-blocking, and
  swallows everything; `notify_api_call` guards the singleton lookup too.
  Same contract as `UsageStore.log`.
- **It can't carry candidate data.** `CallEvent` has counts and ids, no free
  text for résumé content. A test asserts the exact field set, so adding a
  field that could hold PII fails CI. Keep it that way.

Handlers emit rich events and claim the request via `notify_once`; a
fallback middleware in `main.py` covers whatever never reached a handler
(401, 400, 404). `/health` is excluded so liveness probes don't mail.

`GET /match/{job_id}` mails **per poll** — deliberate, per the operator's
request, and the reason the send queue is bounded and sheds rather than
growing. Drops are counted and logged.

## Single-replica constraint

`_AsyncJobStore` and the rate-limiter `_Registry` both hold state in process memory. Horizontal scaling silently breaks async polls and dilutes rate limits — keep `max_replicas=1` in any deploy console. The lifespan logs a loud warning at boot to remind operators; set `TALENT_ENGINE_ALLOW_MULTI_REPLICA=1` to suppress once a shared store is wired (Redis or similar).

## LLM provider swap

Same image, different provider, no rebuild — set `LLM_PROVIDER` to `Gemini` / `Qwen` / `Hunyuan` / `DeepSeek`. The runtime hands the BAML client name to every call via `baml_options={"client": ...}`. Unknown values fail loud at first request (see `llm_config.resolve_llm_provider`).

Gemini is blocked from mainland CN VMs — don't use it from inside 云托管 or UCloud. Qwen and DeepSeek both work fine.

## China-cloud deployment gotchas

When deploying to mainland China cloud (UCloud, Aliyun, Tencent Cloud), several upstream resources are blocked or unreliable from mainland VMs. **Always configure these workarounds at the start of any new deployment.**

### Docker Hub is blocked (`registry-1.docker.io`)

Symptom: `docker pull` (or `docker compose up`) fails with `dial tcp ...:443: i/o timeout`.

Fix: configure registry mirrors in `/etc/docker/daemon.json` *before* the first pull:

```json
{
  "registry-mirrors": [
    "https://docker.m.daocloud.io",
    "https://docker.1panel.live",
    "https://hub-mirror.c.163.com",
    "https://mirror.baidubce.com"
  ]
}
```

Then `sudo systemctl restart docker`. Verify with `sudo docker info | grep -A 5 "Registry Mirrors"`.

These four mirrors are public and have been reliable as of 2026; if all four fail, search for "Docker Hub mirror" — Chinese cloud providers and community projects rotate the available list periodically.

### Docker install script blocked (`get.docker.com`)

Symptom: `curl -fsSL https://get.docker.com | sh` returns `Connection reset by peer`.

Fix: install via apt instead — `sudo apt-get install -y docker.io docker-compose-v2`. UCloud images already have `mirrors.ucloud.cn` configured, so apt is fast and reliable.

### GitHub clone can be slow or unreliable

Symptom: `git clone https://github.com/...` hangs or runs at <100 KB/s.

Fix: use a mainland mirror like `https://gitclone.com/github.com/<user>/<repo>.git` as fallback, or mirror the repo to Gitee and clone from there. For development of this project, pushing to Gitee as a secondary remote is a reasonable workflow if GitHub becomes a bottleneck.

### Other notes

- DashScope (Qwen), DeepSeek, and Tencent Hunyuan API endpoints work fine from mainland VMs (they're hosted in China).
- Google AI / Vertex (Gemini) endpoints are blocked — don't try `LLM_PROVIDER=Gemini` from a mainland VM.
- The `apt-get update` repos on UCloud images already use UCloud mirrors; no extra config needed.

## Known noisy logs (not bugs)

**`RuntimeError: Event loop is closed` from `aiomysql.connection.Connection.__del__`** — appears in short-lived scripts (e.g. `python -m v1.resume_matching.scripts.create_api_key`) *after* the script has finished doing real work. Caused by aiomysql's connection finalizer trying to schedule a socket close on an already-shutting-down event loop. Cosmetic; doesn't affect the long-running uvicorn process. If it ever gets noisy enough to mask real errors, wrap the script's `asyncio.run(...)` with `engine.dispose()` before the loop closes.
