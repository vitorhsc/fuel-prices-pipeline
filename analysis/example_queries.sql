-- Example analyses on top of the analytics layer.
-- Run with `make psql`, or from any SQL client / BI tool connected to localhost:5432.
-- Fuel ids: 2101 Gasóleo simples · 3201 Gasolina simples 95 · 1120 GPL Auto (see `make lookups`)

-- 1. Where is diesel cheapest in my municipality right now?
SELECT price_rank, station_name, brand, address, price_eur, price_since,
       diff_vs_municipality_avg_eur, diff_vs_national_avg_eur
FROM analytics.rpt_cheapest_stations
WHERE municipality = 'Barcelos' AND fuel_type_id = 2101
ORDER BY price_rank;

-- 2. National average price per day and fuel (last 30 days)
SELECT price_date, fuel_type, avg_price_eur, median_price_eur, stations
FROM analytics.agg_daily_prices
WHERE geo_level = 'national' AND price_date >= current_date - 30
ORDER BY fuel_type, price_date;

-- 3. Cheapest and most expensive districts today (diesel)
SELECT district, avg_price_eur, min_price_eur, max_price_eur, stations,
       RANK() OVER (ORDER BY avg_price_eur) AS rank_cheapest
FROM analytics.agg_daily_prices
WHERE geo_level = 'district' AND fuel_type_id = 2101 AND price_date = current_date
ORDER BY avg_price_eur;

-- 4. How much can you save by choosing the right station? (spread per municipality today)
SELECT district, municipality, stations, min_price_eur, max_price_eur,
       max_price_eur - min_price_eur                 AS spread_eur,
       ROUND(50 * (max_price_eur - min_price_eur), 2) AS saving_on_50_litres_eur
FROM analytics.agg_daily_prices
WHERE geo_level = 'municipality' AND fuel_type_id = 2101 AND price_date = current_date
  AND stations >= 5
ORDER BY spread_eur DESC
LIMIT 15;

-- 5. Hypermarkets vs. major oil companies vs. independents (last 7 days, diesel)
WITH daily AS (
    SELECT p.price_date, s.brand_segment, p.price_eur,
           AVG(p.price_eur) OVER (PARTITION BY p.price_date) AS national_avg
    FROM analytics.fct_daily_station_price p
    JOIN analytics.dim_station s USING (station_id)
    WHERE p.fuel_type_id = 2101 AND p.price_date >= current_date - 7
)
SELECT brand_segment,
       ROUND(AVG(price_eur), 3)                               AS avg_price_eur,
       ROUND(100 * (AVG(price_eur) / AVG(national_avg) - 1), 2) AS pct_vs_national_avg
FROM daily
GROUP BY brand_segment
ORDER BY avg_price_eur;

-- 6. Brand price index: how far above/below the market is each brand? (brands with 20+ stations)
WITH current_diesel AS (
    SELECT s.brand, c.price_eur
    FROM analytics.fct_price_changes c
    JOIN analytics.dim_station s USING (station_id)
    WHERE c.is_current AND c.fuel_type_id = 2101 AND s.is_active
)
SELECT brand, COUNT(*) AS stations, ROUND(AVG(price_eur), 3) AS avg_price_eur,
       ROUND(100 * (AVG(price_eur) / (SELECT AVG(price_eur) FROM current_diesel) - 1), 2)
           AS pct_vs_market
FROM current_diesel
GROUP BY brand
HAVING COUNT(*) >= 20
ORDER BY pct_vs_market;

-- 7. Do motorway stations charge more?
SELECT s.is_motorway, f.fuel_type, COUNT(*) AS stations, ROUND(AVG(c.price_eur), 3) AS avg_price_eur
FROM analytics.fct_price_changes c
JOIN analytics.dim_station s USING (station_id)
JOIN analytics.dim_fuel_type f USING (fuel_type_id)
WHERE c.is_current AND s.is_active
GROUP BY 1, 2
ORDER BY 2, 1;

-- 8. On which weekday do stations change prices? (Portuguese prices usually move on Mondays)
SELECT change_weekday,
       to_char(date '2024-01-01' + change_weekday - 1, 'Day') AS weekday,
       COUNT(*)                                               AS price_changes,
       ROUND(AVG(price_change_eur), 4)                        AS avg_change_eur
FROM analytics.fct_price_changes
WHERE NOT is_first_observation
GROUP BY change_weekday
ORDER BY change_weekday;

-- 9. Biggest price increases in the last 7 days
SELECT c.valid_from, s.station_name, s.municipality, f.fuel_type,
       c.previous_price_eur, c.price_eur, c.price_change_eur, c.price_change_pct
FROM analytics.fct_price_changes c
JOIN analytics.dim_station s USING (station_id)
JOIN analytics.dim_fuel_type f USING (fuel_type_id)
WHERE NOT c.is_first_observation AND c.valid_from >= now() - interval '7 days'
ORDER BY c.price_change_eur DESC
LIMIT 20;

-- 10. Pipeline observability: last run per task, data quality and capture latency
SELECT DISTINCT ON (dataset, scope)
       dataset, scope, status, finished_at, rows_extracted, rows_rejected, new_versions,
       api_requests, high_water_after, warning_message, error_message
FROM meta.pipeline_runs
ORDER BY dataset, scope, finished_at DESC;

SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY capture_lag_hours) AS p50_capture_lag_hours,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY capture_lag_hours) AS p90_capture_lag_hours
FROM analytics.fct_price_changes
WHERE capture_lag_hours IS NOT NULL;
