# Kisan Copilot — Project Memory (CLAUDE.md)

> Save this file at the repo root: `~/GitHub/Hackathons/kisan-copilot/CLAUDE.md`.
> Claude Code reads it automatically as project context on every session.

## What we're building
A **voice, local-language crop-advisory copilot** for Indian farmers, for the **Snowflake CoCo CLI Hackathon 2026** (Problem Statement #4: Domain-Specific AI Copilot). A farmer asks a question by voice in their own language (e.g. Telugu) — *"It's been dry 10 days, what do I do with my cotton, and should I sell?"* — and gets a spoken, personalized answer combining **live weather, live mandi prices, and agronomy guidance**, plus an action (SMS/WhatsApp).

**Prototype deadline: 2 August 2026.** Grand finale (if shortlisted): Sept 1–4.

## The rules that decide the outcome (judging rubric)
- **Technical Execution 40%** — Snowflake **CoCo CLI must be the visible core** (data + reasoning + action). Deep use = custom skills, Cortex Search, MCP.
- **Real-World Relevance 30%** — one clear farmer, real money decisions.
- **Solution Completeness 30%** — one flow working end-to-end beats many half-flows.

**Golden rule:** CoCo CLI / Snowflake does the data + reasoning. Claude Code builds everything *around* it (loaders, backend, voice, frontend). Do NOT replace CoCo with your own logic — CoCo must be demonstrably central.

## Current state (already done — do not redo)
- **Snowflake account:** identifier `HE20264`, role `ACCOUNTADMIN`.
- **Warehouse** `KISAN_WH`, **database** `AGRI`, **schema** `AGRI.PUBLIC` — created.
- **Tables** (created + seeded with synthetic data, now going live):
  - `AGRI.PUBLIC.MANDI_PRICES(commodity,variety,state,district,market,min_price,max_price,modal_price,arrival_date)`
  - `AGRI.PUBLIC.WEATHER(district,date,temp_max,temp_min,rainfall_mm,humidity)`
  - `AGRI.PUBLIC.CROP_CALENDAR(crop,region,stage,stage_start_month,stage_end_month,water_need,notes)`
- **CoCo CLI** (`cortex`) installed and connected (SQL + Agent connection `HE20264`).
- **Custom CoCo skill `crop_advisory`** — being created; it queries the 3 tables and returns exactly three one-sentence recommendations (irrigation / sell-or-hold / pest watch) in the farmer's language + English translation. Verify it exists and works.
- **Live data loader:** `data/fetch_live_data.py` — pulls LIVE weather (Open-Meteo, no key) + LIVE mandi prices (data.gov.in Agmarknet feed, resource `9ef84268-d588-465a-a308-a864a43d0070`) and MERGEs into Snowflake. Idempotent (safe to re-run daily). VERIFIED 2026-07-18: live weather loaded for all 4 districts.
- **Daily auto-refresh (Snowflake-native, LIVE, ALL-INDIA):** `data/setup_daily_refresh.sql` (deploy via `data/deploy_daily_refresh.py [file.sql]`) — proc `AGRI.PUBLIC.REFRESH_LIVE_DATA()` fetches both APIs *from inside Snowflake* (external access integration `AGRI_APIS_INTEGRATION`), task `DAILY_REFRESH_TASK` runs it daily 18:30 IST. Mandi load is the FULL national Agmarknet snapshot (paginated 1000/page, deduped on merge key because feed splits by grade). data.gov.in key lives in Snowflake secret `AGRI.PUBLIC.DATA_GOV_KEY`. VERIFIED 2026-07-18: 6.5k+ rows, 18 states, 107 commodities; national run ~29s on X-Small.
- **On-demand weather for ANY district (LIVE):** `data/setup_on_demand_weather.sql` — proc `AGRI.PUBLIC.GET_DISTRICT_WEATHER('<district>')` geocodes via Open-Meteo (cache table `DISTRICT_GEO`), merges past-10-day observations into `WEATHER`, returns JSON with today + 7-day forecast. The 4 pilot districts (Warangal/Khammam/Karimnagar/Nizamabad) stay pre-loaded daily as fast path/demo fallback. VERIFIED 2026-07-18 with Anantapur (AP).
- **Agronomy corpus + Cortex Search (DONE 2026-07-18):** `data/setup_advisory_corpus.sql` — `AGRI.PUBLIC.ADVISORY_CHUNKS` (11 crops × 5 topics: cotton, paddy, chilli, maize, onion, tomato, potato, groundnut, red gram, soybean, wheat; topics sowing_seeds/fertilizer/irrigation/pests_diseases/harvest_market; dosages indicative, source-labeled) + Cortex Search service `AGRI.PUBLIC.ADVISORY_SEARCH` (ON content, ATTRIBUTES crop/topic/region, TARGET_LAG 1h). Retrieval verified. NOTE: earlier claims that this existed pre-2026-07-18 were wrong — always verify Snowflake objects exist before building on them.
- **Snowflake auth:** connection `HE20264` in `~/.snowflake/connections.toml` uses OAuth (account locator `bm13081.ap-southeast-2`), reused by Python via `connection_name="HE20264"`. No SF_PASSWORD needed locally. Quirk: first connect after token expiry fails with "Connection is closed" — just retry once.
- Scope: **ALL-INDIA** — prices national (paginated daily snapshot), weather on-demand via geocoding for any district; demo story stays focused on one Telangana farmer.

## Chosen stack
- **Data + reasoning:** Snowflake CoCo CLI + Cortex Search.
- **Voice:** Sarvam AI (STT/TTS, Hindi/Telugu/code-mixed) for the demo; Bhashini (govt, free) as the scale story.
- **Action:** WhatsApp/SMS via Twilio, ideally exposed to CoCo as an MCP tool.
- **App:** built with Claude Code — backend API + frontend (WhatsApp bot or web PWA).

## Secrets — NEVER commit
Keep all secrets in `.env` (git-ignored). Needed:
```
SF_ACCOUNT=HE20264
SF_USER=NIKKY001
SF_PASSWORD=...
DATA_GOV_KEY=...            # regenerate the leaked one first
SARVAM_KEY=...
TWILIO_SID=... TWILIO_TOKEN=...
```
First task if not present: create `.gitignore` with `.env`, `__pycache__/`, `*.pyc`, `.venv/`.

## Repo layout
```
kisan-copilot/
  CLAUDE.md
  data/        fetch_live_data.py, kisan_seed_data.sql
  coco/        custom CoCo skills (crop_advisory)
  backend/     API + Sarvam voice glue + MCP action
  frontend/    WhatsApp bot or web PWA
  demo/        script + assets
```

## Remaining phases (build in this order — one working flow first)
1. ~~**Live data pipeline**~~ DONE 2026-07-18 (national, Snowflake-native daily task).
2. ~~**Agronomy knowledge base**~~ DONE 2026-07-18 (`ADVISORY_CHUNKS` + `ADVISORY_SEARCH`, 11 crops).
3. **Generalize `crop_advisory`** — take (district, crop, question); use Cortex Search for agronomy answers; still return short, spoken-friendly advice in the farmer's language.
4. **Backend API** — FastAPI endpoint `/ask`: audio in → Sarvam STT → invoke the CoCo/Snowflake reasoning → Sarvam TTS → audio out. Keep the farmer's language end-to-end.
5. **Action** — send the advisory via WhatsApp/SMS (Twilio); expose as a CoCo MCP tool if possible.
6. **Frontend** — WhatsApp voice bot (preferred) or a simple web PWA with a mic button.
7. **Demo + submission** — record a 3–4 min video showing the voice moment AND CoCo doing real SQL + Cortex Search; write the submission emphasizing CoCo.

## Guardrails
- **Live Agmarknet naming differs from seed data:** commodities come as e.g. `Paddy(Common)`, `Paddy(Dhan)(Basmati)`; markets are real APMC names (`Sattupally APMC`), districts beyond the 4 pilot ones appear. The `crop_advisory` skill must match fuzzily (ILIKE '%paddy%' etc.), never by exact string.
- **data.gov.in blocks the default python-requests User-Agent** (silent timeout/502). Always send a custom UA header — already handled in both loaders; keep it if writing new fetch code.
- Prices + weather are **live (daily)**; agronomy knowledge is a **curated corpus** (doesn't change hourly) — don't try to make fertilizer advice "live."
- Keep a seeded-data fallback for the recorded demo in case live conditions don't tell a clear story that day.
- Always prefer Snowflake `MERGE` for idempotent loads.
- Verify each phase end-to-end before moving on.
