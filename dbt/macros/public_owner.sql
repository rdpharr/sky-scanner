{#- Owner name for the public layer, or null.
    When the FAA has classified the registrant, its classification decides,
    whichever source supplied the name: only corporation, government, LLC or
    non-citizen corporation registrants are named. (Co-owned registrations
    usually have no FAA name, and a crowdsourced source may supply a
    person's name for them.) An FAA owner without a registrant type stays
    hidden. Aircraft the FAA does not know: named only when the name reads as
    an organization. Names mentioning a trust or trustee are always hidden,
    because they often contain a person's name. -#}
{% macro public_owner(owner, owner_source, registrant_is_organization, registrant_type) -%}
    case
        when {{ owner }} is null then null
        when regexp_contains(upper({{ owner }}), r'\bTRUST(EE)?S?\b') then null
        when {{ registrant_type }} is not null then
            if(coalesce({{ registrant_is_organization }}, false), {{ owner }}, null)
        when {{ owner_source }} = 'faa_registry' then null
        when regexp_contains(upper({{ owner }}),
            r'\b(AIR|AIRLINES?|AIRWAYS|AVIATION|AERO|AIRCRAFT|JET|JETS|LTD|LLC|INC|CORP|CORPORATION|COMPANY|GMBH|AG|SA|SAS|SPA|PLC|LIMITED|BANK|LEASING|CAPITAL|HOLDINGS?|GROUP|FORCE|NAVY|ARMY|GOVERNMENT|MINISTRY|DEPARTMENT|POLICE|SHERIFF|COUNTY|UNIVERSITY|COLLEGE|SCHOOL|SERVICES?|CARGO|EXPRESS)\b')
            then {{ owner }}
    end
{%- endmacro %}
