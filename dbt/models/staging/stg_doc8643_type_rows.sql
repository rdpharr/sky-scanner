-- Every Doc 8643 row: one per manufacturer name a designator is built or sold
-- under (e.g. R22: Robinson, plus licence builders). dim_aircraft picks the row
-- matching the aircraft's own manufacturer.
select
    trim(Designator) as designator,
    trim(ManufacturerCode) as manufacturer_code,
    trim(ModelFullName) as model_full_name,
    _row_number
from {{ source('raw', 'doc8643_types') }}
where trim(Designator) != ''
