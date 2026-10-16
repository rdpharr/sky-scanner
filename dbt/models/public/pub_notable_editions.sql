-- Notable-aircraft editions for the website: the verified edition, photos,
-- what the verifier removed and a list of the agent's tool calls (without
-- their results; every statement's cited values are in the edition itself).
select
    edition_id,
    week_start,
    week_end,
    generated_at,
    purpose,
    status,
    model,
    edition,
    photos,
    cast(json_value(verification, '$.checked') as int64) as statements_checked,
    cast(json_value(verification, '$.verified') as int64) as statements_verified,
    cast(json_value(verification, '$.verification_rate') as float64) as verification_rate,
    json_query(verification, '$.removed') as removed_statements,
    array(
        select as struct
            json_value(call, '$.call_id') as call_id,
            json_value(call, '$.tool') as tool,
            to_json_string(json_query(call, '$.arguments')) as arguments,
            cast(json_value(call, '$.ok') as bool) as ok
        from unnest(json_query_array(trace)) as call
    ) as tool_calls,
    tool_calls as tool_call_count,
    turns,
    prompt_tokens,
    completion_tokens,
    cost_usd,
    seconds
from {{ source('ai', 'notable_editions') }}
