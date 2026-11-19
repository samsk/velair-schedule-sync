"""Climate profile derivation, priority, and schema tests."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from lib.climate_profiles import (
    choose_mode,
    expand_schedule_key,
    parse_climate_profiles,
    resolve_climate_profile_day,
    schema_findings,
    season_select_automation,
    set_profile_body,
)
from lib.config import ScheduleConfig, load_config
from lib.lint import validate
from lib.resolve import resolve_day_nodes

ROOT = Path(__file__).resolve().parents[1]


_SEASON_SELECT = {
    "type": "season_select",
    "id": "velair_season_select",
    "alias": "Velair season profile select",
    "heating_season": "binary_sensor.heating_season",
    "outdoor": "sensor.outdoor_temperature",
    "labels": ["heating"],
}


@pytest.fixture
def cfg() -> ScheduleConfig:
    """Loaded schedule.yaml with climate_profiles forced on.

    Returns:
        ScheduleConfig.
    """
    loaded = load_config(ROOT / "schedule.yaml")
    raw_cp = dict(loaded.raw.get("climate_profiles") or {})
    raw_cp.pop("enabled", None)
    autos = dict(loaded.automations)
    autos["season_select"] = dict(_SEASON_SELECT)
    base = _with_profiles(loaded, raw_cp)
    return ScheduleConfig(
        path=base.path,
        raw=base.raw,
        version=base.version,
        context=base.context,
        specifiers=base.specifiers,
        rules=base.rules,
        reviews=base.reviews,
        schedules=base.schedules,
        profiles=base.profiles,
        climate_profiles=base.climate_profiles,
        groups=base.groups,
        automations=autos,
        legacy_disable=base.legacy_disable,
        backup=base.backup,
        collect=base.collect,
        scheduler=base.scheduler,
        expectations=base.expectations,
    )


def _with_profiles(cfg: ScheduleConfig, climate_profiles: dict) -> ScheduleConfig:
    """Clone config with replaced climate_profiles.

    Args:
        cfg: Base config.
        climate_profiles: New profiles mapping.
    Returns:
        ScheduleConfig.
    """
    return ScheduleConfig(
        path=cfg.path,
        raw=cfg.raw,
        version=1,
        context=cfg.context,
        specifiers=cfg.specifiers,
        rules=cfg.rules,
        reviews=cfg.reviews,
        schedules=cfg.schedules,
        profiles=cfg.profiles,
        climate_profiles=climate_profiles,
        groups=cfg.groups,
        automations=cfg.automations,
        legacy_disable=cfg.legacy_disable,
        backup=cfg.backup,
        collect=cfg.collect,
        scheduler=cfg.scheduler,
        expectations=cfg.expectations,
    )


def test_offset_snaps_and_skips_preset_only(cfg: ScheduleConfig) -> None:
    """Cold offsets temps, keeps clocks, leaves preset-only alone."""
    base = resolve_day_nodes(cfg, "climate.hc2", "mon")
    cold = resolve_climate_profile_day(cfg, "cold", "climate.hc2", "mon")
    assert [n.clock for n in cold] == [n.clock for n in base]
    for left, right in zip(base, cold):
        if left.temp is None:
            assert right.temp is None
            assert right.preset_mode == left.preset_mode
        else:
            assert right.temp == left.temp - 1.0
    cold_spec = next(s for s in parse_climate_profiles(cfg) if s.key == "cold")
    assert "climate.hc1" not in (cold_spec.entities or [])


def test_hvac_preset_only_unchanged_on_offset(cfg: ScheduleConfig) -> None:
    """HVAC preset-only nodes stay without a forced temp."""
    base = resolve_day_nodes(cfg, "climate.hvac", "mon")
    # HVAC is not in cold; offset helper still preserves preset-only semantics
    from lib.climate_profiles import offset_nodes

    out = offset_nodes(cfg, "climate.hvac", "mon", -1.0)
    for left, right in zip(base, out):
        if left.temp is None:
            assert right.temp is None
            assert right.preset_mode == left.preset_mode
        else:
            assert right.temp == left.temp - 1.0


def test_kids_expands_to_both_climates(cfg: ScheduleConfig) -> None:
    """group:Kids expands before Velair save."""
    assert expand_schedule_key(cfg, "group:Kids") == [
        "climate.heating_eh1",
        "climate.heating_eh2",
    ]
    body = set_profile_body(
        cfg, next(s for s in parse_climate_profiles(cfg) if s.key == "cold")
    )
    assert "climate.heating_eh1" in body["zones"]
    assert "climate.heating_eh2" in body["zones"]
    assert "group:Kids" not in body["zones"]
    assert "climate.hc1" not in body["zones"]


def test_off_season_hold_one_node(cfg: ScheduleConfig) -> None:
    """Off season is a single 00:00 hold."""
    nodes = resolve_climate_profile_day(cfg, "off_season", "climate.hc2", "mon")
    assert len(nodes) == 1
    assert nodes[0].clock == "00:00"
    assert nodes[0].temp == 15.5
    assert nodes[0].hvac_mode == "heat"


def test_hold_above_lowest_default_fails(cfg: ScheduleConfig) -> None:
    """Hold above the circuit floor is a schema error."""
    profiles = copy.deepcopy(cfg.climate_profiles)
    profiles["off_season"]["zones"]["climate.hc2"]["temp"] = 20
    msgs = schema_findings(_with_profiles(cfg, profiles))
    assert any("above lowest Default" in m for m in msgs)


def test_positive_offset_fails(cfg: ScheduleConfig) -> None:
    """Positive offset_c fails schema."""
    profiles = copy.deepcopy(cfg.climate_profiles)
    profiles["cold"]["offset_c"] = 1
    with pytest.raises(ValueError, match="negative"):
        parse_climate_profiles(_with_profiles(cfg, profiles))


def test_two_off_season_profiles_fail(cfg: ScheduleConfig) -> None:
    """Exactly one heating_season off profile is required."""
    profiles = copy.deepcopy(cfg.climate_profiles)
    profiles["also_off"] = {
        "mode": "Also off",
        "priority": 90,
        "when": {"heating_season": "off"},
        "zones": {"climate.hc2": {"temp": 15.5, "hvac_mode": "heat"}},
    }
    msgs = schema_findings(_with_profiles(cfg, profiles))
    assert any("exactly one heating_season off" in m for m in msgs)


def test_reserved_mode_name_fails(cfg: ScheduleConfig) -> None:
    """Default mode name is rejected."""
    profiles = copy.deepcopy(cfg.climate_profiles)
    profiles["cold"]["mode"] = "Default"
    with pytest.raises(ValueError, match="reserved"):
        parse_climate_profiles(_with_profiles(cfg, profiles))


def test_choose_priority(cfg: ScheduleConfig) -> None:
    """Off season beats cold; cold beats Default; thaw returns Default."""
    specs = parse_climate_profiles(cfg)
    assert choose_mode(specs, heating_season="off", outdoor_c=-30) == "Off season"
    assert choose_mode(specs, heating_season="on", outdoor_c=-20) == "Cold"
    assert choose_mode(specs, heating_season="on", outdoor_c=-19.9) == "Default"
    assert choose_mode(specs, heating_season="on", outdoor_c=5) == "Default"


def test_season_select_has_both_outdoor_edges(cfg: ScheduleConfig) -> None:
    """Automation watches both below and above the cold threshold."""
    auto = cfg.automations["season_select"]
    conf = season_select_automation(cfg, auto)
    triggers = conf["triggers"]
    assert any(
        t.get("trigger") == "numeric_state" and t.get("below") == -20 for t in triggers
    )
    assert any(
        t.get("trigger") == "numeric_state" and t.get("above") == -20 for t in triggers
    )
    assert not any(t.get("platform") == "homeassistant" for t in triggers)
    actions = conf["actions"][0]
    assert actions["default"][0]["data"]["option"] == "Default"
    options = [branch["sequence"][0]["data"]["option"] for branch in actions["choose"]]
    assert options[0] == "Off season"
    assert "Cold" in options


def test_schedule_yaml_validates(cfg: ScheduleConfig) -> None:
    """House schedule with climate profiles validates."""
    findings = validate(cfg, strict=True)
    errors = [f for f in findings if f.severity == "error"]
    assert errors == [], "\n".join(f.format() for f in errors)
