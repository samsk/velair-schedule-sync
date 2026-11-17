"""Resolve specifier offsets and expand ramps."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import ScheduleConfig, default_step_c, resolve_alias
from .timeutil import (
    DAYS,
    WEEKDAY,
    WEEKEND,
    ceil_quarter,
    day_kind,
    floor_quarter,
    format_clock,
    parse_clock,
    parse_duration,
    wrap_add,
)

TOKEN_RE = re.compile(
    r"^(?:"
    r"(?P<clock>\d{1,2}:\d{2})"
    r"|(?P<bare>LT)(?P<bare_op>[+-])(?P<bare_off>\d+:\d{2})"
    r"|(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?P<bound>\.start|\.end)?"
    r"(?:(?P<op>[+-])(?P<off>\d+:\d{2}))?"
    r")$"
)


@dataclass
class ResolvedNode:
    """One resolved schedule node."""

    minutes: int
    clock: str
    source: str
    temp: Optional[float] = None
    hvac_mode: Optional[str] = None
    preset_mode: Optional[str] = None
    fan_mode: Optional[str] = None
    swing_mode: Optional[str] = None
    is_end_setback: bool = False


def _specs_for_day(cfg: ScheduleConfig, day: str) -> List[str]:
    """Names of concrete specifiers covering a day.

    Args:
        cfg: Config.
        day: mon..sun.
    Returns:
        Specifier names.
    """
    names = []
    for name, value in cfg.specifiers.items():
        spec = resolve_alias(cfg.specifiers, name) if isinstance(value, str) else value
        # only list concrete names once (skip aliases as keys for bare LT pick)
        if isinstance(value, str):
            continue
        if day in spec.days:
            names.append(name)
    return names


def _lt_specs_for_day(cfg: ScheduleConfig, day: str) -> List[str]:
    """LT-like specifier names covering a day (name starts with LT_).

    Args:
        cfg: Config.
        day: mon..sun.
    Returns:
        Specifier names starting with LT_.
    """
    return [n for n in _specs_for_day(cfg, day) if n.startswith("LT_")]


def resolve_token(
    cfg: ScheduleConfig,
    token: str,
    day: str,
    *,
    for_schedule: bool = True,
    is_end_setback: bool = False,
) -> Tuple[int, str]:
    """Resolve a time token to minutes and clock.

    Args:
        cfg: Config with specifiers.
        token: Clock or specifier expression.
        day: Concrete day mon..sun.
        for_schedule: Apply 15-min rounding when True.
        is_end_setback: Floor-round .end setbacks.
    Returns:
        (minutes, HH:MM).
    """
    match = TOKEN_RE.match(token.strip())
    if not match:
        raise ValueError(f"bad time token: {token!r}")

    if match.group("clock"):
        minutes = parse_clock(match.group("clock"))
        if for_schedule:
            minutes = floor_quarter(minutes) if is_end_setback else ceil_quarter(minutes)
        return minutes, format_clock(minutes)

    if match.group("bare"):
        lt_names = _lt_specs_for_day(cfg, day)
        if len(lt_names) == 0:
            raise ValueError(f"no LT specifier for {day}")
        if len(lt_names) > 1:
            raise ValueError(
                f"bare LT ambiguous on {day}: {lt_names}; use a named specifier"
            )
        name = lt_names[0]
        op = match.group("bare_op")
        off = parse_duration(match.group("bare_off"))
        return _apply_spec(cfg, name, day, None, op, off, for_schedule, is_end_setback)

    name = match.group("name")
    bound = match.group("bound")
    op = match.group("op")
    off_s = match.group("off")
    off = parse_duration(off_s) if off_s else 0
    if name == "LT":
        lt_names = _lt_specs_for_day(cfg, day)
        if len(lt_names) == 0:
            raise ValueError(f"no LT specifier for {day}")
        if len(lt_names) > 1:
            raise ValueError(
                f"bare LT ambiguous on {day}: {lt_names}; use a named specifier"
            )
        name = lt_names[0]
    end_setback = is_end_setback or (bound == ".end" and not op)
    return _apply_spec(cfg, name, day, bound, op, off, for_schedule, end_setback)


def _apply_spec(
    cfg: ScheduleConfig,
    name: str,
    day: str,
    bound: Optional[str],
    op: Optional[str],
    off: int,
    for_schedule: bool,
    is_end_setback: bool,
) -> Tuple[int, str]:
    """Apply named specifier with optional bound/offset.

    Args:
        cfg: Config.
        name: Specifier name.
        day: Concrete day.
        bound: .start/.end or None.
        op: + or - or None.
        off: Offset minutes.
        for_schedule: Round when True.
        is_end_setback: Floor round.
    Returns:
        (minutes, HH:MM).
    """
    spec = resolve_alias(cfg.specifiers, name)
    if day not in spec.days:
        raise ValueError(f"specifier {name} does not cover {day}")
    start = parse_clock(spec.start)
    end = parse_clock(spec.end)

    if bound == ".start":
        base = start
        if op == "+":
            minutes = wrap_add(base, off)
        elif op == "-":
            minutes = wrap_add(base, -off)
        else:
            minutes = base
    elif bound == ".end":
        base = end
        if op == "+":
            minutes = wrap_add(base, off)
        elif op == "-":
            minutes = wrap_add(base, -off)
        else:
            minutes = base
    elif op == "+":
        # + always from start
        minutes = wrap_add(start, off)
    elif op == "-":
        # - always from end
        minutes = wrap_add(end, -off)
    else:
        # bare name = start
        minutes = start

    if for_schedule:
        if is_end_setback or (bound == ".end" and not op):
            minutes = floor_quarter(minutes)
        else:
            minutes = ceil_quarter(minutes)
    return minutes, format_clock(minutes)


def _snap_temp(temp: float, step: float) -> float:
    """Snap temperature to step.

    Args:
        temp: Raw temp.
        step: Granularity.
    Returns:
        Snapped temp.
    """
    if step <= 0:
        return temp
    return round(round(temp / step) * step, 10)


def _node_fields(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Extract optional node fields.

    Args:
        raw: Source node dict.
    Returns:
        Field mapping.
    """
    out: Dict[str, Any] = {}
    for key in ("hvac_mode", "preset_mode", "fan_mode", "swing_mode"):
        if key in raw and raw[key] is not None:
            out[key] = raw[key]
    return out


def _expand_ramp(
    cfg: ScheduleConfig,
    entity: str,
    raw: Dict[str, Any],
    day: str,
    prev_temp: Optional[float],
    temp_step: float,
) -> List[ResolvedNode]:
    """Expand a ramp_to node into stepped nodes.

    Args:
        cfg: Config.
        entity: Entity id for default step_c.
        raw: Ramp node.
        day: Concrete day.
        prev_temp: Previous effective temp.
        temp_step: Snap step.
    Returns:
        Expanded ResolvedNode list.
    """
    if prev_temp is None:
        raise ValueError(f"{entity}: ramp needs a prior temp node")
    ramp_to = float(raw["ramp_to"])
    if ramp_to <= prev_temp:
        raise ValueError(f"{entity}: ramp_to {ramp_to} <= prev {prev_temp}")

    has_time = "time" in raw and raw["time"] is not None
    has_by = "by" in raw and raw["by"] is not None
    if has_time and has_by:
        raise ValueError(
            f"{entity}: ramp must anchor on time or by, not both "
            f"(over-constrains step count vs span)"
        )
    if not has_time and not has_by:
        raise ValueError(f"{entity}: ramp needs time or by")

    step_c = float(raw.get("step_c") or default_step_c(cfg, entity))
    every = parse_duration(str(raw.get("every") or "0:30"))
    if every <= 0:
        raise ValueError(f"{entity}: ramp every must be > 0")

    n = max(1, math.ceil((ramp_to - prev_temp) / step_c))
    fields = _node_fields(raw)
    temps = []
    for i in range(1, n + 1):
        t = min(prev_temp + step_c * i, ramp_to)
        if i == n:
            t = ramp_to
        temps.append(_snap_temp(t, temp_step))

    nodes: List[ResolvedNode] = []
    if has_by:
        by_min, _ = resolve_token(cfg, str(raw["by"]), day, for_schedule=True)
        for j in range(n):
            if j == n - 1:
                minutes = by_min
            else:
                minutes = ceil_quarter(wrap_add(by_min, -(n - 1 - j) * every))
            source = f"ramp:{raw['by']}[{j + 1}/{n}]"
            nodes.append(
                ResolvedNode(
                    minutes=minutes,
                    clock=format_clock(minutes),
                    source=source,
                    temp=temps[j],
                    **fields,
                )
            )
    else:
        start_min, _ = resolve_token(cfg, str(raw["time"]), day, for_schedule=True)
        for j in range(n):
            minutes = ceil_quarter(wrap_add(start_min, j * every))
            source = f"ramp:{raw['time']}[{j + 1}/{n}]"
            nodes.append(
                ResolvedNode(
                    minutes=minutes,
                    clock=format_clock(minutes),
                    source=source,
                    temp=temps[j],
                    **fields,
                )
            )
    return nodes


def resolve_day_nodes(
    cfg: ScheduleConfig,
    entity_key: str,
    day: str,
    *,
    profile: Optional[str] = None,
) -> List[ResolvedNode]:
    """Resolve all nodes for one schedule key and day.

    Args:
        cfg: Config.
        entity_key: climate.* or group:Name.
        day: mon..sun.
        profile: Optional profile name overlay.
    Returns:
        Sorted ResolvedNode list.
    """
    sched = _pick_schedule(cfg, entity_key, profile)
    kind = day_kind(day)
    mode = sched.get("mode") or "5/2"
    if mode == "all_days":
        raw_nodes = list(sched.get("all_days") or [])
    elif mode == "5/2":
        raw_nodes = list(sched.get(kind) or [])
    elif mode == "individual":
        raw_nodes = list(sched.get(day) or [])
    else:
        raise ValueError(f"unknown mode {mode!r} for {entity_key}")

    temp_step = float(sched.get("temp_step") or 0.5)
    entity = (
        entity_key
        if not entity_key.startswith("group:")
        else (cfg.groups.get(entity_key.split(":", 1)[1]) or {}).get("entities", [entity_key])[0]
    )

    out: List[ResolvedNode] = []
    prev_temp: Optional[float] = None
    for raw in raw_nodes:
        if "ramp_to" in raw:
            expanded = _expand_ramp(cfg, entity, raw, day, prev_temp, temp_step)
            out.extend(expanded)
            if expanded:
                prev_temp = expanded[-1].temp
            continue

        token = str(raw["time"])
        is_end = token.endswith(".end") and "+" not in token.split(".end")[-1]
        # also treat explicit setback after ramp as end when token is SPEC.end
        minutes, clock = resolve_token(
            cfg, token, day, for_schedule=True, is_end_setback=is_end
        )
        temp = raw.get("temp")
        if temp is not None:
            temp = _snap_temp(float(temp), temp_step)
            prev_temp = temp
        fields = _node_fields(raw)
        out.append(
            ResolvedNode(
                minutes=minutes,
                clock=clock,
                source=token,
                temp=temp,
                is_end_setback=is_end,
                **fields,
            )
        )

    out.sort(key=lambda n: n.minutes)
    return out


def _pick_schedule(
    cfg: ScheduleConfig, entity_key: str, profile: Optional[str]
) -> Dict[str, Any]:
    """Pick schedule dict, optionally from a profile.

    Args:
        cfg: Config.
        entity_key: Schedule key.
        profile: Profile name or None.
    Returns:
        Schedule mapping.
    """
    if profile:
        prof = cfg.profiles.get(profile) or {}
        if entity_key in prof:
            return prof[entity_key]
    if entity_key not in cfg.schedules:
        raise KeyError(f"no schedule for {entity_key}")
    return cfg.schedules[entity_key]


def resolve_all_days(
    cfg: ScheduleConfig,
    entity_key: str,
    *,
    profile: Optional[str] = None,
) -> Dict[str, List[ResolvedNode]]:
    """Resolve nodes for every weekday.

    Args:
        cfg: Config.
        entity_key: Schedule key.
        profile: Optional profile.
    Returns:
        Map day -> nodes.
    """
    return {d: resolve_day_nodes(cfg, entity_key, d, profile=profile) for d in DAYS}


def schedule_keys(cfg: ScheduleConfig, profile: Optional[str] = None) -> List[str]:
    """List schedule keys to apply.

    Args:
        cfg: Config.
        profile: If set, include profile overlays for known keys.
    Returns:
        Keys.
    """
    keys = list(cfg.schedules.keys())
    if profile and profile in cfg.profiles:
        for k in cfg.profiles[profile]:
            if k not in keys:
                keys.append(k)
    return keys


def format_nodes(nodes: Sequence[ResolvedNode], fmt: str = "ascii") -> str:
    """Format resolved nodes for display.

    Args:
        nodes: Resolved nodes.
        fmt: ascii or csv.
    Returns:
        Formatted string.
    """
    lines = []
    if fmt == "csv":
        lines.append("source,clock,temp,hvac_mode,preset_mode")
        for n in nodes:
            lines.append(
                f"{n.source},{n.clock},{n.temp if n.temp is not None else ''},"
                f"{n.hvac_mode or ''},{n.preset_mode or ''}"
            )
        return "\n".join(lines) + "\n"

    for n in nodes:
        parts = [f"{n.source} → {n.clock}"]
        if n.temp is not None:
            parts.append(f"{n.temp}")
        extras = []
        if n.hvac_mode:
            extras.append(n.hvac_mode)
        if n.preset_mode:
            extras.append(f"preset={n.preset_mode}")
        if extras:
            parts.append(f"[{'/'.join(extras)}]")
        lines.append(" ".join(parts))
    return "\n".join(lines) + ("\n" if lines else "")


def nodes_to_cs_payload(nodes: Sequence[ResolvedNode]) -> List[Dict[str, Any]]:
    """Convert resolved nodes to Climate Scheduler node dicts.

    Args:
        nodes: Resolved nodes.
    Returns:
        List of {time, temp?, hvac_mode?, ...}.
    """
    out = []
    for n in nodes:
        item: Dict[str, Any] = {"time": n.clock}
        if n.temp is not None:
            item["temp"] = n.temp
        if n.hvac_mode is not None:
            item["hvac_mode"] = n.hvac_mode
        if n.preset_mode is not None:
            item["preset_mode"] = n.preset_mode
        if n.fan_mode is not None:
            item["fan_mode"] = n.fan_mode
        if n.swing_mode is not None:
            item["swing_mode"] = n.swing_mode
        out.append(item)
    return out


def resolve_automation_trigger(
    cfg: ScheduleConfig, at_token: str, weekdays: Sequence[str]
) -> List[Tuple[str, str]]:
    """Resolve automation trigger times without schedule rounding.

    Args:
        cfg: Config.
        at_token: Time token.
        weekdays: Days the trigger applies to.
    Returns:
        List of (day, HH:MM) with exact minutes.
    """
    out = []
    for day in weekdays:
        minutes, _ = resolve_token(cfg, at_token, day, for_schedule=False)
        out.append((day, format_clock(minutes)))
    return out


def resolve_time_triggers(
    cfg: ScheduleConfig, auto: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Resolve time_switch triggers from specifier tokens.

    Args:
        cfg: Config.
        auto: Automation block with triggers list.
    Returns:
        HA time trigger list.
    """
    clocks: Dict[str, List[str]] = {}
    for trig in auto.get("triggers") or []:
        days = list(trig.get("weekday") or [])
        for day, clock in resolve_automation_trigger(cfg, str(trig["at"]), days):
            clocks.setdefault(clock, [])
            if day not in clocks[clock]:
                clocks[clock].append(day)
    return [
        {
            "trigger": "time",
            "at": clock if clock.count(":") == 2 else f"{clock}:00",
            "weekday": days,
        }
        for clock, days in clocks.items()
    ]
