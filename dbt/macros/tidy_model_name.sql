{#- Registry model names often start with an all-caps manufacturer
    ("LOCKHEED MARTIN C-130J"): title-case that leading run of words, leaving
    designations such as UH-60 alone. -#}
{% macro tidy_model_name(expr) -%}
    if(regexp_contains({{ expr }}, r'^(?:[A-Z]{2,} )+'),
       concat(initcap(regexp_extract({{ expr }}, r'^((?:[A-Z]{2,} )+)')),
              regexp_replace({{ expr }}, r'^(?:[A-Z]{2,} )+', '')),
       {{ expr }})
{%- endmacro %}
