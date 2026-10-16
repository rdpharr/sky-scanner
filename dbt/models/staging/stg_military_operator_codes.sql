select
    code as operator_code,
    name as operator_name
from {{ source('raw', 'military_operator_codes') }}
