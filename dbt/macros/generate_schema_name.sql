{#- prod builds into the real datasets (analytics, public); every other target
    prefixes them (dev_analytics, dev_public) so development never touches
    what the website and MCP server read. -#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- set name = custom_schema_name or target.schema -%}
    {%- if target.name == 'prod' -%}{{ name }}{%- else -%}{{ target.name }}_{{ name }}{%- endif -%}
{%- endmacro %}
