# SalesAgent — deploy to an Ubuntu droplet (all-local Ollama)

Target: DigitalOcean droplet, Ubuntu 24.04, 8 GB RAM / 4 vCPUs. No Docker —
on 8 GB the model wants every spare MB, so the app runs native under systemd
and Ollama runs as the service its installer creates. (The Windows-container
path in DEPLOY.md is for the Eisco VM and does not apply here.)

All commands below run on the droplet as root (`ssh root@<droplet-ip>`).

## 1. Base packages + swap

```bash
apt update && apt install -y python3-venv python3-pip git
# 4 GB swap: safety margin so a model load never OOM-kills the app.
fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

## 2. Ollama + models

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen3:8b        # orchestrator + extraction (one model resident)
ollama pull qwen3:4b        # optional: the "too slow" fallback, test both
```

Tune the service (queue users instead of splitting 4 vCPUs; keep the model
loaded instead of re-loading after idle; halve KV-cache memory):

```bash
mkdir -p /etc/systemd/system/ollama.service.d
cat > /etc/systemd/system/ollama.service.d/override.conf <<'EOF'
[Service]
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_KEEP_ALIVE=-1"
Environment="OLLAMA_FLASH_ATTENTION=1"
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"
EOF
systemctl daemon-reload && systemctl restart ollama
```

## 3. App

```bash
git clone https://github.com/Gunoo1/SalesFriend.git /opt/salesagent
# (private repo? use a GitHub fine-grained PAT:
#  git clone https://<TOKEN>@github.com/Gunoo1/SalesFriend.git /opt/salesagent)
cd /opt/salesagent
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
nano .env
```

`.env` for the all-local setup — the lines that differ from the example:

```
ORCHESTRATOR_MODEL=qwen3:8b
EXTRACT_MODEL=qwen3:8b
```

**Keep both models THE SAME on 8 GB RAM.** Extraction runs inside tools
mid-turn; a different EXTRACT_MODEL means Ollama holds (or swaps) two models
during a turn — on this droplet that's an OOM or a 20s+ reload thrash per
tool call. If you drop to qwen3:4b for speed, the pair 4b + qwen3:1.7b does
fit together (~4 GB) and a split becomes reasonable.

Also fill in whatever API keys the tools should have (SERPER_API_KEY etc.).
ANTHROPIC_API_KEY can stay empty when both models are local. Leave
SEAMLESS_DRY_RUN=1 until credits are meant to be real.

## 4. Users + service

```bash
.venv/bin/python -m salesagent.auth add-user gunoo --admin --password "CHANGE-ME"
cp ops/salesagent.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now salesagent
curl -s localhost:8511/api/health   # expect 200
```

App is at `http://<droplet-ip>:8511`. If the droplet has a firewall
(`ufw status` / DO cloud firewall), open 8511 (and keep OpenSSH):
`ufw allow OpenSSH && ufw allow 8511/tcp && ufw enable`.

Plain HTTP + the app's own login is fine for testing; before real reps use
it, put Caddy or nginx with TLS in front and stop exposing 8511 directly.

## 5. Update cycle

```bash
cd /opt/salesagent && git pull && .venv/bin/pip install -r requirements.txt
systemctl restart salesagent
```

State (data/ with app.db, checkpoints, artifacts, uploads) lives in
/opt/salesagent/data and survives updates; git never touches it.

## Ops crib sheet

```bash
journalctl -u salesagent -f        # app logs
journalctl -u ollama -f            # model server logs
ollama ps                          # what's loaded, RAM, ctx
systemctl restart salesagent       # after .env edits (env is read at boot)
```
