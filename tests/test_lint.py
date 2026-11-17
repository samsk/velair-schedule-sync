"""Offline linter coverage."""

from __future__ import annotations

import copy
from pathlib import Path

import yaml

from lib.config import load_config
from lib.lint import validate


def _write_tmp(tmp_path: Path, data: dict) -> Path:
    """Write a temporary schedule yaml.

    Args:
        tmp_path: Pytest temp dir.
        data: Schedule mapping.
    Returns:
        Path to the written file.
    """
    path = tmp_path / "schedule.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def test_schedule_validates_clean(root: Path) -> None:
    """House schedule has no lint errors."""
    cfg = load_config(root / "schedule.yaml")
    findings = validate(cfg)
    errors = [f for f in findings if f.severity == "error"]
    assert errors == []


def test_aku_big_step(schedule_raw: dict, tmp_path: Path) -> None:
    """AKU step larger than 2.5 C fails aku_step."""
    data = copy.deepcopy(schedule_raw)
    data["schedules"]["climate.hc1"]["weekday"] = [
        {"time": "LT+2:00", "temp": 15.5, "hvac_mode": "heat"},
        {"time": "LT-1:00", "temp": 19.0, "hvac_mode": "heat"},
        {"time": "LT.end", "temp": 15.5, "hvac_mode": "heat"},
    ]
    cfg = load_config(_write_tmp(tmp_path, data))
    text = "\n".join(f.format() for f in validate(cfg))
    assert "[user:aku_step]" in text


def test_threshold_sanity(schedule_raw: dict, tmp_path: Path) -> None:
    """Overlap threshold below hc2 hold fails rule_threshold_sanity."""
    data = copy.deepcopy(schedule_raw)
    for rule in data["rules"]["user"]:
        if rule["id"] == "no_hc1_hc2_dhw_hightemp_overlap":
            rule["above_c"] = 19
    cfg = load_config(_write_tmp(tmp_path, data))
    text = "\n".join(f.format() for f in validate(cfg))
    assert "[builtin:rule_threshold_sanity]" in text


def test_ramp_spill(schedule_raw: dict, tmp_path: Path) -> None:
    """Ramp past LT.end fails ramp_geometry."""
    data = copy.deepcopy(schedule_raw)
    data["schedules"]["climate.hc1"]["weekday"] = [
        {"time": "LT+2:00", "temp": 15.5, "hvac_mode": "heat"},
        {
            "ramp_to": 24,
            "by": "LT.end+1:00",
            "step_c": 2.5,
            "every": "0:30",
            "hvac_mode": "heat",
        },
        {"time": "LT.end+1:30", "temp": 15.5, "hvac_mode": "heat"},
    ]
    cfg = load_config(_write_tmp(tmp_path, data))
    text = "\n".join(f.format() for f in validate(cfg))
    assert "[builtin:ramp_geometry]" in text


def test_missing_midnight(schedule_raw: dict, tmp_path: Path) -> None:
    """Weekend without 00:00 fails midnight_transition."""
    data = copy.deepcopy(schedule_raw)
    data["schedules"]["climate.hc2"]["weekend"] = [
        n
        for n in data["schedules"]["climate.hc2"]["weekend"]
        if n.get("time") != "00:00"
    ]
    cfg = load_config(_write_tmp(tmp_path, data))
    text = "\n".join(f.format() for f in validate(cfg))
    assert "[builtin:midnight_transition]" in text


def test_dhw_overlap(schedule_raw: dict, tmp_path: Path) -> None:
    """DHW trigger during hc2 boost fails overlap rule."""
    data = copy.deepcopy(schedule_raw)
    data["automations"]["tuv_charge"]["triggers"] = [
        {"at": "LT+4:15", "weekday": ["mon", "tue", "wed", "thu", "fri"]},
        {"at": "LT_weekend_morning+3:00", "weekday": ["sat", "sun"]},
        {"at": "LT_weekend_afternoon+0:05", "weekday": ["sat", "sun"]},
    ]
    cfg = load_config(_write_tmp(tmp_path, data))
    text = "\n".join(f.format() for f in validate(cfg))
    assert "[user:no_hc1_hc2_dhw_hightemp_overlap]" in text


def test_missing_automation_type(schedule_raw: dict, tmp_path: Path) -> None:
    """Automation without type fails config_schema."""
    data = copy.deepcopy(schedule_raw)
    del data["automations"]["pool_gate"]["type"]
    cfg = load_config(_write_tmp(tmp_path, data))
    text = "\n".join(f.format() for f in validate(cfg))
    assert "[builtin:config_schema]" in text
    assert "missing or unknown type" in text


def test_missing_duration_minutes(schedule_raw: dict, tmp_path: Path) -> None:
    """no_overlap_above without duration_minutes errors."""
    data = copy.deepcopy(schedule_raw)
    for rule in data["rules"]["user"]:
        if rule.get("id") == "no_hc1_hc2_dhw_hightemp_overlap":
            for auto in rule.get("automations") or []:
                auto.pop("duration_minutes", None)
    cfg = load_config(_write_tmp(tmp_path, data))
    text = "\n".join(f.format() for f in validate(cfg))
    assert "missing duration_minutes" in text


def test_bad_collect_ratio(schedule_raw: dict, tmp_path: Path) -> None:
    """Collect ratio pointing at unknown metric fails."""
    data = copy.deepcopy(schedule_raw)
    data["collect"]["ratios"]["bad"] = {"num": "missing", "den": "consumed_kwh"}
    cfg = load_config(_write_tmp(tmp_path, data))
    text = "\n".join(f.format() for f in validate(cfg))
    assert "[builtin:config_schema]" in text
    assert "unknown num" in text
