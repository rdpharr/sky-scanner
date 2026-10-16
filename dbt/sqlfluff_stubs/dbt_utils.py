"""Lint-time stand-in for the dbt_utils package (sqlfluff's jinja templater
cannot load package macros without a dbt project). Never used by dbt."""


def generate_surrogate_key(field_list):
    return "to_hex(md5(concat(" + ", ".join(f"coalesce(cast({f} as string), '')" for f in field_list) + ")))"
