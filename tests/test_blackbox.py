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


def _run_apply(config: Path, url: str, *extra: str) -> subprocess.CompletedProcess[str]:
    """Run sync.py apply against a URL.

    Args:
        config: Schedule path.
        url: Fake HA URL.
        extra: Extra CLI args after apply.
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
        "apply",
        *extra,
    ]
    return subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, check=False)


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
