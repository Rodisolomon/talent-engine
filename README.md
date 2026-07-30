# talent-engine

AI infrastructure powering a recruiting platform. Today: resume ↔ job-description matching via Gemini. Designed to grow to additional talent operations (parsing, ranking, screening).

Extracted from the `mnexa-ai` monorepo's `api/v1/resume_matching/` module. The folder layout intentionally mirrors mnexa-ai (`api/v1/...`) so code can be diffed and ported back if needed.

## Layout

```
api/
  main.py                      # FastAPI entrypoint, mounts routers, /health
  conftest.py                  # disables rate limiter for the test session
  v1/
    db/database.py             # async + sync engines, init_db, close_db
    routers/deps.py            # get_engine FastAPI dependency
    resume/parse_router.py     # POST /v1/resume/parse (PDF → structured)
    resume/pdf_text.py         # pypdf wrapper
    job/parse_router.py        # POST /v1/job/parse (JD text → structured)
    notify/                    # e2a email notification, one per API call
    resume_matching/           # the matching service
      public_router.py         # POST /match, /match/async, GET /match/{id}
      pipeline.py              # parse + score orchestration
      auth.py                  # X-API-Key FastAPI dependency
      rate_limit.py            # per-api_key_id token bucket
      llm_call.py              # timeout + retry wrapper around BAML calls
      llm_config.py            # LLM_PROVIDER env → BAML client name
      public_schema.py         # public Pydantic models + BAML translators
      baml_src/                # BAML prompts (4-provider clients)
      baml_client/             # GENERATED — run `baml-cli generate`
      storage/                 # api_keys + usage tables (own MetaData)
      tests/                   # pytest, sqlite in-memory
      scripts/                 # operator CLI (manage_api_keys, demo, smoke test)
  requirements.txt
  requirements-dev.txt
  pyproject.toml
  Dockerfile
```

## Local setup

```bash
cd api
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# Generate the BAML client (gitignored — must regen after editing .baml)
cd v1/resume_matching && baml-cli generate && cd ../..

# Start a local MySQL (or set DATABASE_URL to your own)
# docker run -d -p 3306:3306 \
#   -e MYSQL_ROOT_PASSWORD=root -e MYSQL_DATABASE=talent_engine \
#   -e MYSQL_USER=talent_engine -e MYSQL_PASSWORD=talent_engine \
#   mysql:8.0

cp ../.env.example ../.env  # edit values
uvicorn main:app --reload
```

## Tests

```bash
cd api && pytest
```

Tests use SQLite in-memory and stub the BAML client — no MySQL or LLM credentials required.

## LLM provider selection

The `LLM_PROVIDER` env var picks which client BAML routes through at runtime — same image, different provider. Supported values:

| `LLM_PROVIDER` | Hosted by | Endpoint | Why pick |
|---|---|---|---|
| `Gemini` (default) | Google | Vertex AI / AI Studio | Original. Best for non-China deployments. |
| `Qwen` | Aliyun DashScope | OpenAI-compatible | Strong Chinese, mature API. |
| `Hunyuan` | Tencent | OpenAI-compatible | Best vendor consolidation if hosted on Tencent Cloud. |
| `DeepSeek` | DeepSeek | OpenAI-compatible | Cheapest per token; text-only. |

Wired through `baml_options={"client": LLM_PROVIDER}` at every BAML call site — see [llm_config.py](api/v1/resume_matching/llm_config.py) and [main.baml](api/v1/resume_matching/baml_src/main.baml). Unknown values fail loud at first request.

Only set the API key for the active provider — leaving the others unset is fine. If you're switching providers mid-flight, re-run your eval set: structured-extraction quality varies meaningfully across models, and the prompts in `baml_src/` were originally tuned against Gemini.

## Managing API keys

`manage_api_keys` is the one-stop operator CLI. Run inside the API container
(or any environment where `DATABASE_URL` is set):

```bash
python -m v1.resume_matching.scripts.manage_api_keys create <partner-name>
python -m v1.resume_matching.scripts.manage_api_keys list
python -m v1.resume_matching.scripts.manage_api_keys revoke <id-or-name>
python -m v1.resume_matching.scripts.manage_api_keys rotate <partner-name>
```

- `create` — mints a key; plaintext is shown once, only the SHA-256 hash is
  persisted.
- `list` — every key (active + revoked) with id, name, prefix, status.
- `revoke` — by numeric id (single key) or by partner name (every active
  key for that partner).
- `rotate` — revoke all active keys for the partner and mint a fresh one
  in one shot. Use when a key has leaked.

The older `create_api_key` script is still wired for backwards compatibility
but `manage_api_keys` is the recommended entry point.

## Email notifications (e2a)

Every `/v1/*` call emails a one-message summary via [e2a](https://e2a.dev).
Off unless all three of `E2A_API_KEY`, `E2A_AGENT_EMAIL`, `E2A_NOTIFY_TO`
are set — so local dev and CI never send mail.

```bash
E2A_API_KEY=e2a_agt_...
E2A_AGENT_EMAIL=talent-engine@team.tokencanopy.com
E2A_NOTIFY_TO=ops@example.com        # comma-separated for several
E2A_NOTIFY_ENABLED=0                 # mute without removing credentials
```

Bodies are **metadata only** — endpoint, key name, request id, counts,
latency, status, provider. No résumé or JD content is ever mailed, and
`CallEvent` has no field that could carry it ([a test enforces
that](api/v1/notify/tests/test_e2a_notifier.py)). Résumé PII stays on the
existing LLM-provider egress path.

Two things worth knowing before turning this on in production:

- **`GET /match/{job_id}` mails on every poll.** Partners are told to poll
  every 2s ([接入文档.md](接入文档.md)), so one 3-minute async match produces
  roughly 90 emails. That is intended behaviour, not a bug.
- **Delivery is fire-and-forget and shed under load.** Sends go through a
  bounded in-process queue (`E2A_NOTIFY_QUEUE_MAX`, default 1000) drained by
  `E2A_NOTIFY_WORKERS` (default 2). When mail falls behind the request rate
  the queue drops events and logs a running count — a mail backlog must
  never become a memory leak or slow down a partner's request. Nothing in
  this path can fail an API call.

## China deployment

For WeChat mini-program partners, deploy on **Tencent Cloud 微信云托管 (Cloud Hosting)**. See [DEPLOY-tencent.md](DEPLOY-tencent.md) for the full runbook. The killer feature: mini-programs can call your API without configuring a request-domain whitelist, removing the biggest friction in partner onboarding.

PDF text extraction happens in Python via [pypdf](https://pypi.org/project/pypdf/) before any LLM call so the same code path works for all four providers — Qwen / Hunyuan / DeepSeek don't all support PDF input via OpenAI-compatible endpoints. Trade-off: scanned/image-only PDFs aren't supported (they surface as `PdfExtractionError`); add Tencent OCR or PaddleOCR upstream if you need them.
