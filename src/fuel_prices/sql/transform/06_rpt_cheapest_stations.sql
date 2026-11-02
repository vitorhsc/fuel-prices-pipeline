-- "Where is fuel cheapest right now?" - top 3 stations per municipality and fuel,
-- compared with the local and national averages. Only prices still listed by the API.
DROP TABLE IF EXISTS analytics.rpt_cheapest_stations;
CREATE TABLE analytics.rpt_cheapest_stations AS
WITH current_prices AS (
    SELECT station_id, fuel_type_id, price_eur, valid_from AS price_since
    FROM (
        SELECT c.*, max(c.last_seen_at) OVER (PARTITION BY c.fuel_type_id) AS latest_snapshot
        FROM analytics.fct_price_changes c
        WHERE c.is_current
    ) x
    WHERE last_seen_at >= latest_snapshot - interval '1 day'
),
ranked AS (
    SELECT
        s.district,
        s.municipality,
        cp.fuel_type_id,
        COALESCE(f.fuel_type, 'fuel ' || cp.fuel_type_id)                          AS fuel_type,
        DENSE_RANK() OVER local_market                                             AS price_rank,
        cp.station_id,
        s.station_name,
        s.brand,
        s.brand_segment,
        s.address,
        s.latitude,
        s.longitude,
        cp.price_eur,
        cp.price_since,
        ROUND(cp.price_eur - AVG(cp.price_eur) OVER local_area, 3)                  AS diff_vs_municipality_avg_eur,
        ROUND(cp.price_eur - AVG(cp.price_eur) OVER (PARTITION BY cp.fuel_type_id), 3) AS diff_vs_national_avg_eur,
        COUNT(*) OVER local_area                                                   AS stations_in_municipality
    FROM current_prices cp
    JOIN analytics.dim_station s USING (station_id)
    LEFT JOIN analytics.dim_fuel_type f USING (fuel_type_id)
    WINDOW
        local_area   AS (PARTITION BY s.district, s.municipality, cp.fuel_type_id),
        local_market AS (PARTITION BY s.district, s.municipality, cp.fuel_type_id ORDER BY cp.price_eur)
)
SELECT *, now() AS refreshed_at
FROM ranked
WHERE price_rank <= 3;

CREATE INDEX ON analytics.rpt_cheapest_stations (municipality, fuel_type_id);
