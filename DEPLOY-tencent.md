# Deploying talent-engine on Tencent Cloud (微信云托管)

This guide covers deploying the API to **WeChat Mini Program Cloud Hosting (微信云托管 / Cloud Hosting)** so mini-program partners can call it without configuring a request-domain whitelist.

## Why 微信云托管 specifically

Mini-program `wx.request` calls into Tencent-hosted backends are **routed internally** — partners don't need to add your domain to their mini-program's 服务器域名 list, and don't have to re-submit the mini-program for review when adding you as a backend. This removes the single biggest friction in partner onboarding.

## Prerequisites

- A Chinese business entity (营业执照) registered with Tencent Cloud.
- A WeChat Mini Program AppID that you operate, **or** a partner mini-program where you're added as a 开发者 / 运营者. (Cloud Hosting is provisioned per-AppID; one Cloud Hosting environment can serve any number of mini-programs that explicitly invoke it.)
- ICP filing (备案) is **not** required for 微信云托管 — Tencent handles compliance for the auto-allocated `*.cloudbaserun.com` domain. Required only if you front it with your own custom domain.
- Tencent Cloud account with billing enabled.

## One-time setup

1. **Enable 微信云托管** in your mini-program's Tencent Cloud console:
   - Open 云开发 CloudBase → 云托管 in the WeChat 公众平台 / Tencent Cloud console.
   - Pick a region — `广州 (ap-guangzhou)` for nearest mainland latency.
   - Choose a billing plan. For low traffic, the basic 包年包月 (≈ ¥30/mo) is enough; switch to 按量计费 if you expect spiky load.

2. **Create a MySQL instance.** Two options:
   - **微信云托管 内置 MySQL**: simplest — provisioned from the same console (左侧栏 → MySQL), billed alongside the service, auto-wired to your VPC. Recommended for the partner-MVP scale.
   - **TencentDB for MySQL** (云数据库 MySQL): standalone managed MySQL if you want it independent of the 云托管 environment lifecycle. Same region as Cloud Hosting (`ap-guangzhou`), smallest tier (1 CPU / 2 GB), enable VPC peering so Cloud Hosting can reach it on a private IP.

   Either way, you end up with a connection string like `mysql://user:pass@10.x.x.x:3306/talent_engine`.

3. **Get LLM API keys.**
   - **Qwen (DashScope)**: visit `dashscope.console.aliyun.com` → 创建API-KEY. Note: DashScope is Aliyun, *not* Tencent — it's the same key whether you host on Tencent or anywhere else.
   - **Hunyuan**: console.cloud.tencent.com → 混元大模型 → 接入管理. Tencent-hosted, lower latency from Tencent Cloud Hosting.
   - **DeepSeek**: platform.deepseek.com → API Keys.

   You only need the key for the provider you'll use. Pick one based on the eval — don't enable all three in production unless you actually fall back between them.

## Deployment

Cloud Hosting deploys directly from a git repo, building the `Dockerfile` in your image. Two paths:

### Option 1: Console-based deploy (simplest first time)

1. Tencent Cloud console → 云托管 → 服务管理 → 新建服务.
2. Service name: `talent-engine-api`.
3. Code source: GitHub. Authorize Tencent's GitHub app to access `jiashuoz/talent-engine`.
4. Branch: `main`. **Build context: `api/`** (Dockerfile and [.dockerignore](api/.dockerignore) live there; the latter excludes `.venv`, `baml_client/`, tests, and local PDFs from the build upload). Dockerfile path: `api/Dockerfile`.
5. **Service config:**
   - Listen port: `80` (Cloud Hosting routes external traffic to whatever port your container listens on; the Dockerfile reads `$PORT`).
   - Min replicas: `1` (avoid cold starts on partner traffic).
   - Max replicas: **`1`** — not a typo, and not a knob to raise with traffic.
     `_AsyncJobStore` and the rate-limit registry both live in process
     memory, so a second replica makes most `/match/{job_id}` polls land on
     a container that never heard of the job (404) and divides every rate
     limit by the replica count. `api/main.py` logs a warning about this at
     every boot. Raise it only after a shared store (Redis) is wired, then
     set `TALENT_ENGINE_ALLOW_MULTI_REPLICA=1` to silence the warning.
   - Memory: `1 GB` (enough for FastAPI + concurrent BAML calls).
6. **Env vars** (under 环境变量):
   ```
   PORT=80
   DATABASE_URL=mysql://<user>:<pass>@<mysql-private-ip>:3306/talent_engine
   LLM_PROVIDER=Qwen          # or Hunyuan / DeepSeek
   QWEN_API_KEY=sk-...        # only the active provider's key is required

   # Per-call email notifications — omit all three to leave them off.
   E2A_API_KEY=e2a_agt_...
   E2A_AGENT_EMAIL=talent-engine@team.tokencanopy.com
   E2A_NOTIFY_TO=ops@example.com
   ```

   Note that `GET /match/{job_id}` mails on **every poll**, and partners
   poll every 2s — see the notifications section in
   [README.md](README.md) before enabling this on partner traffic.
7. Click 部署. First deploy takes ~5–10 min (Docker build + image push + container start).

### Option 2: CLI-based deploy via tcb-cli (faster for repeated pushes)

```bash
npm install -g @cloudbase/cli
tcb login
tcb run deploy --name talent-engine-api --target service
```
Requires a `cloudbaserc.json` in the repo root pointing at the right environment ID; can add later.

## Database initialization

The first request triggers `init_db()` in `api/main.py`'s lifespan, which calls `metadata.create_all` on the resume-matching tables. No separate migration step.

To mint the first API key, exec into a running container:

```bash
# From console: 服务管理 → talent-engine-api → 实例 → 登录 (web shell)
python -m v1.resume_matching.scripts.manage_api_keys create "first-partner"
```

The plaintext key is printed once. Store it somewhere safe before closing the shell.

## Mini-program partner integration

Once deployed, give partners the auto-allocated callable name (something like `talent-engine-12345-xxx.ap-guangzhou.app.tcloudbase.com`, or the shorter `wx.cloud.callContainer` API form).

Partner mini-program code (per Tencent's 微信云托管 SDK):
```js
wx.cloud.callContainer({
  config: { env: 'your-env-id' },
  path: '/v1/resume/parse',
  method: 'POST',
  data: { /* multipart payload */ },
  header: { 'X-API-Key': 'mnk_...' }
})
```
**No `wx.request` whitelist setup required.** This is the killer feature.

## Health check

Tencent Cloud Hosting can use `/health` (already wired) for liveness probes — set it in 服务设置 → 健康检查 → 路径: `/health`, expected: `200`.

## Logs + monitoring

- **Container logs**: 服务管理 → 实例日志 (real-time stdout/stderr).
- **Request metrics**: 服务管理 → 监控 (QPS, latency p50/p95, error rate).
- **DB metrics**: TencentDB console → 实例监控.

For a more operator-friendly view, mirror logs to 日志服务 CLS — adds searchability + alerts.

## Cost estimate

For low traffic (< 1k requests/day):
- Cloud Hosting: ~¥30–80 / month (1 instance, 1 GB).
- 微信云托管 内置 MySQL (1 CPU / 1 GB): ~¥60–80 / month. TencentDB MySQL standalone: ~¥100–140 / month (smallest tier).
- LLM tokens (Qwen / DeepSeek): scales with usage; ~¥0.001–0.005 per resume parse.

## Operational checklist before going live

- [ ] ICP filing if using a custom domain (`api.talent-engine.cn`)
- [ ] LLM provider eval against your match-quality benchmark
- [ ] First API key minted, stored in your password manager
- [ ] `LLM_PROVIDER` env var matches the LLM whose key you set
- [ ] Min replicas ≥ 1 to avoid cold starts on partner-facing endpoints
- [ ] Cloud Hosting + TencentDB in the same VPC + region
- [ ] `/health` wired into the liveness probe
- [ ] Partner onboarding kit prepared (key, docs URL, support contact)
