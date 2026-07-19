-- Top up CROP_CALENDAR for the full 11-crop scope (cotton/chilli/paddy were
-- seeded already). Idempotent MERGE on (crop, region, stage).
-- Deploy with: .venv/bin/python data/deploy_daily_refresh.py data/setup_crop_calendar.sql
--
-- NOTE for consumers: some stages wrap the year (e.g. 12 -> 1). Month-in-stage
-- SQL must be:  (start <= end AND m BETWEEN start AND end)
--            OR (start > end AND (m >= start OR m <= end))

USE ROLE ACCOUNTADMIN;
USE WAREHOUSE KISAN_WH;
USE SCHEMA AGRI.PUBLIC;

MERGE INTO AGRI.PUBLIC.CROP_CALENDAR t
USING (SELECT * FROM VALUES
('Maize','Telangana','Sowing',6,7,'Moderate','Sow with monsoon; pre-emergence herbicide keeps first 30 days weed free.'),
('Maize','Telangana','Vegetative',7,8,'High','Scout whorls for fall armyworm from 10 days after emergence.'),
('Maize','Telangana','Tasseling-Silking',8,9,'High','Critical water fortnight; stress here can halve yield.'),
('Maize','Telangana','Grain Fill-Harvest',9,11,'Moderate','Harvest at black layer, about 20 percent moisture; dry to 14.'),
('Onion','Deccan rabi','Nursery',10,11,'Moderate','Raised beds; fresh seed only, onion seed dies within a year.'),
('Onion','Deccan rabi','Transplanting',12,1,'Moderate','Transplant 6 to 8 week seedlings at 15 x 10 cm.'),
('Onion','Deccan rabi','Bulb Development',1,3,'High','Light frequent irrigation; stop all nitrogen; watch thrips.'),
('Onion','Deccan rabi','Harvest-Curing',3,4,'Low','Stop irrigation 10-15 days before harvest; cure until necks dry.'),
('Tomato','Telangana','Nursery',6,7,'Moderate','Nursery under nylon net keeps early leaf curl out.'),
('Tomato','Telangana','Transplanting',7,8,'Moderate','Stake or trellis early; 60 x 45 cm.'),
('Tomato','Telangana','Flowering-Fruit Set',8,9,'High','Even moisture; swings cause flower drop and fruit cracking.'),
('Tomato','Telangana','Harvest',9,11,'Moderate','Pick breaker stage for distant markets; grade before sale.'),
('Potato','Deccan rabi','Planting',10,11,'Moderate','Certified sprouted seed tubers on ridges; cool nights needed.'),
('Potato','Deccan rabi','Tuber Initiation',11,12,'High','Keep ridges moist; earthing up with nitrogen top dress at 30 days.'),
('Potato','Deccan rabi','Tuber Bulking',12,2,'High','Water stress now means small tubers; watch late blight in cloudy spells.'),
('Potato','Deccan rabi','Harvest',2,3,'Low','Cut haulms 10 days before digging so skins set.'),
('Groundnut','Telangana','Sowing',6,7,'Moderate','Treat kernels with rhizobium; sow with monsoon.'),
('Groundnut','Telangana','Flowering-Pegging',7,9,'High','Apply gypsum at flowering; keep pegging zone loose and moist.'),
('Groundnut','Telangana','Pod Development',9,10,'High','Protective irrigation if 10 day dry spell; pods filling now.'),
('Groundnut','Telangana','Harvest',10,11,'Low','Dig when inner shell shows dark veins; dry thin to avoid aflatoxin.'),
('Red Gram','Telangana','Sowing',6,7,'Moderate','Sow with first good rains; wide rows allow intercrop.'),
('Red Gram','Telangana','Vegetative',7,9,'Low','Rainfed; drain heavy rain fast, waterlogging kills in a day.'),
('Red Gram','Telangana','Flowering',10,11,'High','Protective irrigation pays most now; pod borer traps from flower start.'),
('Red Gram','Telangana','Pod Fill-Harvest',11,1,'Moderate','Harvest at 80 percent brown pods; NAFED procures at MSP.'),
('Soybean','MP-Maharashtra','Sowing',6,7,'Moderate','Sow after 100 mm cumulative rain; handle seed gently.'),
('Soybean','MP-Maharashtra','Vegetative',7,8,'Low','Drainage is the task; standing water yellows patches.'),
('Soybean','MP-Maharashtra','Pod Fill',8,9,'High','One protective irrigation if monsoon breaks 2 weeks.'),
('Soybean','MP-Maharashtra','Harvest',9,10,'Low','Harvest when pods rattle at 13-14 percent moisture.'),
('Wheat','North India','Sowing',11,11,'Moderate','Sow 1-20 November; each late day costs about 1 percent yield.'),
('Wheat','North India','Crown Root Initiation',11,12,'High','The 21 day irrigation is make or break; top dress N with it.'),
('Wheat','North India','Tillering-Flowering',12,2,'High','Water at tillering, jointing, flowering; watch yellow rust in cool cloud.'),
('Wheat','North India','Grain Fill-Harvest',2,4,'Moderate','Avoid irrigating in strong wind; combine at 12-14 percent moisture.')
  AS v(crop, region, stage, stage_start_month, stage_end_month, water_need, notes)) s
ON t.crop = s.crop AND t.region = s.region AND t.stage = s.stage
WHEN MATCHED THEN UPDATE SET
  t.stage_start_month = s.stage_start_month, t.stage_end_month = s.stage_end_month,
  t.water_need = s.water_need, t.notes = s.notes
WHEN NOT MATCHED THEN INSERT (crop, region, stage, stage_start_month, stage_end_month, water_need, notes)
  VALUES (s.crop, s.region, s.stage, s.stage_start_month, s.stage_end_month, s.water_need, s.notes);
