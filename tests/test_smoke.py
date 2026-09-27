"""Smoke test: the package layout imports."""

import importlib

import pytest

MODULES = [
    "scripts.resolve_topic",
    "scripts.fetch_pageviews",
    "scripts.analyze_trend",
    "scripts.make_chart",
    "scripts.make_report",
    "scripts.cli",
]


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name: str) -> None:
    importlib.import_module(name)
