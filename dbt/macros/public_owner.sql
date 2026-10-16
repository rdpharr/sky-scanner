{#- Owner name for the public layer, or null.
    FAA aircraft: only when the FAA registrant type is an organization
    (corporation, government, LLC, non-citizen corporation). Until the
    registrant type is imported for an aircraft, its owner stays hidden.
    Other aircraft: only when the name reads as an organization.
    Names mentioning a trust or trustee are hidden either way, because they
    often contain a person's name. -#}
{% macro public_owner(owner, owner_source, registrant_is_organization) -%}
    case
        when {{ owner }} is null then null
        when regexp_contains(upper({{ owner }}), r'\bTRUST(EE)?S?\b') then null
        when {{ owner_source }} = 'faa_registry' then
            if(coalesce({{ registrant_is_organization }}, false), {{ owner }}, null)
        when regexp_contains(upper({{ owner }}),
            r'\b(AIR|AIRLINES?|AIRWAYS|AVIATION|AERO|AIRCRAFT|JET|JETS|LTD|LLC|INC|CORP|CORPORATION|COMPANY|GMBH|AG|SA|SAS|SPA|PLC|LIMITED|BANK|LEASING|CAPITAL|HOLDINGS?|GROUP|FORCE|NAVY|ARMY|GOVERNMENT|MINISTRY|DEPARTMENT|POLICE|SHERIFF|COUNTY|UNIVERSITY|COLLEGE|SCHOOL|SERVICES?|CARGO|EXPRESS)\b')
            then {{ owner }}
    end
{%- endmacro %}
