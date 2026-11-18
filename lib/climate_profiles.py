"""Derive and validate Velair climate profiles from yaml."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import ScheduleConfig
from .resolve import (
    DAYS,
    ResolvedNode,
    _snap_temp,
    nodes_to_cs_payload,
    resolve_day_nodes,
)
from .velair_api import DAY_NAME, node_to_block

RESERVED_MODE_NAMES = frozenset(
    {"default", "manual", "unknown", "unavailable", "predeterminado"}
)
PROTECTED_AUTOMATION_IDS = frozenset(
    {"1726390292836", "pool_gate_velair", "1760162526412"}
)
SEASON_SELECT_ID = "velair_season_select"


@dataclass
class ClimateProfileSpec:
    """One climate_profiles entry after schema normalize.

    Attributes:
        key: Stable profile id.
        mode: Visible Velair mode name.
        priority: Descending select priority.
        when: Condition mapping.
        kind: hold or offset.
        zones: Hold temps by schedule key.
        offset_c: Offset for derived week.
        entities: Schedule keys for offset profile.
        for_minutes: Outdoor stability delay.
    """

    key: str
    mode: str
    priority: int
    when: Dict[str, Any]
    kind: str
    zones: Dict[str, Dict[str, Any]]
    offset_c: Optional[float] = None
    entities: Optional[List[str]] = None
    for_minutes: int = 1


def expand_schedule_key(cfg: ScheduleConfig, key: str) -> List[str]:
    """Expand a schedule key to climate entity ids.

    Args:
        cfg: Config.
        key: climate.* or group:Name.
    Returns:
        Entity ids.
    """
    if key.startswith("group:"):
        name = key.split(":", 1)[1]
        return [str(e) for e in (cfg.groups.get(name) or {}).get("entities") or []]
    return [key]


def parse_climate_profiles(cfg: ScheduleConfig) -> List[ClimateProfileSpec]:
    """Parse climate_profiles into specs.

    Args:
        cfg: Config.
    Returns:
        Spec list.
    Raises:
        ValueError: On invalid schema.
    """
    raw = cfg.climate_profiles or {}
    if not isinstance(raw, dict):
        raise ValueError("climate_profiles must be a mapping")
    out: List[ClimateProfileSpec] = []
    for key, block in raw.items():
        if not isinstance(block, dict):
            raise ValueError(f"climate_profiles.{key} must be a mapping")
        mode = str(block.get("mode") or "").strip()
        if not mode:
            raise ValueError(f"climate_profiles.{key}: missing mode")
        if mode.casefold() in RESERVED_MODE_NAMES or len(mode) > 255:
            raise ValueError(f"climate_profiles.{key}: reserved or long mode {mode!r}")
        if "priority" not in block:
            raise ValueError(f"climate_profiles.{key}: missing priority")
        priority = int(block["priority"])
        when = dict(block.get("when") or {})
        if not when:
            raise ValueError(f"climate_profiles.{key}: missing when")
        has_zones = "zones" in block
        has_offset = block.get("from") == "default" or "offset_c" in block
        if has_zones and has_offset:
            raise ValueError(f"climate_profiles.{key}: hold and offset together")
        if has_zones:
            zones = dict(block.get("zones") or {})
            if not zones:
                raise ValueError(f"climate_profiles.{key}: empty zones")
            out.append(
                ClimateProfileSpec(
                    key=str(key),
                    mode=mode,
                    priority=priority,
                    when=when,
                    kind="hold",
                    zones=zones,
                    for_minutes=int(when.get("for_minutes") or 1),
                )
            )
            continue
        if not has_offset:
            raise ValueError(f"climate_profiles.{key}: need zones or from:default")
        if block.get("from") != "default":
            raise ValueError(f"climate_profiles.{key}: from must be default")
        offset = float(block["offset_c"])
        if offset >= 0:
            raise ValueError(f"climate_profiles.{key}: offset_c must be negative")
        entities = [str(e) for e in (block.get("entities") or [])]
        if not entities:
            raise ValueError(f"climate_profiles.{key}: empty entities")
        for_minutes = int(when.get("for_minutes") or block.get("for_minutes") or 1)
        if for_minutes < 1:
            raise ValueError(f"climate_profiles.{key}: for_minutes must be >= 1")
        out.append(
            ClimateProfileSpec(
                key=str(key),
                mode=mode,
                priority=priority,
                when=when,
                kind="offset",
                zones={},
                offset_c=offset,
                entities=entities,
                for_minutes=for_minutes,
            )
        )
    return out


def schema_findings(cfg: ScheduleConfig) -> List[str]:
    """Validate climate_profiles schema.

    Args:
        cfg: Config.
    Returns:
        Error messages.
    """
    if not cfg.climate_profiles:
        return []
    try:
        specs = parse_climate_profiles(cfg)
    except ValueError as exc:
        return [str(exc)]
    errs: List[str] = []
    priorities = [s.priority for s in specs]
    if len(set(priorities)) != len(priorities):
        errs.append("climate_profiles: duplicate priority")
    off = [s for s in specs if str(s.when.get("heating_season")) == "off"]
    if len(off) != 1:
        errs.append("climate_profiles: need exactly one heating_season off profile")
    elif any(s.priority > off[0].priority for s in specs):
        errs.append("climate_profiles: off season must have highest priority")
    for spec in specs:
        season = str(spec.when.get("heating_season") or "")
        if season not in ("on", "off"):
            errs.append(f"climate_profiles.{spec.key}: when.heating_season must be on/off")
        if season == "on" and spec.when.get("outdoor_at_or_below_c") is None:
            errs.append(f"climate_profiles.{spec.key}: missing outdoor_at_or_below_c")
        if season == "off" and spec.when.get("outdoor_at_or_below_c") is not None:
            errs.append(f"climate_profiles.{spec.key}: off season cannot set outdoor")
        if spec.kind == "hold":
            for key, zone in spec.zones.items():
                ents = expand_schedule_key(cfg, key)
                if not ents:
                    errs.append(f"climate_profiles.{spec.key}: unknown {key}")
                if not isinstance(zone, dict) or zone.get("temp") is None:
                    errs.append(f"climate_profiles.{spec.key}.{key}: need temp")
                else:
                    try:
                        floor = lowest_default_temp(cfg, key)
                    except Exception as exc:
                        errs.append(f"climate_profiles.{spec.key}.{key}: {exc}")
                        continue
                    if float(zone["temp"]) > floor:
                        errs.append(
                            f"climate_profiles.{spec.key}.{key}: "
                            f"hold {zone['temp']} above lowest Default {floor}"
                        )
        else:
            for key in spec.entities or []:
                if key not in cfg.schedules:
                    errs.append(f"climate_profiles.{spec.key}: unknown entity {key}")
                if not expand_schedule_key(cfg, key):
                    errs.append(f"climate_profiles.{spec.key}: empty expand {key}")
    return errs


def lowest_default_temp(cfg: ScheduleConfig, key: str) -> float:
    """Lowest temperature node on Default for a schedule key.

    Args:
        cfg: Config.
        key: Schedule key.
    Returns:
        Lowest temp.
    """
    temps: List[float] = []
    for day in DAYS:
        for node in resolve_day_nodes(cfg, key, day):
            if node.temp is not None:
                temps.append(float(node.temp))
    if not temps:
        raise ValueError(f"no Default temps for {key}")
    return min(temps)


def hold_nodes(zone: Dict[str, Any], temp_step: float) -> List[ResolvedNode]:
    """Build an all-day hold node list.

    Args:
        zone: {temp, hvac_mode?}.
        temp_step: Snap step.
    Returns:
        One 00:00 node.
    """
    temp = _snap_temp(float(zone["temp"]), temp_step)
    return [
        ResolvedNode(
            minutes=0,
            clock="00:00",
            source="hold",
            temp=temp,
            hvac_mode=zone.get("hvac_mode"),
            preset_mode=zone.get("preset_mode"),
        )
    ]


def offset_nodes(
    cfg: ScheduleConfig, key: str, day: str, offset_c: float
) -> List[ResolvedNode]:
    """Offset Default nodes for one key and day.

    Args:
        cfg: Config.
        key: Schedule key.
        day: mon..sun.
        offset_c: Negative offset.
    Returns:
        Offset nodes.
    """
    sched = cfg.schedules.get(key) or {}
    step = float(sched.get("temp_step") or 0.5)
    out: List[ResolvedNode] = []
    for node in resolve_day_nodes(cfg, key, day):
        temp = node.temp
        if temp is not None:
            temp = _snap_temp(float(temp) + offset_c, step)
        out.append(
            ResolvedNode(
                minutes=node.minutes,
                clock=node.clock,
                source=node.source,
                temp=temp,
                hvac_mode=node.hvac_mode,
                preset_mode=node.preset_mode,
                fan_mode=node.fan_mode,
                swing_mode=node.swing_mode,
                is_end_setback=node.is_end_setback,
            )
        )
    return out


def resolve_climate_profile_day(
    cfg: ScheduleConfig, profile_key: str, entity_key: str, day: str
) -> List[ResolvedNode]:
    """Resolve one climate profile day for show/validate.

    Args:
        cfg: Config.
        profile_key: climate_profiles key.
        entity_key: Schedule key.
        day: mon..sun.
    Returns:
        Resolved nodes.
    """
    specs = {s.key: s for s in parse_climate_profiles(cfg)}
    spec = specs.get(profile_key)
    if spec is None:
        raise KeyError(f"unknown climate profile {profile_key}")
    if spec.kind == "hold":
        if entity_key not in spec.zones:
            raise KeyError(f"{profile_key} has no zone {entity_key}")
        step = float((cfg.schedules.get(entity_key) or {}).get("temp_step") or 0.5)
        return hold_nodes(spec.zones[entity_key], step)
    if entity_key not in (spec.entities or []):
        raise KeyError(f"{profile_key} has no entity {entity_key}")
    return offset_nodes(cfg, entity_key, day, float(spec.offset_c))


def climate_profile_keys(cfg: ScheduleConfig, profile_key: str) -> List[str]:
    """Schedule keys owned by a climate profile.

    Args:
        cfg: Config.
        profile_key: Profile key.
    Returns:
        Keys.
    """
    specs = {s.key: s for s in parse_climate_profiles(cfg)}
    spec = specs[profile_key]
    if spec.kind == "hold":
        return list(spec.zones.keys())
    return list(spec.entities or [])


def choose_mode(
    specs: Sequence[ClimateProfileSpec],
    *,
    heating_season: str,
    outdoor_c: Optional[float],
) -> str:
    """Pick mode name from current states by priority.

    Args:
        specs: Profile specs.
        heating_season: on/off.
        outdoor_c: Outdoor temperature or None.
    Returns:
        Mode name or Default.
    """
    ordered = sorted(specs, key=lambda s: s.priority, reverse=True)
    for spec in ordered:
        want = str(spec.when.get("heating_season"))
        if want != heating_season:
            continue
        if want == "off":
            return spec.mode
        thr = float(spec.when["outdoor_at_or_below_c"])
        if outdoor_c is not None and outdoor_c <= thr:
            return spec.mode
    return "Default"


def outdoor_thresholds(specs: Sequence[ClimateProfileSpec]) -> List[float]:
    """Distinct outdoor thresholds for triggers.

    Args:
        specs: Profile specs.
    Returns:
        Sorted thresholds ascending.
    """
    values = []
    for spec in specs:
        if spec.when.get("outdoor_at_or_below_c") is not None:
            values.append(float(spec.when["outdoor_at_or_below_c"]))
    return sorted(set(values))


def velair_zone_payload(
    cfg: ScheduleConfig, spec: ClimateProfileSpec
) -> Dict[str, Any]:
    """Build Velair set_profile zones mapping.

    Args:
        cfg: Config.
        spec: Profile spec.
    Returns:
        entity_id -> zone behavior.
    """
    zones: Dict[str, Any] = {}
    if spec.kind == "hold":
        for key, zone in spec.zones.items():
            step = float((cfg.schedules.get(key) or {}).get("temp_step") or 0.5)
            nodes = hold_nodes(zone, step)
            blocks = [node_to_block(n) for n in nodes_to_cs_payload(nodes)]
            schedule = {DAY_NAME[d]: list(blocks) for d in DAYS}
            for ent in expand_schedule_key(cfg, key):
                zones[ent] = {
                    "behavior": "schedule",
                    "day_layout_key": "all_days",
                    "schedule": schedule,
                    "template_links": {},
                }
        return zones

    for key in spec.entities or []:
        sched = cfg.schedules.get(key) or {}
        mode = sched.get("mode") or "5/2"
        schedule: Dict[str, List[Dict[str, Any]]] = {}
        if mode == "5/2":
            weekday = [
                node_to_block(n)
                for n in nodes_to_cs_payload(
                    offset_nodes(cfg, key, "mon", float(spec.offset_c))
                )
            ]
            weekend = [
                node_to_block(n)
                for n in nodes_to_cs_payload(
                    offset_nodes(cfg, key, "sat", float(spec.offset_c))
                )
            ]
            for d in ("monday", "tuesday", "wednesday", "thursday", "friday"):
                schedule[d] = list(weekday)
            for d in ("saturday", "sunday"):
                schedule[d] = list(weekend)
            layout = "weekday_weekend"
        elif mode == "all_days":
            blocks = [
                node_to_block(n)
                for n in nodes_to_cs_payload(
                    offset_nodes(cfg, key, "mon", float(spec.offset_c))
                )
            ]
            schedule = {DAY_NAME[d]: list(blocks) for d in DAYS}
            layout = "all_days"
        else:
            for day in DAYS:
                schedule[DAY_NAME[day]] = [
                    node_to_block(n)
                    for n in nodes_to_cs_payload(
                        offset_nodes(cfg, key, day, float(spec.offset_c))
                    )
                ]
            layout = "individual"
        for ent in expand_schedule_key(cfg, key):
            zones[ent] = {
                "behavior": "schedule",
                "day_layout_key": layout,
                "schedule": schedule,
                "template_links": {},
            }
    return zones


def set_profile_body(cfg: ScheduleConfig, spec: ClimateProfileSpec) -> Dict[str, Any]:
    """Body for velair/set_profile.

    Args:
        cfg: Config.
        spec: Profile spec.
    Returns:
        Profile payload.
    """
    return {
        "key": spec.key,
        "name": spec.mode,
        "zones": velair_zone_payload(cfg, spec),
    }


def set_mode_body(spec: ClimateProfileSpec) -> Dict[str, Any]:
    """Body for velair/set_mode.

    Args:
        spec: Profile spec.
    Returns:
        Mode payload.
    """
    return {"key": spec.key, "name": spec.mode, "profile_ids": [spec.key]}


def season_select_automation(
    cfg: ScheduleConfig, auto: Dict[str, Any]
) -> Dict[str, Any]:
    """Build season_select automation config.

    Args:
        cfg: Config.
        auto: Automation block.
    Returns:
        HA automation mapping.
    """
    specs = parse_climate_profiles(cfg)
    season = str(auto["heating_season"])
    outdoor = str(auto["outdoor"])
    thresholds = outdoor_thresholds(specs)
    triggers: List[Dict[str, Any]] = [
        {
            "trigger": "state",
            "entity_id": season,
            "for": {"hours": 0, "minutes": 1, "seconds": 0},
        }
    ]
    for thr in thresholds:
        delay = 30
        for spec in specs:
            if spec.when.get("outdoor_at_or_below_c") is not None and float(
                spec.when["outdoor_at_or_below_c"]
            ) == thr:
                delay = int(spec.for_minutes)
                break
        triggers.append(
            {
                "trigger": "numeric_state",
                "entity_id": outdoor,
                "below": thr,
                "for": {"hours": 0, "minutes": delay, "seconds": 0},
            }
        )
        triggers.append(
            {
                "trigger": "numeric_state",
                "entity_id": outdoor,
                "above": thr,
            }
        )

    choose: List[Dict[str, Any]] = []
    for spec in sorted(specs, key=lambda s: s.priority, reverse=True):
        conditions: List[Dict[str, Any]] = [
            {
                "condition": "state",
                "entity_id": season,
                "state": str(spec.when["heating_season"]),
            }
        ]
        if spec.when.get("outdoor_at_or_below_c") is not None:
            conditions.append(
                {
                    "condition": "numeric_state",
                    "entity_id": outdoor,
                    "below": float(spec.when["outdoor_at_or_below_c"]) + 1e-9,
                }
            )
            # HA below is exclusive of equality in some paths; use template for <=
            conditions = [
                {
                    "condition": "state",
                    "entity_id": season,
                    "state": str(spec.when["heating_season"]),
                },
                {
                    "condition": "template",
                    "value_template": (
                        "{{ states('%s') | float(999) <= %s }}"
                        % (outdoor, float(spec.when["outdoor_at_or_below_c"]))
                    ),
                },
            ]
        choose.append(
            {
                "conditions": conditions,
                "sequence": [
                    {
                        "action": "select.select_option",
                        "target": {"entity_id": "select.velair_mode"},
                        "data": {"option": spec.mode},
                    }
                ],
            }
        )

    return {
        "id": str(auto.get("id") or SEASON_SELECT_ID),
        "alias": auto.get("alias") or "Velair season profile select",
        "mode": "single",
        "triggers": triggers,
        "conditions": [],
        "actions": [
            {
                "choose": choose,
                "default": [
                    {
                        "action": "select.select_option",
                        "target": {"entity_id": "select.velair_mode"},
                        "data": {"option": "Default"},
                    }
                ],
            }
        ],
    }


def protected_ids() -> frozenset:
    """Automation ids this tool must never overwrite for season_select.

    Returns:
        Id set.
    """
    return PROTECTED_AUTOMATION_IDS
