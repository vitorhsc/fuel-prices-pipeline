-- Price history as a slowly changing fact (SCD type 2 style): one row per price version,
-- with the validity interval and the change versus the previous version.
DROP TABLE IF EXISTS analytics.fct_price_changes;
CREATE TABLE analytics.fct_price_changes AS
SELECT
    station_id,
    fuel_type_id,
    price_updated_at                                          AS valid_from,
    LEAD(price_updated_at) OVER w                             AS valid_to,
    LEAD(price_updated_at) OVER w IS NULL                     AS is_current,
    LAG(price_eur) OVER w IS NULL                             AS is_first_observation,
    price_eur,
    LAG(price_eur) OVER w                                     AS previous_price_eur,
    price_eur - LAG(price_eur) OVER w                         AS price_change_eur,
    ROUND(100 * (price_eur / NULLIF(LAG(price_eur) OVER w, 0) - 1), 2)
                                                              AS price_change_pct,
    price_updated_at::date                                    AS change_date,
    EXTRACT(ISODOW FROM price_updated_at)::int                AS change_weekday,  -- 1 = Monday
    first_seen_at,
    last_seen_at,
    -- how long it took the pipeline to notice a change (meaningless for the first observation)
    CASE WHEN LAG(price_eur) OVER w IS NOT NULL
         THEN ROUND((EXTRACT(EPOCH FROM first_seen_at - price_updated_at) / 3600.0)::numeric, 1)
    END                                                       AS capture_lag_hours
FROM raw.price_history
WINDOW w AS (PARTITION BY station_id, fuel_type_id ORDER BY price_updated_at);

ALTER TABLE analytics.fct_price_changes ADD PRIMARY KEY (station_id, fuel_type_id, valid_from);
CREATE INDEX ON analytics.fct_price_changes (fuel_type_id, change_date);
