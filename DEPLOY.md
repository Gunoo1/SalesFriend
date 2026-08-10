# SalesAgent — deploy to a Windows VM via git + Docker

Code travels through git (https://github.com/Eisco-LLC/SalesAgent).
**State does not**: chat history, users, caches, and the .env (secrets) move
as a file-copied *bundle*. The image itself is stateless.

## On this dev machine — refresh the bundle

```powershell
cd SalesAgent
.venv\Scripts\python.exe scripts\make_bundle.py     # -> C:\Apps\salesagent\
```

The bundle carries everything the container mounts:

```
C:\Apps\salesagent\
  .env       secrets (Anthropic/Seamless/Serper keys, Acumatica creds) — gitignored
  data\      app.db + checkpoints.db snapshots (users incl. paul, all chat
             history/artifacts), uploads\, estate\, trends_source.json
             (path pre-rewritten to the container's C:/data/uploads)
  prompts\   system.md — editable on the host without rebuild (hot-reloads)
```

Safe to run while the dev server is up (sqlite backup API). Then copy the
whole `C:\Apps\salesagent` folder to the VM (same path keeps the run line
copy-pasteable).

## On the VM (Windows containers, like the price app)

```powershell
git clone https://github.com/Eisco-LLC/SalesAgent.git   # or git pull
cd SalesAgent
docker build -t salesagent .

docker run -d --name salesagent -p 8504:8504 --restart always `
  --env-file C:\Apps\salesagent\.env `
  -v C:\Apps\salesagent\data:C:/data `
  -v C:\Apps\salesagent\prompts:C:/prompts `
  -e DATA_DIR=C:/data -e PROMPTS_DIR=C:/prompts `
  salesagent
```

- Port **8504** (8501-8503 / 8090 / 8508 are taken on the VM).
- Health: `http://<vm>:8504/api/health` (image has a HEALTHCHECK).
- Base is `servercore:ltsc2019` to match the Server 2019 VM (process
  isolation). If the host is ever Server 2022, change the FROM to ltsc2022.
- **Env changes need `docker rm -f salesagent` + the run line again** —
  `docker restart` does NOT re-read `--env-file` (learned the hard way with
  the price app's Acumatica block).

## Fresh install instead of carrying history?

Skip the bundle's data\: create the folders, put `.env` (from `.env.example`)
and `prompts\system.md` in place, run the container, then seed users inside:

```powershell
docker exec salesagent python -m salesagent.auth add-user gunoo --admin --password "..."
docker exec salesagent python -m salesagent.auth add-user paul --password "..."
```

The K12 estate rebuilds itself: ask the agent any K12 question and it runs
`k12_build_reference` (fresh NCES download, a few minutes, national).

## Update cycle

1. Dev machine: push to GitHub.
2. VM: `git pull`, `docker build -t salesagent .`,
   `docker rm -f salesagent`, run line above. Data survives — it lives in
   the mounted `C:\Apps\salesagent\data`, not the container.

## Current accounts (ride along in the bundle's app.db)

gunoo (admin) · admin (admin) · rep2 · paul — passwords are set in the DB;
admins can reset any of them from the in-app admin panel.
