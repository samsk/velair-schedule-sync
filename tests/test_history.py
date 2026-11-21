"""Unit tests for schedule vs history compare helpers."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from lib.config import load_config
from lib.history import (
    Sample,
    compare_day,
    format_compare_table,
    parse_history,
    value_at,
)

ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Europe/Bratislava")


def test_value_at_picks_last_before() -> None:
    """value_at returns the latest sample at or before the clock."""
    samples = [
        Sample(datetime(2026, 11, 21, 5, 0, tzinfo=TZ), "heat", 20.9, 18.0, {}),
        Sample(datetime(2026, 11, 21, 5, 45, tzinfo=TZ), "heat", 20.8, 21.5, {}),
        Sample(datetime(2026, 11, 21, 6, 15, tzinfo=TZ), "heat", 20.8, 20.5, {}),
    ]
    hit = value_at(samples, datetime(2026, 11, 21, 6, 0, tzinfo=TZ))
    assert hit is not None
    assert hit.setpt == 21.5
    assert value_at(samples, datetime(2026, 11, 21, 4, 0, tzinfo=TZ)) is None


def test_parse_history_reads_climate_attrs() -> None:
    """Climate history rows expose actual and setpoint attrs."""
    rows = [
        {
            "entity_id": "climate.hc2",
            "state": "heat",
            "attributes": {"current_temperature": 20.8, "temperature": 21.5},
            "last_updated": "2026-11-21T05:45:00+01:00",
        }
    ]
    samples = parse_history(rows)
    assert len(samples) == 1
    assert samples[0].actual == 20.8
    assert samples[0].setpt == 21.5


def test_compare_day_builds_delta_rows(tmp_path: Path) -> None:
    """compare_day aligns schedule clocks with history samples."""
    raw = {
        "version": 1,
        "specifiers": {
            "LT_weekend_morning": {
                "days": ["sat", "sun"],
                "start": "02:30",
                "end": "08:15",
            }
        },
        "rules": {"disable": None, "user": []},
        "schedules": {
            "climate.hc2": {
                "mode": "5/2",
                "temp_step": 0.5,
                "weekday": [
                    {"time": "00:00", "temp": 15.5, "hvac_mode": "heat"},
                ],
                "weekend": [
                    {"time": "00:00", "temp": 15.5, "hvac_mode": "heat"},
                    {
                        "time": "LT_weekend_morning+2:15",
                        "temp": 21.5,
                        "hvac_mode": "heat",
                    },
                    {
                        "time": "LT_weekend_morning-2:00",
                        "temp": 20.5,
                        "hvac_mode": "heat",
                    },
                ],
            }
        },
        "groups": {},
        "automations": {},
        "reviews": [],
        "profiles": {},
        "climate_profiles": {},
        "legacy_disable": [],
        "backup": {},
        "collect": {},
    }
    path = tmp_path / "mini.yaml"
    import yaml

    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    cfg = load_config(path)
    samples = [
        Sample(datetime(2026, 11, 21, 0, 0, tzinfo=TZ), "heat", 21.0, 15.5, {}),
        Sample(datetime(2026, 11, 21, 4, 45, tzinfo=TZ), "heat", 20.8, 21.5, {}),
        Sample(datetime(2026, 11, 21, 6, 15, tzinfo=TZ), "heat", 20.8, 20.5, {}),
    ]
    rows = compare_day(
        cfg, "climate.hc2", "sat", date(2026, 11, 21), samples, tz=TZ
    )
    assert [r.clock for r in rows] == ["00:00", "04:45", "06:15"]
    peak = rows[1]
    assert peak.sched_temp == 21.5
    assert peak.actual == 20.8
    assert peak.delta == -0.7
    text = format_compare_table(rows)
    assert "04:45" in text
    assert "sched" in text
