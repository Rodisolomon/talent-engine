# Deploying talent-engine on UCloud

This guide covers deploying the API to **UCloud mainland China** at the most basic level: a single **ULightHost** (轻量应用云主机) running both the API and MySQL via `docker compose`. Cheapest, simplest, no managed DB. Right answer for MVP / partner-validation scale (< 1k requests/day).

When you outgrow it, the [Scaling up](#scaling-up) section covers the migration paths (UDB MySQL on a UHost, then UK8S).

## Why ULightHost (and what you give up vs Tencent 云托管)

ULightHost gives you a fixed-price, all-in-one box that runs your Dockerized stack. Trade-offs vs Tencent 微信云托管:

- **No `wx.cloud.callContainer` shortcut.** Mini-program partners must add your domain to their `request合法域名` whitelist and re-submit their mini-program for review. There's no way around this on any cloud except Tencent 云托管.
- **ICP备案 is on you** for any custom mainland domain. UCloud has a filing portal but doesn't auto-handle a pre-filed subdomain the way 云托管's `*.cloudbaserun.com` does.
- **More control in exchange.** Standard Docker, easy to migrate off later.

## Prerequisites

- Chinese business entity (营业执照).
- UCloud account (`ucloud.cn`) registered via a Chinese-resident colleague's phone, then completed **企业实名认证** with the 营业执照. After 企业认证 the account is corporate, not tied to that individual.
- Billing enabled (corporate invoice / 对公转账 supported).
- An ICP-filed domain for partner-facing custom DNS. Filing through UCloud's 备案 portal takes 2–4 weeks. Until it's filed, test over the EIP's public IP without HTTPS.

## One-time setup

1. **Pick a region.**
   - `cn-bj2` (Beijing 2) — best for North China users.
   - `cn-sh2` (Shanghai 2) — best for East China.
   - `cn-gd` (Guangzhou) — best for South China + closest to HK.

2. **Get LLM API keys.**
   - **Qwen (DashScope)**: `dashscope.console.aliyun.com` → 创建API-KEY. (DashScope is Aliyun, but the key works from anywhere.)
   - **DeepSeek**: `platform.deepseek.com` → API Keys.
   - **Hunyuan**: requires a Tencent Cloud account — skip if you don't have one.

   You only need the active provider's key in `.env`.

## Deployment

The API and MySQL run together on one ULightHost via `docker-compose.yml` (already at repo root).

### Step 1: Create the ULightHost

UCloud console → 轻量应用云主机 → 创建实例.

- **Region**: from setup above.
- **Image**: Ubuntu 22.04 LTS.
- **Spec**: **2 vCPU / 4 GB / 60 GB SSD** (~¥80–100/月). The 4 GB matters because MySQL + FastAPI + BAML's concurrent LLM calls together push past 2 GB under any real load.
- **Bandwidth**: 4–5 Mbps bundled (raise via console anytime).
- **Login**: upload your SSH public key. Skip root password.

Note the assigned **public IP** — that's your EIP equivalent.

### Step 2: Configure the firewall

ULightHost console → 防火墙. Inbound rules:

- `22` (SSH) — restrict to your IP if stable.
- `80` and `443` (Caddy / HTTPS) — open to internet.

Outbound is unrestricted by default (needed for LLM API calls).

### Step 3: Install Docker

```bash
ssh ubuntu@<public-ip>
curl -fsSL https://get.docker.com | sh
sudo systemctl enable --now docker
sudo usermod -aG docker $USER && newgrp docker
```

### Step 4: Clone the repo

```bash
sudo apt-get update && sudo apt-get install -y git
sudo git clone https://github.com/jiashuoz/talent-engine.git /opt/talent-engine
sudo chown -R $USER:$USER /opt/talent-engine
cd /opt/talent-engine
```

### Step 5: Configure secrets

```bash
cp .env.example .env
chmod 600 .env
nano .env   # edit values below
```

In `.env`, set:
- `MYSQL_PASSWORD` and `MYSQL_ROOT_PASSWORD` to strong random values (e.g. `openssl rand -hex 24` each).
- `LLM_PROVIDER` to `Qwen` or `DeepSeek`.
- The matching `<PROVIDER>_API_KEY`.
- Leave the explicit `DATABASE_URL` commented out — `docker-compose.yml` constructs it from `MYSQL_PASSWORD` and points at the `db` service.
- Optionally `E2A_API_KEY` + `E2A_AGENT_EMAIL` + `E2A_NOTIFY_TO` to email a summary of every API call. All three are required or the notifier stays off. Be aware that `GET /match/{job_id}` mails on **every poll** and partners poll every 2s — see the notifications section in [README.md](README.md).

### Step 6: Start the stack

```bash
docker compose up -d --build
```

First run takes ~5–10 min (Docker image build + MySQL init + container start). Subsequent restarts are seconds.

Smoke test from inside the VM:
```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

The API is bound to `127.0.0.1:8000` — not reachable from the internet yet. Step 7 wires Caddy as a public HTTPS reverse proxy.

### Step 7: Front the API with Caddy (HTTPS)

Caddy auto-provisions Let's Encrypt certs. **This only works after your domain is ICP-filed and DNS points at the public IP.** Until then, skip Caddy and test by temporarily exposing `0.0.0.0:8000` in `docker-compose.yml` + opening port 8000 in the firewall (revert before going live).

```bash
sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt-get update && sudo apt-get install -y caddy

sudo tee /etc/caddy/Caddyfile > /dev/null <<'EOF'
api.talent-engine.cn {
  reverse_proxy 127.0.0.1:8000
}
EOF
sudo systemctl restart caddy
```

Smoke test from your laptop:
```bash
curl https://api.talent-engine.cn/health
```

## Database initialization

The first request triggers `init_db()` in `api/main.py`'s lifespan, which calls `metadata.create_all` on the resume-matching tables. No separate migration step.

To mint the first API key:
```bash
ssh ubuntu@<public-ip>
cd /opt/talent-engine
docker compose exec api python -m v1.resume_matching.scripts.create_api_key "first-partner"
```

The plaintext key is printed once. Store it before closing the shell.

## Updates and rollouts

```bash
ssh ubuntu@<public-ip>
cd /opt/talent-engine
git pull
docker compose up -d --build api    # rebuilds + restarts only the api service
```

MySQL data persists across rebuilds via the `mysql_data` named volume. There's brief API downtime (~5–10s) during the restart.

## Backups

MySQL data lives in the `mysql_data` Docker volume on the VM disk. **You are responsible for backups** — the simplest approach is a nightly `mysqldump` to a local backup directory plus offsite copy to UCloud **UFile** (object storage).

Daily `mysqldump` cron (run on the VM):
```bash
sudo mkdir -p /var/backups/talent-engine
sudo tee /etc/cron.daily/mysql-backup > /dev/null <<'EOF'
#!/bin/bash
set -euo pipefail
TS=$(date +%Y%m%d-%H%M%S)
cd /opt/talent-engine
# MYSQL_ROOT_PASSWORD comes from .env loaded by docker compose
docker compose exec -T db sh -c \
  'mysqldump -u root -p"$MYSQL_ROOT_PASSWORD" --single-transaction talent_engine' \
  | gzip > /var/backups/talent-engine/dump-$TS.sql.gz
# retain 14 days
find /var/backups/talent-engine -name 'dump-*.sql.gz' -mtime +14 -delete
EOF
sudo chmod +x /etc/cron.daily/mysql-backup
```

For offsite: install UCloud's `ufile-cli`, push `/var/backups/talent-engine/` to a UFile bucket on the same cron. Worth setting up before you have data you'd be sad to lose.

**This is the biggest operational risk of the on-box-MySQL model.** When it starts feeling fragile, migrate to UDB (see [Scaling up](#scaling-up)).

## Mini-program partner integration

Once deployed and your domain is ICP-filed, give partners:
- The full HTTPS URL (e.g., `https://api.talent-engine.cn/v1/resume/parse`).
- The API key.

**Partners must:**
1. Add your domain to their mini-program's **服务器域名 / request合法域名** list (mini-program 管理后台 → 开发管理 → 开发设置 → 服务器域名).
2. Re-submit their mini-program for review (audits new domains).

Partner request shape:
```js
wx.request({
  url: 'https://api.talent-engine.cn/v1/resume/parse',
  method: 'POST',
  data: { /* multipart payload */ },
  header: { 'X-API-Key': 'mnk_...' }
})
```

## Health check

The API exposes `/health` (already wired). For a single-VM deploy, the simplest liveness check is an external uptime monitor (UCloud **UMon** site monitoring, or any external pinger) hitting `https://api.talent-engine.cn/health` every minute. Docker's `--restart unless-stopped` already handles container crashes.

## Logs + monitoring

```bash
docker compose logs -f api      # API stdout/stderr
docker compose logs -f db       # MySQL logs
docker compose ps               # service status
```

VM-level metrics in UCloud **UMon** (CPU, memory, disk, network). For searchable logs + alerts, ship `docker compose logs` to UCloud **ULogHub** later.

## Cost estimate

For low traffic (< 1k requests/day):
- ULightHost (2 vCPU / 4 GB, bundled bandwidth): **~¥80–100 / month**
- LLM tokens (Qwen / DeepSeek): scales with usage; ~¥0.001–0.005 per resume parse.

That's it. No separate DB, EIP, or LB charges. **~¥100/mo all-in** before LLM tokens.

## Scaling up

Stay on this setup until one of these triggers fires; then graduate.

| Trigger | Migration |
|---|---|
| MySQL backups feel fragile, or you have data you're afraid to lose | **Move DB to UDB MySQL.** Spin up a UDB instance in the same region, `mysqldump` from the on-box DB, restore into UDB, swap `DATABASE_URL` to point at UDB's private IP, redeploy. The ULightHost stays as-is for the API. |
| Sustained CPU > 70% on the VM, or memory pressure | Vertical bump to 4 vCPU / 8 GB ULightHost first; if still tight, move to **UHost** (uncapped tiers, real VPC). |
| You need autoscaling, blue/green, or multiple services | **UK8S.** Same Docker image, standard k8s manifests, ULB-backed ingress. |
| You want serverless billing and can tolerate cold starts | UCloud **Cube**. Generally not worth it for a partner-facing API — min-replicas-1 erases the cost benefit. |

## Operational checklist before going live

- [ ] 企业实名认证 completed on the UCloud account
- [ ] ICP备案 filed for the custom domain
- [ ] DNS A record points at the ULightHost public IP
- [ ] LLM provider eval against your match-quality benchmark
- [ ] First API key minted, stored in your password manager
- [ ] `.env` has strong `MYSQL_PASSWORD` + `MYSQL_ROOT_PASSWORD` + correct `LLM_PROVIDER` / API key
- [ ] `.env` is `chmod 600`
- [ ] Caddy serving HTTPS, API container bound to `127.0.0.1` only
- [ ] `restart: unless-stopped` on both compose services
- [ ] Daily `mysqldump` cron running, backups verified by restoring into a scratch container
- [ ] UMon site monitor on `/health`
- [ ] SSH key-only auth on the VM (password auth disabled)
- [ ] Partner onboarding kit prepared (key, docs URL, support contact, **server-domain whitelist instructions**)
