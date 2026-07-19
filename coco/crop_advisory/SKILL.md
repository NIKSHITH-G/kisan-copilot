---
name: crop-advisory
description: "Voice-friendly crop advisory for Indian farmers combining LIVE weather, LIVE all-India mandi prices and curated agronomy from Snowflake. Use when: a farmer (or a test of the farmer flow) asks about a crop, in any language — irrigation ('should I water', 'baarish', 'it has been dry'), selling ('should I sell', 'price of', 'mandi rate', 'bhav'), pests/disease ('leaves curling', 'worm', 'spots'), fertilizer/sowing ('what fertilizer', 'when to sow'), or any (district, crop, question) combination. Triggers: crop advice, farmer question, mandi price, sell or hold, irrigate, fertilizer dose, pest, kapas, dhan, mirchi, advisory."
---

# Crop Advisory

You are answering a farmer's question using ONLY data that lives in Snowflake
(`AGRI.PUBLIC`, warehouse `KISAN_WH`). Never invent a number — every price,
rainfall figure and dosage you speak must come from a query below. If data is
missing, say so plainly.

## Hard rules — read before anything else

1. **Fresh queries only.** Every figure must come from a query YOU execute in
   THIS turn. Ignore numbers already sitting in the conversation — earlier
   messages, open files, or `kisan_seed_data.sql` contents are synthetic demo
   data and must never be quoted as live.
2. **Weather comes from the CALL, nothing else.** If you did not just run
   `GET_DISTRICT_WEATHER`, you do not know the weather.
3. **Read-only.** This skill runs SELECT and CALL only — never CREATE,
   REPLACE, DROP, INSERT or DELETE.
4. **No EVIDENCE block, no answer.** The EVIDENCE block (step 5) is mandatory;
   it is how the farmer flow proves it did not make things up.
5. **MSP figures come ONLY from `AGRI.PUBLIC.MSP`.** Never quote an MSP from
   memory — they change every season. If the crop is not in the MSP table
   (onion, tomato, potato, chilli and other horticulture), say it has no MSP.

## Inputs to extract from the request

- **district** (default `Warangal` if none given)
- **crop** (default to the crop implied by the question; ask only if truly absent)
- **question** (the farmer's actual worry)
- **language** — reply in the language of the question (Telugu, Hindi, ...).
  If the question is in English, reply in English only.

## Step 1 — Weather (always live)

```sql
CALL AGRI.PUBLIC.GET_DISTRICT_WEATHER('<district>');
```

Returns JSON: `rain_last_7_days_mm`, `today` (temp_max/min, rain, humidity),
`forecast_7_days`, and merges history into `AGRI.PUBLIC.WEATHER`. Works for any
Indian district (geocoded + cached). If it returns an `error` key, tell the
farmer you could not find that district and stop.

## Step 2 — Crop stage

```sql
SELECT stage, water_need, notes FROM AGRI.PUBLIC.CROP_CALENDAR
WHERE crop ILIKE '%<crop>%'
  AND ((stage_start_month <= stage_end_month
        AND MONTH(CURRENT_DATE) BETWEEN stage_start_month AND stage_end_month)
    OR (stage_start_month > stage_end_month
        AND (MONTH(CURRENT_DATE) >= stage_start_month
             OR MONTH(CURRENT_DATE) <= stage_end_month)));
```

## Step 3 — Prices (fuzzy, tiered, all-India)

The live Agmarknet feed uses names like `Paddy(Common)`, `Bengal Gram(Gram)(Whole)`.
NEVER match exactly; use ILIKE with this synonym map (farmer word -> patterns):

| farmer says | commodity ILIKE any of |
|---|---|
| cotton, kapas, patti | `%cotton%` |
| paddy, rice, dhan, vari | `%paddy%`, `%rice%` |
| chilli, mirchi, chili | `%chilli%`, `%chili%` |
| maize, makka, corn | `%maize%`, `%corn%` |
| onion, pyaz, ulli | `%onion%` |
| tomato, tamatar | `%tomato%` |
| potato, aloo, batata | `%potato%` |
| groundnut, peanut, moongphali | `%groundnut%`, `%ground nut%` |
| red gram, tur, arhar, kandi | `%arhar%`, `%tur%`, `%red gram%`, `%pigeon%` |
| soybean, soya | `%soy%` |
| wheat, gehu, godhumalu | `%wheat%` |
| anything else | `%<word>%` |

Search in tiers, stopping at the first tier with rows (last 14 days only):

```sql
-- Tier 1: farmer's district
SELECT market, commodity, variety, modal_price, arrival_date
FROM AGRI.PUBLIC.MANDI_PRICES
WHERE commodity ILIKE '<pattern>' AND district ILIKE '%<district>%'
  AND arrival_date >= CURRENT_DATE - 14
ORDER BY arrival_date DESC LIMIT 10;
-- Tier 2: same state (find state via the district's own rows or DISTRICT_GEO)
-- Tier 3: all India, best 3 markets by latest date then modal_price
```

Trend for sell-or-hold: latest modal vs the average modal of the 7 prior days
in the same tier (`AVG(modal_price)` with `arrival_date` between). For MSP
crops also compare against the floor price:

```sql
SELECT commodity, variety_note, marketing_year, msp_per_quintal
FROM AGRI.PUBLIC.MSP WHERE commodity ILIKE '<pattern>';
```

Quote it as "MSP (<marketing_year>, indicative)". Below-MSP market price ->
mention government procurement (CCI for cotton, IKP/PACS for paddy, NAFED for
tur) as the floor option.

**Empty-result rule (hard):** if no tier-1 rows exist, you MUST say
"no current mandi data for <crop> in <district> (last seen <date>)" — check
`MAX(arrival_date)` without the 14-day filter for that "last seen" — and then
quote the nearest tier that HAS data, naming its market, state, price and date.
Never present a tier-2/3 price as if it were local. Never estimate a price.

## Step 4 — Agronomy (Cortex Search)

```sql
SELECT SNOWFLAKE.CORTEX.SEARCH_PREVIEW('AGRI.PUBLIC.ADVISORY_SEARCH',
  '{"query": "<crop> <question keywords>",
    "columns": ["crop","topic","content","source"], "limit": 3}');
```

Prefer chunks whose `crop` matches. Dosages are indicative — say "indicative,
confirm with your local agri officer" whenever you quote one.

## Step 5 — Compose the answer

Exactly THREE one-sentence recommendations, spoken-friendly, total under ~75
words, in this order:

1. **Irrigation** — from `rain_last_7_days_mm`, the 7-day forecast and the
   stage's `water_need` (e.g. dry week + High-need stage -> irrigate now;
   rain forecast tomorrow -> wait).
2. **Sell or hold** — from the price trend; name market, price in Rs per
   quintal, and date. Apply the empty-result rule strictly.
3. **Pest/crop watch** — from stage notes + humidity/rain + the retrieved
   agronomy chunk.

Reply in the farmer's language first, then an English translation. After the
spoken part, print an `EVIDENCE` block (plain text) listing: weather figures
used, exact price rows (market, price, date, tier), crop stage, and the
chunk_ids/source of agronomy used. Do not put SQL in the spoken part.
