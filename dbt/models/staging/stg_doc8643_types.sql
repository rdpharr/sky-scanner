-- The file lists one row per manufacturer name, so a designator can repeat.
-- Keep the first row in file order, as the aircraft log does.
select
    trim(Designator) as designator,
    trim(Description) as description,
    trim(WTC) as wake_category,
    trim(AircraftDescription) as aircraft_description,
    safe_cast(EngineCount as int64) as engine_count,
    trim(EngineType) as engine_type,
    trim(ManufacturerCode) as manufacturer_code,
    trim(ModelFullName) as model_full_name
from {{ source('raw', 'doc8643_types') }}
where trim(Designator) != ''
qualify row_number() over (
    partition by trim(Designator) order by _row_number) = 1
