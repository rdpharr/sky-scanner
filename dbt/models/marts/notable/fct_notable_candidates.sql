-- Candidates for the weekly "Notable aircraft" edition: confirmed public
-- aircraft heard in a 7-day window, scored on rarity, military and
-- ownership signals, flight paths unusual for that aircraft, and frequency.
-- LADD/PIA aircraft are included and flagged. Every score comes with
-- readable reasons; the agent investigates the top candidates and decides.
-- Design notes and the prototype findings behind these choices:
-- docs/PLAN.md §4.8.
-- One window per Pacific day: the 7 complete days before window_end, so an
-- edition can be written (and re-evaluated) for any week since collection began.
with params as (
    select
        window_end,
        date_sub(window_end, interval 7 day) as window_start,
        timestamp(date_sub(window_end, interval 7 day), 'America/Los_Angeles') as week_start,
        timestamp(window_end, 'America/Los_Angeles') as week_end,
        date_diff(window_end, date('{{ var("collection_start") }}'), day)
        >= {{ var("novelty_min_history_days") }} as novelty_enabled
    from unnest(generate_date_array(
        date_add(date('{{ var("collection_start") }}'), interval 7 day),
        current_date('America/Los_Angeles')
    )) as window_end
),

aircraft as (
    select
        a.*,
        {{ public_owner('a.owner', 'a.owner_source', 'a.faa_registrant_is_organization', 'a.faa_registrant_type') }}
            as owner_name
    from {{ ref('dim_aircraft') }} as a
    where a.is_confirmed
),

week as (
    select
        p.window_end,
        s.*
    from {{ ref('fct_sightings') }} as s
    inner join params as p on s.started_at >= p.week_start and s.started_at < p.week_end
),

prior as (
    -- The 28 days before the window: each aircraft's own baseline.
    select
        p.window_end,
        s.*
    from {{ ref('fct_sightings') }} as s
    inner join params as p
        on
            s.started_at >= timestamp_sub(p.week_start, interval 28 day)
            and s.started_at < p.week_start
),

this_week as (
    select
        window_end,
        address_type,
        address,
        count(*) as sightings_7d,
        count(distinct date(started_at, 'America/Los_Angeles')) as days_seen_7d,
        sum(messages) as messages_7d,
        max(circling_ratio) as max_circling_ratio_7d,
        max(total_turn_deg) as max_turn_deg_7d,
        sum(low_away_from_airport_snapshots) as low_away_snapshots_7d,
        logical_or(emergency_squawk) as emergency_squawk_7d,
        array_concat_agg(squawks) as squawks_7d,
        max(max_altitude_baro_ft) as max_altitude_baro_ft_7d,
        min(min_altitude_baro_ft) as min_altitude_baro_ft_7d,
        max(max_ground_speed_kt) as max_ground_speed_kt_7d,
        sum(positioned_snapshots) as positioned_snapshots_7d
    from week
    group by window_end, address_type, address
),

baseline as (
    select
        window_end,
        address_type,
        address,
        count(*) as sightings_prior_28d,
        count(distinct date(started_at, 'America/Los_Angeles')) as days_seen_prior_28d,
        approx_quantiles(circling_ratio, 10 ignore nulls)[safe_offset(9)] as p90_circling_ratio_prior_28d
    from prior
    group by window_end, address_type, address
),

types as (
    -- Rarity as of each window's end: aircraft of the type heard before then,
    -- so past editions are not scored with what was heard later.
    select
        p.window_end,
        a.designator,
        count(*) as type_aircraft_heard,
        min(a.first_seen_at) as type_first_seen_at
    from aircraft as a
    inner join params as p on a.first_seen_at < p.week_end
    where a.designator is not null
    group by p.window_end, a.designator
),

features as (
    select
        w.window_end,
        p.window_start,
        a.address_type,
        a.address,
        a.registration,
        a.designator,
        a.type_name,
        a.aircraft_class,
        a.aircraft_kind,
        a.operator,
        a.owner_name,
        a.registrant_category,
        a.country,
        a.year_manufactured,
        a.is_military,
        a.military_rules,
        a.is_ladd,
        a.is_pia,
        a.latest_callsign,
        a.first_seen_at,
        w.* except (window_end, address_type, address),
        coalesce(b.sightings_prior_28d, 0) as sightings_prior_28d,
        coalesce(b.days_seen_prior_28d, 0) as days_seen_prior_28d,
        b.p90_circling_ratio_prior_28d,
        t.type_aircraft_heard,
        -- A type is only "new this week" once there are two weeks of history.
        t.type_first_seen_at >= p.week_start
        and date_diff(p.window_start, date('{{ var("collection_start") }}'), day) >= 14
            as type_first_seen_this_week,
        a.first_seen_at >= p.week_start as first_seen_this_week,
        p.novelty_enabled,
        exists(
            select 1 from unnest(w.squawks_7d) as sq
            where sq in ('7500', '7600', '7700'))
            as emergency_code_7d
    from this_week as w
    inner join aircraft as a using (address_type, address)
    left join baseline as b
        on b.window_end = w.window_end and b.address_type = a.address_type and b.address = a.address
    left join types as t on t.window_end = w.window_end and t.designator = a.designator
    inner join params as p on p.window_end = w.window_end
),

components as (
    select
        *,
        -- Type rarity: 1 aircraft of the type ever heard here = 1.0.
        least(
            1.0,
            case
                when type_aircraft_heard is null then 0
                when type_aircraft_heard = 1 then 1.0
                when type_aircraft_heard = 2 then 0.8
                when type_aircraft_heard <= 5 then 0.5
                when type_aircraft_heard <= 10 then 0.25
                else 0
            end + if(type_first_seen_this_week, 0.3, 0)
        ) as type_score,
        if(is_military, 1.0, 0) as military_score,
        -- Civil government and public-safety owners or operators (organizations
        -- only). Military aircraft are government-owned by definition; the
        -- military signal already covers them.
        if(not is_military and (
            registrant_category = 'Government'
            or regexp_contains(
                upper(concat(coalesce(owner_name, ''), ' ', coalesce(operator, ''))),
                r'POLICE|SHERIFF|COUNTY|STATE OF|CITY OF|UNITED STATES|DEPARTMENT|FEDERAL|FOREST SERVICE|BUREAU|CUSTOMS|BORDER|UNIVERSITY'  -- noqa: LT05
            )
        ),
        1.0, 0) as ownership_score,
        if(country is not null and country != 'United States' and aircraft_kind != 'Airline', 1.0, 0)
            as foreign_score,
        -- Vintage civil aircraft (FAA year of manufacture): 1975 or earlier.
        if(
            not is_military and year_manufactured is not null and year_manufactured <= 1975
            and aircraft_kind != 'Airline', 1.0, 0
        ) as vintage_score,
        -- Circling or low flying, relative to the aircraft's own last 28 days:
        -- an aircraft that circles every week (flight schools) scores low.
        case
            when positioned_snapshots_7d = 0 then null
            else least(
                1.0,
                case
                    when coalesce(max_circling_ratio_7d, 0) < 3 then 0
                    when p90_circling_ratio_prior_28d is null then 0.5
                    when p90_circling_ratio_prior_28d >= 3 then 0.1
                    else 1.0
                end
                + if(low_away_snapshots_7d >= 10 and coalesce(p90_circling_ratio_prior_28d, 0) < 3, 0.3, 0)
            )
        end as path_score,
        -- Aircraft-level novelty only once there is history to compare with.
        if(
            novelty_enabled,
            least(
                1.0,
                if(first_seen_this_week, 0.6, 0)
                + if(not first_seen_this_week and sightings_prior_28d = 0, 0.6, 0)
                + if(sightings_7d >= 5 and sightings_7d > 3 * sightings_prior_28d / 4, 0.4, 0)
            ),
            null
        ) as frequency_score,
        if(emergency_code_7d, 1.0, 0) as emergency_score
    from features
),

scored as (
    select
        *,
        -- A type heard only once here outweighs "military": the owner's
        -- reference week prized rare and vintage civil aircraft as much as
        -- military ones (evals/golden_week.json).
        (
            1.0 * emergency_score
            + 0.45 * type_score
            + 0.20 * military_score
            + 0.15 * vintage_score
            + 0.20 * ownership_score
            + 0.25 * coalesce(path_score, 0)
            + 0.15 * coalesce(frequency_score, 0)
            + 0.10 * foreign_score
        )
        -- Airliners are routine here unless something unusual happened.
        * if(aircraft_kind = 'Airline' and emergency_score = 0, 0.3, 1.0) as notable_score,
        -- Parts can be missing (e.g. no type name); drop reasons that came out null.
        array(select reason from
            unnest(array_concat(
                if(emergency_score > 0, [concat(
                    'Squawked an emergency code (',
                    (
                        select string_agg(distinct sq, ', ') from unnest(squawks_7d) as sq
                        where sq in ('7500', '7600', '7700')
                    ), ')'
                )], []),
                if(
                    type_aircraft_heard = 1, [concat('Only ', coalesce(type_name, designator), ' heard here')],
                    if(type_score >= 0.25, [concat(
                        'Rare type here: ', cast(type_aircraft_heard as string),
                        ' aircraft of type ', designator, ' heard'
                    )], [])
                ),
                if(
                    type_first_seen_this_week and type_aircraft_heard > 1,
                    [concat('Type ', designator, ' first heard here this week')], []
                ),
                if(is_military, [concat('Military (', array_to_string(military_rules, ', '), ')')], []),
                if(ownership_score > 0, [concat(
                    'Government or public-safety owner/operator: ',
                    coalesce(owner_name, operator, registrant_category, 'listed')
                )], []),
                if(foreign_score > 0, [concat('Foreign-registered (', country, '), not an airline')], []),
                if(vintage_score > 0, [concat('Vintage: built in ', cast(year_manufactured as string))], []),
                if(
                    coalesce(path_score, 0) > 0.2 and max_circling_ratio_7d >= 3,
                    [concat(
                        'Circled more than usual for this aircraft (circling ratio ',
                        cast(round(max_circling_ratio_7d, 1) as string), ')'
                    )], []
                ),
                if(
                    coalesce(path_score, 0) > 0 and low_away_snapshots_7d >= 10
                    and coalesce(p90_circling_ratio_prior_28d, 0) < 3,
                    [concat(
                        'Flew below 4,500 ft away from airports (', cast(low_away_snapshots_7d as string),
                        ' snapshots)'
                    )], []
                ),
                if(coalesce(frequency_score, 0) > 0, ['Unusual frequency: new, returning or a spike in visits'], [])
            )) as reason
        where reason is not null) as reasons
    from components
)

select
    *,
    -- Rank within the aircraft's type, so a list can show a few of each type.
    row_number() over (
        partition by window_end, coalesce(designator, address)
        order by notable_score desc, address asc
    ) as rank_within_type,
    count(*) over (partition by window_end, coalesce(designator, address)) as candidates_of_type
from scored
where notable_score > 0
