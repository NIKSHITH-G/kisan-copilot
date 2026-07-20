# Kisan Copilot — demo video script (target 3:30–3:45)

**Pitch line (memorize):** "A cotton farmer in Warangal asks one question in Telugu —
and Snowflake answers with live weather, live mandi prices and agronomy, in his
language, with evidence, and can WhatsApp him the advice. CoCo is the brain."

## Screen setup (before recording)

| Window | Contents |
|---|---|
| A | Terminal, **fresh** `cortex` session in the repo (never resume an old one) |
| B | Browser: `http://127.0.0.1:8400/` (mic page, permission already granted) |
| C | Snowsight worksheet (queries pre-typed, see Scene 4) |
| D (optional) | Phone with WhatsApp visible (only if Twilio is live) |

## Timeline

**0:00–0:25 — The farmer and the money.** Face to camera or slide: "70% of Indian
farmers decide irrigation and selling on hearsay. Ravi grows cotton in Warangal.
Water too early, he wastes a pump-day; sell a week early, he loses thousands of
rupees. He doesn't read dashboards. He talks."

**0:25–1:15 — The voice moment (window B).** Tap mic, ask in Telugu:
"పది రోజులుగా వాన లేదు, నా పత్తి పంటకి నీళ్లు పెట్టాలా? అమ్మాలా వద్దా?"
Let the answer PLAY OUT LOUD. Point at the screen while it plays:
"It heard Telugu, found his crop, and said: don't irrigate — 27mm fell this week,
more coming; hold your cotton — ₹7,949 and rising. Every number came from
Snowflake — there's the EVIDENCE panel." Expand EVIDENCE briefly.

**1:15–2:15 — CoCo is the brain (window A).** Say: "That app is a thin shell.
The reasoning is Snowflake's Cortex agent — here it is raw." In the fresh cortex
session ask: `What is the apple price in Warangal market today?`
While it runs, narrate the SQL_EXECUTE steps appearing: "Watch it — our
crop-advisory skill: live weather proc, tiered price SQL, Cortex Search on the
agronomy corpus." When the answer lands, highlight: "**No apple data in Warangal —
it says so.** It never invents a price; it names the nearest real market instead.
That guardrail is written into the skill."

**2:15–2:50 — The action (window A, continue same session).** Say:
`Send that advisory to +91XXXXXXXXXX on WhatsApp` — CoCo calls the `send_advisory`
MCP tool (server `kisan-actions`). Show the tool call + payload on screen
(and the phone buzzing if Twilio is live). "Data, reasoning, action — all through
CoCo."

**2:50–3:25 — It runs itself (window C).** Run the two pre-typed queries:
```sql
SELECT * FROM AGRI.PUBLIC.REFRESH_LOG ORDER BY run_at DESC LIMIT 5;
SELECT COUNT(*), COUNT(DISTINCT state), MAX(arrival_date) FROM AGRI.PUBLIC.MANDI_PRICES;
```
"Snowflake refreshes itself — twice a day a task pulls the full national Agmarknet
snapshot and Open-Meteo weather from inside Snowflake, ~8,000 mandi rows across
20+ states, no server anywhere. Weather works for any district in India — it
geocodes on demand." (Optionally: `CALL GET_DISTRICT_WEATHER('Anantapur');`)

**3:25–3:45 — Close.** "One farmer, one question, one honest answer — built on
CoCo CLI, Cortex Search, Cortex COMPLETE and MCP. Scales to every district and
22 languages via Bhashini. Thank you."

## Pre-flight checklist (morning of recording)

- [ ] `SELECT * FROM REFRESH_LOG ORDER BY run_at DESC LIMIT 2` — last run has mandi rows (if not: `CALL REFRESH_LIVE_DATA();`)
- [ ] Backend up: `.venv/bin/uvicorn backend.app:app --port 8400` → `/health` all true
- [ ] Mic permission already granted in the recording browser (no popup on camera)
- [ ] `cortex` session is FRESH (skill loads, EVIDENCE block appears; no EVIDENCE = restart session)
- [ ] If phone shot wanted: mic needs HTTPS on phones — `brew install cloudflared && cloudflared tunnel --url http://localhost:8400`, use the printed URL
- [ ] Twilio live? `.env` has SID/TOKEN + phone joined sandbox; else say "dry-run mode" honestly
- [ ] System volume up; screen recorder captures system audio

## If live data tells no story that day (fallback — use only if truly needed)

1. `.venv/bin/python data/deploy_daily_refresh.py data/kisan_seed_data.sql` (DESTRUCTIVE — resets 3 tables to the scripted dry-spell demo world)
2. Record the demo.
3. Restore live: `CALL AGRI.PUBLIC.REFRESH_LIVE_DATA();` then redeploy `data/setup_crop_calendar.sql`.
