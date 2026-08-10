# SalesAgent — the chat-driven sales rep copilot

Reps log in, type asks like *"find K12 districts in NJ with strong science
programs and get purchasing contacts"* or *"scrape these SKUs on VWR"*, and a
claude-sonnet-5 LangGraph agent calls tools over data **this app gathered
itself**. Results render as live **artifacts** (tables / charts / maps) in
the workspace panel and MUTATE on follow-up chat ("only show NJ", "chart it
by state", "export to excel") — server-side transforms over stored data,
never a re-query, never data through the model's context window.

**Self-contained by design (2026-08-06):** no other project's databases or
files are read. Reference data lives in the app's own estate —
`data/estate/<domain>/runs/<UTC-stamp>/` immutable snapshots built by the
app's own download jobs (fresh public sources), with `current.json` pointing
at the run tools read. Past runs stay on disk as history; live lookups are
memoized in app.db (`company_locations`, `business_status`, `api_cache`) —
all populated only by this app's own activity.

Built 2026-08-05 per the approved architecture plan
(`~/.claude/plans/encapsulated-kindling-parasol.md`). Status: **M0–M5 gates
passed live**, estate decoupling verified live.

## Run (dev)

```
cd SalesAgent
.venv\Scripts\python.exe run.py          # http://localhost:8511
.venv\Scripts\python.exe -m salesagent.auth add-user <name> [--admin]  # CLI seed
```

Users seeded so far: `gunoo` (admin), `admin` (admin), `rep2`. Admins get an
**admin** button in the sidebar footer: add/remove users, reset passwords
(revokes their sessions), and an org activity view (per-user convo/message/
credit/job counts + a merged recent-events feed incl. Seamless spends vs the
day cap). Removing a user locks them out immediately; their past
conversations stay in the DB for history but become unreachable.

## What the agent can do (26 tools)

| Family | Tools | Notes |
|---|---|---|
| K12 intel (free, own estate) | k12_build_reference (**builds the estate** from fresh NCES CCD/F-33/CRDC public data), k12_find_districts, k12_district_profile, k12_contacts | estate missing → tools say so and the agent builds it; partial-state estates refuse uncovered queries |
| People (Seamless.ai) | seamless_search (preview), seamless_research (**paid → approval card**) | 5-layer credit rails; `SEAMLESS_DRY_RUN=1` default; results persist to contacts_app (the app's own contact history) |
| Gov spend (free) | usaspending_vendor_customers, usaspending_keyword_vendors (+Haiku noise filter), checkbook_vendor_customers, checkbook_basket | coverage limits baked into descriptions |
| Geo | find_company_locations (Fastenal=locator scrape job, others=OSM brand job; memoized 30d in company_locations), find_nearby_orgs (Overpass, vendored state bboxes), verify_business_status (Serper, open/closed) | grainger.com scrape denylisted → OSM |
| Prospect combining | entity_merge (dedupe across sources w/ in_<source> flags), geo_rank (haversine join: rank branches by nearby districts/enrollment) | server-side over full rows |
| Reference (seeded) | adoption_calendar, kit_maker_roster | curated JSON vendored in `salesagent/ref/seeds/` with provenance columns |
| Research | web_search (Serper→DDG fallback on ANY Serper failure), fetch_page (curl_cffi+pdfplumber+Haiku extract), sales_brief | blocked = UNKNOWN, never absence |
| Price | price_own (instant Acumatica), price_scrape (job → price_comparison app) | needs `PRICE_COMPARISON_URL` |
| Views | transform_artifact, artifact_peek, entity_merge, export_excel, job_status | the dynamic-UI machinery |

**Uploads:** attach a CSV/XLSX in chat → it becomes a table artifact; the agent
is told on the next turn and any list-taking tool reads it server-side
(`{artifact_id, column}`).

## Architecture (short)

```
static/            vanilla JS SPA (no build): app.js chat+SSE, artifacts.js
                   renderers (Chart.js + Leaflet vendored), chat.css
salesagent/
  web/             FastAPI: auth, chat SSE (fetch-stream POST), artifacts,
                   uploads, jobs SSE
  agent/           LangGraph loop: agent ↔ custom tools node; confirm gate
                   (interrupt) runs BEFORE any handler; AsyncSqliteSaver on
                   data/checkpoints.db (thread_id = conversation id);
                   prompts/system.md hot-reloads (mtime)
  tools/           @tool_spec registry (pkgutil discovery — drop a file to add
                   a tool); envelope caps: model sees ≤15 sample rows, full
                   data goes to the artifact store
  artifacts/       zlib row store + version chains + the transform op grammar
  estate.py        the app's OWN snapshot estate: runs/<stamp>/ + manifest +
                   atomic current.json pointer (history browsable, never
                   depended on)
  integrations/    seamless_client (ledger+day cap+dry-run), k12_local
                   (queries over the own estate + contacts_app),
                   socrata detect(), usaspending, overpass, serper, fetchers,
                   claude_cached (Haiku memoized), price_comparison client,
                   xlsx (shop palette + auto Methodology tab)
  jobs/            ThreadPool jobs + ring logs; runners: k12_build (estate
                   builder), branch_finder, price_scrape (delegated),
                   verify_status_bulk
  ref/seeds/       vendored static reference (state_bboxes.json, adoption
                   calendar, kit makers, checkbook dataset directory)
data/app.db        users/convos/messages/artifacts/jobs/ledger/caches (WAL)
data/checkpoints.db  LangGraph state (separate file: lock isolation)
data/estate/       the self-built reference snapshots (k12/runs/<stamp>/)
```

Everything the app knows lives under `data/` and was fetched by the app
itself. First use on a fresh install: ask anything K12 — the agent will
report the estate is missing and run `k12_build_reference` (national build =
~14 requests to educationdata.urban.org, a few minutes).

## .env keys

`ANTHROPIC_API_KEY` `SEAMLESS_API_KEY` `SERPER_API_KEY` (unset/failed = web
search falls back to DDG; open/closed verification needs it)
`GOOGLE_PLACES_API_KEY` `PRICE_COMPARISON_URL` (empty = price tools report
not-configured) `ORCHESTRATOR_MODEL=claude-sonnet-5`
`EXTRACT_MODEL=claude-haiku-4-5` `SEAMLESS_DAY_CAP=200`
`SEAMLESS_CONFIRM_THRESHOLD=5` `SEAMLESS_CONVO_BUDGET=25`
`SEAMLESS_DRY_RUN=1` `PORT=8511`

## Credit safety (Seamless)

1. org ledger in app.db (every call, header-diffed)
2. per-day org cap checked before any spend
3. per-conversation budget
4. **approval card**: paid tools interrupt the graph; the rep approves/denies
   in the UI; resume survives restarts (durable checkpoint); a denial becomes
   a ToolMessage so the model adapts and never retries
5. `SEAMLESS_DRY_RUN=1` end-to-end synthetic mode (the default until go-live)

## Version pins that matter

`langgraph==1.2.4` + `langgraph-checkpoint-sqlite==3.1.1` + `aiosqlite==0.20.0`
+ `langchain-anthropic==1.5.4` (this exact set passed the sonnet-5 bind_tools
smoke; 2.x checkpoint-sqlite and aiosqlite 0.21+ both break). Node functions
MUST annotate `config: RunnableConfig` or langgraph won't inject it.

## Deploy (Windows VM)

`docker build -t salesagent .` then see the run line in the Dockerfile.
Port 8511. Mount `data/` + `prompts/` dirs —
that's ALL the state; there is no external database to copy in. The estate
builds itself inside the container on first use (or pre-build by asking the
agent once).

## Adding a tool

Drop a module in `salesagent/tools/` with a `@tool_spec(...)`-decorated
function `(ctx, **params) -> envelope`. Use `table_envelope()` for data
results (it stores the artifact + emits the SSE event + caps the preview).
Set `cost_class`; `PAID_CREDITS` or `needs_confirmation=True` puts it behind
the approval card. Restart; discovery is automatic (duplicate names raise).
