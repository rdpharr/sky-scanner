"""Lint-time stand-in for dbt's `target` (no warehouse connection in CI)."""

name = "dev"
project = "sky-scanner-pharr"
database = "sky-scanner-pharr"
schema = "analytics"
