-- Fuel types with an analysis-friendly family and grade.
DROP TABLE IF EXISTS analytics.dim_fuel_type;
CREATE TABLE analytics.dim_fuel_type AS
SELECT
    fuel_type_id,
    fuel_type,
    unit,
    CASE
        WHEN fuel_type ILIKE 'Gasóleo%'  THEN 'Diesel'
        WHEN fuel_type ILIKE 'Gasolina%' THEN 'Petrol'
        WHEN fuel_type ILIKE 'GPL%'      THEN 'LPG'
        WHEN fuel_type ILIKE 'GN%'       THEN 'Natural gas'
        ELSE 'Other'
    END                                                       AS fuel_family,
    (fuel_type ILIKE '%especial%' OR fuel_type ILIKE '%98%')  AS is_premium_grade,
    is_road_fuel,
    is_active
FROM raw.fuel_types;

ALTER TABLE analytics.dim_fuel_type ADD PRIMARY KEY (fuel_type_id);
