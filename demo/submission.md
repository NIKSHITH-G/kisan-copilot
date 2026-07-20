# Kisan Copilot — voice crop advisory with Snowflake CoCo at the core

**Problem Statement #4: Domain-Specific AI Copilot · Snowflake CoCo CLI Hackathon 2026**

A farmer in Warangal asks, by voice, in Telugu: *"It hasn't rained in ten days —
should I water my cotton? Should I sell?"* Kisan Copilot answers out loud in
Telugu: don't irrigate (27mm fell this week, more forecast), hold your cotton
(₹7,949 and rising, still under the ₹8,110 MSP), and check leaf undersides for
sucking pests — with an EVIDENCE block proving every number came from Snowflake.
It can then WhatsApp him that advice — triggered by CoCo itself.

## Why it matters (Real-World Relevance)

Irrigation and sell-or-hold are the two highest-stakes recurring decisions for
120M+ Indian smallholders. Both are made on hearsay today. The information
exists — Agmarknet prices, weather, agronomy packages of practices — but not in
one place, not current, and never in the farmer's language or medium (voice).
Getting either decision right once pays for a season of this service.

## CoCo / Snowflake is the visible core (Technical Execution)

| Layer | Where it lives |
|---|---|
| **Data** | `AGRI.PUBLIC`: MANDI_PRICES (full national Agmarknet snapshot, MERGEd twice daily by task `DAILY_REFRESH_TASK` **from inside Snowflake** via External Access Integration; API key in a Snowflake SECRET), WEATHER (4 pilot districts daily + `GET_DISTRICT_WEATHER('<any district>')` geocodes on demand, cache table), CROP_CALENDAR (11 crops, stage-aware), MSP (only permitted source of MSP figures), REFRESH_LOG, 90-day retention task |
| **Knowledge** | ADVISORY_CHUNKS (11 crops × 5 topics, PJTSAU/ICAR-derived, dosages labeled indicative) retrieved through **Cortex Search** service ADVISORY_SEARCH |
| **Reasoning (interactive)** | Custom CoCo skill **`crop-advisory`**: live weather call, tiered fuzzy price SQL (district→state→India, synonym map for real feed names like `Paddy(Common)`), Cortex Search retrieval, hard rules — fresh queries only, never invent a price, MSP only from the MSP table, mandatory EVIDENCE block |
| **Reasoning (app path)** | Stored proc **`ANSWER_FARMER`**: same data gathering, decisions computed deterministically in-proc (irrigate / sell-hold / pest watch), **Cortex COMPLETE (llama3.3-70b)** only phrases them |
| **Action** | MCP server **`kisan-actions`** registered with CoCo — the agent itself calls `send_advisory` to WhatsApp/SMS the farmer (Twilio) |

The app layer is deliberately thin: FastAPI + Sarvam AI (STT saarika, translate,
TTS bulbul) + one static HTML mic page. Remove the app and the whole copilot
still works inside the `cortex` TUI.

## One flow, end-to-end (Solution Completeness)

Voice (Telugu) → Sarvam STT → Snowflake `ANSWER_FARMER` (live weather + live
prices + MSP + Cortex Search + Cortex COMPLETE) → Sarvam translate + TTS →
spoken Telugu answer + EVIDENCE → optional WhatsApp send via CoCo MCP tool.
Every hop verified, including the honesty path: ask for apple prices in Warangal
and it answers "no current mandi data for apple in Warangal" and names the
nearest real market — it never fabricates a number.

## Architecture

```
farmer voice ──▶ Sarvam STT ──▶ FastAPI /ask ──▶ CALL AGRI.PUBLIC.ANSWER_FARMER
                                                    │  GET_DISTRICT_WEATHER (Open-Meteo, in-Snowflake)
                                                    │  tiered price SQL + MSP table
                                                    │  Cortex Search (ADVISORY_SEARCH)
                                                    │  deterministic hints → Cortex COMPLETE
   spoken answer ◀── Sarvam TTS ◀── translate ◀────┘  + EVIDENCE json
   WhatsApp/SMS ◀── Twilio ◀── MCP tool send_advisory ◀── CoCo agent (crop-advisory skill)
   daily: DAILY_REFRESH_TASK (13:30+18:30 IST) → REFRESH_LIVE_DATA() → national Agmarknet + weather → REFRESH_LOG → PRUNE_MANDI_TASK
```

## Honesty notes

- Fertilizer/pesticide dosages and MSP figures are labeled **indicative** and
  source-tagged; the skill refuses to quote an MSP not present in the MSP table.
- Prices are the live national Agmarknet feed. Where a commodity is out of
  season locally (cotton arrivals start ~October), a clearly identifiable
  synthetic series backstops the demo narrative for the pilot district; it ages
  out automatically via the 90-day retention task.
- Scale story: weather already works for any Indian district (on-demand
  geocoding); prices are all-India; languages scale via Bhashini (govt, free)
  with Sarvam powering the demo.

## Run it

```bash
cp .env.example .env            # add SARVAM_KEY (+ TWILIO_* to send for real)
.venv/bin/pip install -r backend/requirements.txt
.venv/bin/uvicorn backend.app:app --port 8400    # open http://127.0.0.1:8400
cortex                          # fresh session: ask a farmer question; skill crop-advisory loads
```
Snowflake objects deploy from `data/setup_*.sql` via `data/deploy_daily_refresh.py <file>`.
