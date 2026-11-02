-- Daily snapshot fact: the price in force at the end of each day, per station and fuel.
-- Prices change on irregular dates, so the version history is expanded into one row per day
-- (end of the last day = the last snapshot in which the price was still listed).
DROP TABLE IF EXISTS analytics.fct_daily_station_price;
CREATE TABLE analytics.fct_daily_station_price AS
WITH end_of_day AS (  -- several changes on the same day -> keep the last one
    SELECT DISTINCT ON (station_id, fuel_type_id, price_updated_at::date)
        station_id,
        fuel_type_id,
        price_updated_at::date AS day,
        price_eur,
        last_seen_at
    FROM raw.price_history
    ORDER BY station_id, fuel_type_id, price_updated_at::date, price_updated_at DESC
),
ranges AS (
    SELECT
        station_id,
        fuel_type_id,
        price_eur,
        day AS from_day,
        COALESCE(
            LEAD(day) OVER (PARTITION BY station_id, fuel_type_id ORDER BY day) - 1,
            last_seen_at::date
        ) AS to_day
    FROM end_of_day
)
SELECT
    r.station_id,
    r.fuel_type_id,
    d::date     AS price_date,
    r.price_eur
FROM ranges r
CROSS JOIN LATERAL generate_series(r.from_day::timestamp, r.to_day::timestamp, interval '1 day') AS d
WHERE r.to_day >= r.from_day;

ALTER TABLE analytics.fct_daily_station_price ADD PRIMARY KEY (station_id, fuel_type_id, price_date);
CREATE INDEX ON analytics.fct_daily_station_price (fuel_type_id, price_date);
