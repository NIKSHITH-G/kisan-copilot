-- Minimum Support Price reference table (indicative — MSPs are announced by
-- GoI each season; verify against the latest CACP notification before demo).
-- Only MSP-notified crops appear here; horticulture (onion, tomato, potato,
-- chilli) has NO MSP and must be reported as such.
-- Deploy with: .venv/bin/python data/deploy_daily_refresh.py data/setup_msp.sql

USE ROLE ACCOUNTADMIN;
USE WAREHOUSE KISAN_WH;
USE SCHEMA AGRI.PUBLIC;

CREATE TABLE IF NOT EXISTS AGRI.PUBLIC.MSP (
  commodity STRING,
  variety_note STRING,
  season STRING,           -- 'Kharif' | 'Rabi'
  marketing_year STRING,
  msp_per_quintal NUMBER,
  source STRING
);

MERGE INTO AGRI.PUBLIC.MSP t
USING (SELECT * FROM VALUES
('Paddy','Common','Kharif','2025-26',2369,'CACP/GoI (indicative - verify current season)'),
('Paddy','Grade A','Kharif','2025-26',2389,'CACP/GoI (indicative - verify current season)'),
('Cotton','Medium Staple','Kharif','2025-26',7710,'CACP/GoI (indicative - verify current season)'),
('Cotton','Long Staple','Kharif','2025-26',8110,'CACP/GoI (indicative - verify current season)'),
('Maize','-','Kharif','2025-26',2400,'CACP/GoI (indicative - verify current season)'),
('Red Gram (Tur/Arhar)','-','Kharif','2025-26',8000,'CACP/GoI (indicative - verify current season)'),
('Groundnut','-','Kharif','2025-26',7263,'CACP/GoI (indicative - verify current season)'),
('Soybean','Yellow','Kharif','2025-26',5328,'CACP/GoI (indicative - verify current season)'),
('Wheat','-','Rabi','2025-26',2425,'CACP/GoI (indicative - verify current season)')
  AS v(commodity, variety_note, season, marketing_year, msp_per_quintal, source)) s
ON t.commodity = s.commodity AND t.variety_note = s.variety_note AND t.marketing_year = s.marketing_year
WHEN MATCHED THEN UPDATE SET t.season = s.season,
  t.msp_per_quintal = s.msp_per_quintal, t.source = s.source
WHEN NOT MATCHED THEN INSERT (commodity, variety_note, season, marketing_year, msp_per_quintal, source)
  VALUES (s.commodity, s.variety_note, s.season, s.marketing_year, s.msp_per_quintal, s.source);
