-- Daily price statistics at three geographic levels in one pass (GROUPING SETS):
-- national, district and municipality. The table a BI dashboard would point at.
DROP TABLE IF EXISTS analytics.agg_daily_prices;
CREATE TABLE analytics.agg_daily_prices AS
SELECT
    p.price_date,
    p.fuel_type_id,
    COALESCE(f.fuel_type, 'fuel ' || p.fuel_type_id)                        AS fuel_type,
    CASE
        WHEN GROUPING(s.municipality) = 0 THEN 'municipality'
        WHEN GROUPING(s.district) = 0     THEN 'district'
        ELSE 'national'
    END                                                                     AS geo_level,
    CASE WHEN GROUPING(s.district) = 1 THEN 'Portugal'
         ELSE COALESCE(s.district, '(unknown)') END                         AS district,
    CASE WHEN GROUPING(s.municipality) = 1 THEN '(all)'
         ELSE COALESCE(s.municipality, '(unknown)') END                     AS municipality,
    COUNT(*)                                                                AS stations,
    ROUND(AVG(p.price_eur), 3)                                              AS avg_price_eur,
    (percentile_cont(0.5) WITHIN GROUP (ORDER BY p.price_eur))::numeric(6,3) AS median_price_eur,
    MIN(p.price_eur)                                                        AS min_price_eur,
    MAX(p.price_eur)                                                        AS max_price_eur
FROM analytics.fct_daily_station_price p
JOIN analytics.dim_station s USING (station_id)
LEFT JOIN analytics.dim_fuel_type f USING (fuel_type_id)
GROUP BY GROUPING SETS (
    (p.price_date, p.fuel_type_id, f.fuel_type),
    (p.price_date, p.fuel_type_id, f.fuel_type, s.district),
    (p.price_date, p.fuel_type_id, f.fuel_type, s.district, s.municipality)
);

ALTER TABLE analytics.agg_daily_prices
    ADD PRIMARY KEY (price_date, fuel_type_id, district, municipality);
