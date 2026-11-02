-- Current attributes of every station ever seen, with a market segment and activity flag.
DROP TABLE IF EXISTS analytics.dim_station;
CREATE TABLE analytics.dim_station AS
WITH latest AS (
    SELECT max(last_seen_at) AS latest_snapshot FROM raw.stations
)
SELECT
    s.station_id,
    s.station_name,
    s.brand,
    -- Segment heuristic (documented assumption): hypermarket forecourts, the big oil
    -- companies, and everyone else (independent / low-cost / unbranded).
    CASE
        WHEN s.station_type ILIKE '%hipermercado%'
          OR upper(s.brand) IN ('INTERMARCHÉ', 'INTERMARCHE', 'AUCHAN', 'E.LECLERC', 'LECLERC',
                                'PINGO DOCE', 'CONTINENTE', 'ALDI', 'LIDL')  THEN 'Hypermarket'
        WHEN upper(s.brand) IN ('GALP', 'BP', 'REPSOL', 'CEPSA', 'MOEVE', 'SHELL')
                                                                          THEN 'Major oil company'
        ELSE 'Independent / low-cost'
    END                                                        AS brand_segment,
    s.station_type,
    s.station_type = 'Auto-estrada'                            AS is_motorway,
    s.district,
    s.municipality,
    s.locality,
    s.address,
    s.postal_code,
    s.latitude,
    s.longitude,
    s.first_seen_at,
    s.last_seen_at,
    s.last_seen_at >= l.latest_snapshot - interval '7 days'    AS is_active
FROM raw.stations s
CROSS JOIN latest l;

ALTER TABLE analytics.dim_station ADD PRIMARY KEY (station_id);
CREATE INDEX ON analytics.dim_station (district, municipality);
