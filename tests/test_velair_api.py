"""Velair block mapping and helpers."""

from __future__ import annotations

from pathlib import Path

from lib.config import ScheduleConfig, load_config
from lib import velair_api
from sync import _temp_step


def test_node_to_block_set_temperature() -> None:
    """Temp + heat becomes set_temperature."""
    block = velair_api.node_to_block(
        {"time": "04:00", "temp": 18.0, "hvac_mode": "heat"}
    )
    assert block == {
        "start": "04:00",
        "action": "set_temperature",
        "temperature": 18.0,
        "hvac_mode": "heat",
    }


def test_node_to_block_turn_off() -> None:
    """hvac_mode off becomes turn_off."""
    assert velair_api.node_to_block({"time": "00:00", "hvac_mode": "off"}) == {
        "start": "00:00",
        "action": "turn_off",
    }


def test_node_to_block_preset_only() -> None:
    """Preset without temp becomes set_climate_options."""
    assert velair_api.node_to_block({"time": "18:00", "preset_mode": "high"}) == {
        "start": "18:00",
        "action": "set_climate_options",
        "preset_mode": "high",
    }


def test_node_to_block_temp_and_preset() -> None:
    """Temp + preset stay on set_temperature."""
    block = velair_api.node_to_block(
        {"time": "00:00", "temp": 18.5, "preset_mode": "home"}
    )
    assert block == {
        "start": "00:00",
        "action": "set_temperature",
        "temperature": 18.5,
        "preset_mode": "home",
    }


def test_days_for_label() -> None:
    """weekday and weekend expand to Velair day names."""
    assert velair_api.days_for_label("weekday") == list(velair_api.WEEKDAYS)
    assert velair_api.days_for_label("weekend") == list(velair_api.WEEKEND)
    assert velair_api.days_for_label("mon") == ["monday"]


def test_shared_nodes_match_and_differ() -> None:
    """shared_nodes returns nodes when equal and None when not."""
    nodes = [{"time": "00:00", "temp": 15.5, "hvac_mode": "heat"}]
    blocks = velair_api.nodes_to_blocks(nodes)
    a = {"monday": blocks}
    b = {"monday": blocks}
    assert velair_api.shared_nodes([a, b], ["monday"]) == nodes
    b2 = {"monday": velair_api.nodes_to_blocks(
        [{"time": "00:00", "temp": 16.0, "hvac_mode": "heat"}]
    )}
    assert velair_api.shared_nodes([a, b2], ["monday"]) is None


def test_temp_step_from_yaml(root: Path) -> None:
    """Explicit temp_step wins."""
    cfg = load_config(root / "schedule.yaml")
    desired = {"weekday": [{"time": "00:00", "temp": 15.5, "hvac_mode": "heat"}]}
    assert _temp_step(cfg, "climate.hc1", desired) == 0.5


def test_temp_step_from_fractional() -> None:
    """Fractional target yields 0.5 when yaml has no temp_step."""
    cfg = ScheduleConfig(
        path=Path("x.yaml"),
        raw={},
        version=1,
        context={},
        specifiers={},
        rules={},
        reviews=[],
        schedules={"climate.x": {}},
        profiles={},
        climate_profiles={},
        groups={},
        automations={},
        legacy_disable=[],
        backup={},
    )
    desired = {"weekday": [{"time": "00:00", "temp": 19.5, "hvac_mode": "heat"}]}
    assert _temp_step(cfg, "climate.x", desired) == 0.5
