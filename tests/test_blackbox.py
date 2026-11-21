"""Blackbox apply against the fake HA server."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.fake_ha import TOKEN, FakeHaServer

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/house_mini.yaml"


@pytest.fixture
def fake_ha():
    """Start and stop the fake HA.

    Yields:
        Running FakeHaServer.
    """
    server = FakeHaServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _run_sync(
    config: Path, url: str, *args: str
) -> subprocess.CompletedProcess[str]:
    """Run sync.py against a URL.

    Args:
        config: Schedule path.
        url: Fake HA URL.
        args: Subcommand and flags.
    Returns:
        Completed process.
    """
    cmd = [
        sys.executable,
        str(ROOT / "sync.py"),
        "--config",
        str(config),
        "--url",
        url,
        "--token",
        TOKEN,
        *args,
    ]
    return subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, check=False)


def _run_apply(config: Path, url: str, *extra: str) -> subprocess.CompletedProcess[str]:
    """Run sync.py apply against a URL.

    Args:
        config: Schedule path.
        url: Fake HA URL.
        extra: Extra CLI args after apply.
    Returns:
        Completed process.
    """
    return _run_sync(config, url, "apply", *extra)


def test_compare_prints_schedule_vs_history(
    fake_ha: FakeHaServer, tmp_path: Path
) -> None:
    """compare prints sched/setpt/actual columns from HA history."""
    config = tmp_path / "house_mini.yaml"
    shutil.copy(FIXTURE, config)
    result = _run_sync(
        config,
        fake_ha.url,
        "compare",
        "--entity",
        "climate.hc1",
        "--day",
        "sat",
        "--date",
        "2026-11-21",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "compare climate.hc1 sat 2026-11-21" in result.stdout
    assert "sched" in result.stdout
    assert "actual" in result.stdout


def test_collect_history_metrics_dry_run(
    fake_ha: FakeHaServer, tmp_path: Path
) -> None:
    """collect dry-run includes windowed history_metrics."""
    config = tmp_path / "house_mini.yaml"
    shutil.copy(FIXTURE, config)
    data = yaml.safe_load(config.read_text())
    data["specifiers"]["weekend_LT_early"] = {
        "days": ["sat", "sun"],
        "start": "02:30",
        "end": "04:00",
    }
    data["collect"]["history_metrics"] = {
        "mod_mean": {
            "entity": "sensor.actual_modulation",
            "agg": "mean",
            "windows": ["LT_weekend_morning"],
        },
        "mod_high_h": {
            "entity": "sensor.actual_modulation",
            "agg": "hours_at_or_above",
            "threshold": 95,
            "windows": ["LT_weekend_morning"],
        },
        "outdoor_early": {
            "entity": "sensor.outdoor_temperature",
            "agg": "mean",
            "windows": ["weekend_LT_early"],
        },
    }
    config.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    fake_ha.state.history["sensor.actual_modulation"] = [
        {
            "entity_id": "sensor.actual_modulation",
            "state": "0",
            "attributes": {},
            "last_changed": "2026-11-21T02:00:00+01:00",
            "last_updated": "2026-11-21T02:00:00+01:00",
        },
        {
            "entity_id": "sensor.actual_modulation",
            "state": "99",
            "attributes": {},
            "last_changed": "2026-11-21T05:30:00+01:00",
            "last_updated": "2026-11-21T05:30:00+01:00",
        },
    ]
    fake_ha.state.history["sensor.outdoor_temperature"] = [
        {
            "entity_id": "sensor.outdoor_temperature",
            "state": "8.5",
            "attributes": {},
            "last_changed": "2026-11-21T02:30:00+01:00",
            "last_updated": "2026-11-21T02:30:00+01:00",
        },
        {
            "entity_id": "sensor.outdoor_temperature",
            "state": "5.0",
            "attributes": {},
            "last_changed": "2026-11-21T04:00:00+01:00",
            "last_updated": "2026-11-21T04:00:00+01:00",
        },
    ]
    result = _run_sync(
        config, fake_ha.url, "collect", "--since", "2026-11-21", "--dry-run"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "mod_mean" in result.stdout
    assert "mod_high_h" in result.stdout
    assert "outdoor_early" in result.stdout
    assert "dry-run: reviews not written" in result.stdout


def test_apply_dry_run_zero_writes(fake_ha: FakeHaServer, tmp_path: Path) -> None:
    """Dry-run prints desired diffs and writes nothing."""
    config = tmp_path / "house_mini.yaml"
    shutil.copy(FIXTURE, config)
    before = len(fake_ha.state.writes)
    result = _run_apply(config, fake_ha.url, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 writes" in result.stdout
    assert len(fake_ha.state.writes) == before
    assert fake_ha.state.turn_off_calls == []


def test_apply_yes_stores_blocks_and_triggers(fake_ha: FakeHaServer, tmp_path: Path) -> None:
    """apply --yes stores half-degree blocks and DHW triggers."""
    config = tmp_path / "house_mini.yaml"
    shutil.copy(FIXTURE, config)
    result = _run_apply(config, fake_ha.url, "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "writes=" in result.stdout
    assert fake_ha.state.turn_off_calls == []

    mon = fake_ha.state.zones["climate.hc1"]["monday"]
    assert mon[0]["temperature"] == 15.5
    assert mon[0]["hvac_mode"] == "heat"
    assert mon[1]["temperature"] == 18.0
    assert fake_ha.state.steps["climate.hc1"] == 0.5

    hvac_mon = fake_ha.state.zones["climate.hvac"]["monday"]
    presets = [b.get("preset_mode") for b in hvac_mon]
    assert "high" in presets
    assert "home" in presets

    pool_mon = fake_ha.state.zones["climate.pool"]["monday"]
    assert pool_mon[0]["action"] == "turn_off"
    assert any(b.get("temperature") == 29 for b in pool_mon)

    tuv = fake_ha.state.automations["1760162526412"]
    triggers = tuv["triggers"]
    assert {"trigger": "time", "at": "23:01:00", "weekday": ["mon", "tue", "wed", "thu", "fri"]} in triggers
    assert {"trigger": "time", "at": "04:30:00", "weekday": ["sat", "sun"]} in triggers
    assert tuv["conditions"] == [
        {
            "condition": "numeric_state",
            "entity_id": "sensor.sonoff_10018e421f_temperature",
            "below": 45,
        }
    ]
    assert tuv["actions"] == [
        {"action": "switch.turn_on", "target": {"entity_id": "switch.charge"}}
    ]
    tuv_labels = set(fake_ha.state.entities["automation.tuv_charge"]["labels"])
    assert tuv_labels == {"heating", "managed", "tuv"}
    assert "managed" in fake_ha.state.labels
    pool_labels = set(
        fake_ha.state.entities["automation.pool_heat_only_if_outdoor_at_least_5_c"][
            "labels"
        ]
    )
    assert "managed" in pool_labels
    assert "heating" in pool_labels

    raw = yaml.safe_load(config.read_text())
    assert str(raw.get("backup", {}).get("last", "")).startswith("backups/")


def test_apply_climate_profiles_without_activate(
    fake_ha: FakeHaServer, tmp_path: Path
) -> None:
    """apply upserts profiles and season automation without activating."""
    raw = yaml.safe_load(FIXTURE.read_text())
    raw["groups"] = {
        "Kids": {"entities": ["climate.heating_eh1", "climate.heating_eh2"]}
    }
    raw["schedules"]["climate.hc2"] = {
        "mode": "5/2",
        "temp_step": 0.5,
        "weekday": [
            {"time": "00:00", "temp": 15.5, "hvac_mode": "heat"},
            {"time": "06:00", "temp": 18, "hvac_mode": "heat"},
            {"time": "17:00", "temp": 15.5, "hvac_mode": "heat"},
        ],
        "weekend": [
            {"time": "00:00", "temp": 15.5, "hvac_mode": "heat"},
            {"time": "08:00", "temp": 18, "hvac_mode": "heat"},
            {"time": "17:00", "temp": 15.5, "hvac_mode": "heat"},
        ],
    }
    raw["schedules"]["group:Kids"] = {
        "mode": "5/2",
        "temp_step": 0.5,
        "weekday": [
            {"time": "00:00", "temp": 19, "hvac_mode": "heat"},
            {"time": "06:00", "temp": 20, "hvac_mode": "heat"},
            {"time": "20:00", "temp": 19, "hvac_mode": "heat"},
        ],
        "weekend": [
            {"time": "00:00", "temp": 19, "hvac_mode": "heat"},
        ],
    }
    raw["climate_profiles"] = {
        "off_season": {
            "mode": "Off season",
            "priority": 100,
            "when": {"heating_season": "off"},
            "zones": {
                "climate.hc1": {"temp": 15.5, "hvac_mode": "heat"},
                "climate.hc2": {"temp": 15.5, "hvac_mode": "heat"},
                "group:Kids": {"temp": 19, "hvac_mode": "heat"},
            },
        },
        "cold": {
            "mode": "Cold",
            "priority": 50,
            "when": {
                "heating_season": "on",
                "outdoor_at_or_below_c": -20,
                "for_minutes": 30,
            },
            "from": "default",
            "offset_c": -1,
            "entities": ["climate.hc2", "group:Kids"],
        },
    }
    raw["automations"]["season_select"] = {
        "type": "season_select",
        "id": "velair_season_select",
        "alias": "Velair season profile select",
        "heating_season": "binary_sensor.heating_season",
        "outdoor": "sensor.outdoor_temperature",
        "labels": ["heating"],
    }
    config = tmp_path / "house_profiles.yaml"
    config.write_text(yaml.safe_dump(raw, sort_keys=False))

    dry = _run_apply(config, fake_ha.url, "--dry-run")
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert "climate_profile cold:" in dry.stdout
    assert fake_ha.state.forbidden_calls == []
    assert not any("set_profile" in w for w in fake_ha.state.writes)

    result = _run_apply(config, fake_ha.url, "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "cold" in fake_ha.state.profiles
    assert "off_season" in fake_ha.state.profiles
    assert "cold" in fake_ha.state.modes
    assert fake_ha.state.modes["cold"]["profile_ids"] == ["cold"]
    assert "climate.hc1" not in fake_ha.state.profiles["cold"]["zones"]
    assert "climate.heating_eh1" in fake_ha.state.profiles["cold"]["zones"]
    season = fake_ha.state.automations["velair_season_select"]
    assert any(t.get("above") == -20 for t in season["triggers"])
    assert any(t.get("below") == -20 for t in season["triggers"])
    assert fake_ha.state.forbidden_calls == []
    assert not any("activate" in w for w in fake_ha.state.writes)
    assert not any(w == "select.select_option" for w in fake_ha.state.writes)

    # overlay apply must not push climate profiles
    before = list(fake_ha.state.writes)
    overlay = _run_apply(config, fake_ha.url, "--yes", "--profile", "Summer")
    assert overlay.returncode == 0, overlay.stdout + overlay.stderr
    assert "skips climate_profiles" in overlay.stdout or "NOTE: --profile" in overlay.stdout
    new_writes = fake_ha.state.writes[len(before) :]
    assert not any(w.startswith("set_profile") for w in new_writes)
    assert not any(w.startswith("set_mode") for w in new_writes)
