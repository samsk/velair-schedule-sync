"""Golden resolved schedule output."""

from __future__ import annotations

from pathlib import Path

from lib.config import load_config
from lib.resolve import format_nodes, resolve_day_nodes


def test_hc1_weekday_golden(root: Path) -> None:
    """hc1 Monday matches expected_hc1_weekday.txt."""
    cfg = load_config(root / "schedule.yaml")
    nodes = resolve_day_nodes(cfg, "climate.hc1", "mon")
    body = format_nodes(nodes)
    expected = (root / "tests/expected_hc1_weekday.txt").read_text()
    assert "# climate.hc1 mon\n" + body == expected


def test_hc2_saturday_golden(root: Path) -> None:
    """hc2 Saturday matches expected_hc2_sat.txt."""
    cfg = load_config(root / "schedule.yaml")
    nodes = resolve_day_nodes(cfg, "climate.hc2", "sat")
    body = format_nodes(nodes)
    expected = (root / "tests/expected_hc2_sat.txt").read_text()
    assert "# climate.hc2 sat\n" + body == expected
