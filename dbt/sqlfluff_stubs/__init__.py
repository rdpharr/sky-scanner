"""Lint-time stand-ins for dbt names that need a warehouse. Functions here are
global in sqlfluff's jinja templater; modules beside this file (dbt_utils,
target) are available by name. Never used by dbt."""


class _Column:
    def values(self):
        return ["2026-01-01 00:00:00"]


class _Result:
    columns = [_Column()]


def run_query(sql):
    """dbt's run_query returns an agate table; the models read one value."""
    return _Result()
