#!/usr/bin/env python3
"""Run broken fixtures; each must fail with expected rule id."""

from __future__ import annotations

import copy
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.config import load_config
from lib.lint import validate


def base() -> dict:
    """Load clean schedule mapping.

    Returns:
        Deep-copied raw dict.
    """
    return copy.deepcopy(yaml.safe_load((ROOT / "schedule.yaml").read_text()))


def write_tmp(data: dict) -> Path:
    """Write temp yaml.

    Args:
        data: Mapping.
    Returns:
        Temp path.
    """
    path = Path(tempfile.mkstemp(suffix=".yaml")[1])
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def expect_fail(name: str, data: dict, needle: str) -> None:
    """Assert validate fails mentioning needle.

    Args:
        name: Fixture name.
        data: Mutated yaml.
        needle: Expected rule id substring.
    """
    path = write_tmp(data)
    try:
        cfg = load_config(path)
        findings = validate(cfg)
        text = "\n".join(f.format() for f in findings)
        errors = [f for f in findings if f.severity == "error"]
        if not errors:
            raise SystemExit(f"{name}: expected failure, got ok\n{text}")
        if needle not in text:
            raise SystemExit(f"{name}: expected {needle!r} in:\n{text}")
        print(f"ok {name} -> {needle}")
    finally:
        path.unlink(missing_ok=True)


def main() -> int:
    """Run all bad fixtures.

    Returns:
        Exit code.
    """
    # 1) AKU +3.5 step
    d = base()
    d["schedules"]["climate.hc1"]["weekday"] = [
        {"time": "LT+2:00", "temp": 15.5, "hvac_mode": "heat"},
        {"time": "LT-1:00", "temp": 19.0, "hvac_mode": "heat"},
        {"time": "LT.end", "temp": 15.5, "hvac_mode": "heat"},
    ]
    expect_fail("aku_big_step", d, "[user:aku_step]")

    # 2) above_c 19 under hc2 hold 20.5
    d = base()
    for rule in d["rules"]["user"]:
        if rule["id"] == "no_hc1_hc2_dhw_hightemp_overlap":
            rule["above_c"] = 19
    expect_fail("threshold_sanity", d, "[builtin:rule_threshold_sanity]")

    # 3) ramp spilling past .end (by after LT end)
    d = base()
    d["schedules"]["climate.hc1"]["weekday"] = [
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
    expect_fail("ramp_spill", d, "[builtin:ramp_geometry]")

    # 4) missing weekend 00:00
    d = base()
    d["schedules"]["climate.hc2"]["weekend"] = [
        n
        for n in d["schedules"]["climate.hc2"]["weekend"]
        if n.get("time") != "00:00"
    ]
    expect_fail("missing_midnight", d, "[builtin:midnight_transition]")

    # 5) DHW overlapping hc2 (move tuv into hc2 boost)
    d = base()
    d["automations"]["tuv_charge"]["triggers"] = [
        {"at": "LT+4:15", "weekday": ["mon", "tue", "wed", "thu", "fri"]},
        {"at": "LT_weekend_morning+3:00", "weekday": ["sat", "sun"]},
        {"at": "LT_weekend_afternoon+0:05", "weekday": ["sat", "sun"]},
    ]
    expect_fail("dhw_overlap", d, "[user:no_hc1_hc2_dhw_hightemp_overlap]")

    print("all bad fixtures ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
