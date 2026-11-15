"""Built-in and user schedule linters."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from .config import ScheduleConfig, resolve_alias
from .resolve import (
    ResolvedNode,
    resolve_all_days,
    resolve_automation_trigger,
    resolve_day_nodes,
    schedule_keys,
)
from .timeutil import DAYS, WEEKDAY, WEEKEND, parse_clock, wrap_add

BUILTIN_IDS = {
    "grid_15min",
    "unique_times",
    "temp_bounds",
    "temp_step_multiple",
    "ramp_geometry",
    "rule_threshold_sanity",
    "midnight_transition",
    "cycle_closed",
    "noop_nodes",
    "time_grid",
}

USER_TYPES = {
    "max_step_up",
    "max_temp_outside",
    "no_overlap_above",
    "forbid_node_keys",
    "min_temp_at",
    "max_temp_at",
    "time_grid",
    "temp_bounds",
    "temp_step_multiple",
    "unique_times",
    "ramp_geometry",
    "rule_threshold_sanity",
    "midnight_transition",
    "cycle_closed",
    "noop_nodes",
}


@dataclass
class Finding:
    """One linter finding."""

    layer: str  # builtin|user
    rule_id: str
    severity: str  # error|warn
    entity: str
    day: str
    message: str

    def format(self) -> str:
        """Format finding for CLI output.

        Returns:
            One-line finding string.
        """
        return (
            f"[{self.layer}:{self.rule_id}] {self.entity} {self.day}: {self.message}"
        )


def _disabled(cfg: ScheduleConfig) -> Set[str]:
    """Collect disabled built-in ids.

    Args:
        cfg: Config.
    Returns:
        Set of disabled ids.
    """
    out: Set[str] = set()
    for item in cfg.rules.get("disable") or []:
        if not item:
            continue
        rid = item.get("id") if isinstance(item, dict) else None
        if not rid:
            continue
        if rid not in BUILTIN_IDS:
            raise ValueError(f"unknown rules.disable id: {rid}")
        out.add(rid)
    return out


def _effective_temp_timeline(
    nodes: Sequence[ResolvedNode],
) -> List[Tuple[int, Optional[float], Optional[str]]]:
    """Build (minutes, temp, hvac_mode) holding timeline.

    Args:
        nodes: Sorted day nodes.
    Returns:
        List of state changes.
    """
    timeline = []
    temp: Optional[float] = None
    mode: Optional[str] = None
    for n in nodes:
        if n.hvac_mode is not None:
            mode = n.hvac_mode
        if n.temp is not None and mode != "off":
            temp = n.temp
        if mode == "off":
            temp = None
        timeline.append((n.minutes, temp, mode))
    return timeline


def _temp_at(timeline: Sequence[Tuple[int, Optional[float], Optional[str]]], minute: int) -> Optional[float]:
    """Effective temp at minute (same day).

    Args:
        timeline: Day timeline.
        minute: Minute of day.
    Returns:
        Temp or None if off/unknown.
    """
    current: Optional[float] = None
    for m, t, mode in timeline:
        if m > minute:
            break
        if mode == "off":
            current = None
        elif t is not None:
            current = t
    return current


def lint_grid_15min(cfg: ScheduleConfig, key: str, day: str, nodes: List[ResolvedNode]) -> List[Finding]:
    """Check 15-minute grid.

    Args:
        cfg: Config.
        key: Schedule key.
        day: Day.
        nodes: Resolved nodes.
    Returns:
        Findings.
    """
    out = []
    for n in nodes:
        if n.minutes % 15 != 0:
            out.append(
                Finding("builtin", "grid_15min", "error", key, day, f"{n.clock} not on 15-min grid")
            )
    return out


def lint_unique_times(cfg: ScheduleConfig, key: str, day: str, nodes: List[ResolvedNode]) -> List[Finding]:
    """Reject duplicate minutes.

    Args:
        cfg: Config.
        key: Schedule key.
        day: Day.
        nodes: Nodes.
    Returns:
        Findings.
    """
    out = []
    seen: Dict[int, str] = {}
    for n in nodes:
        if n.minutes in seen:
            out.append(
                Finding(
                    "builtin",
                    "unique_times",
                    "error",
                    key,
                    day,
                    f"duplicate {n.clock} ({seen[n.minutes]} and {n.source})",
                )
            )
        seen[n.minutes] = n.source
    return out


def lint_temp_bounds(cfg: ScheduleConfig, key: str, day: str, nodes: List[ResolvedNode]) -> List[Finding]:
    """Temp within 5..35 C.

    Args:
        cfg: Config.
        key: Key.
        day: Day.
        nodes: Nodes.
    Returns:
        Findings.
    """
    out = []
    for n in nodes:
        if n.temp is None:
            continue
        if n.temp < 5 or n.temp > 35:
            out.append(
                Finding(
                    "builtin",
                    "temp_bounds",
                    "error",
                    key,
                    day,
                    f"temp {n.temp} outside 5-35 at {n.clock}",
                )
            )
    return out


def lint_temp_step_multiple(
    cfg: ScheduleConfig, key: str, day: str, nodes: List[ResolvedNode]
) -> List[Finding]:
    """Temps multiple of entity temp_step.

    Args:
        cfg: Config.
        key: Key.
        day: Day.
        nodes: Nodes.
    Returns:
        Findings.
    """
    sched = cfg.schedules.get(key) or {}
    step = float(sched.get("temp_step") or 0.5)
    out = []
    for n in nodes:
        if n.temp is None:
            continue
        # allow float tolerance
        ratio = n.temp / step
        if abs(ratio - round(ratio)) > 1e-6:
            out.append(
                Finding(
                    "builtin",
                    "temp_step_multiple",
                    "error",
                    key,
                    day,
                    f"temp {n.temp} not multiple of {step} at {n.clock}",
                )
            )
    return out


def lint_noop_nodes(cfg: ScheduleConfig, key: str, day: str, nodes: List[ResolvedNode]) -> List[Finding]:
    """Warn on consecutive identical nodes.

    Args:
        cfg: Config.
        key: Key.
        day: Day.
        nodes: Nodes.
    Returns:
        Findings.
    """
    out = []
    for a, b in zip(nodes, nodes[1:]):
        if a.hvac_mode == "off" and b.hvac_mode == "off":
            continue
        if (
            a.temp == b.temp
            and a.hvac_mode == b.hvac_mode
            and a.preset_mode == b.preset_mode
        ):
            out.append(
                Finding(
                    "builtin",
                    "noop_nodes",
                    "warn",
                    key,
                    day,
                    f"noop {a.clock}->{b.clock} identical state",
                )
            )
    return out


def lint_cycle_closed(cfg: ScheduleConfig, key: str, day: str, nodes: List[ResolvedNode]) -> List[Finding]:
    """Boosted day must return to setback/off.

    Args:
        cfg: Config.
        key: Key.
        day: Day.
        nodes: Nodes.
    Returns:
        Findings.
    """
    if not nodes:
        return []
    temps = [n.temp for n in nodes if n.temp is not None]
    modes = [n.hvac_mode for n in nodes if n.hvac_mode is not None]
    out = []
    if temps:
        mn, mx = min(temps), max(temps)
        if mx > mn + 0.05:
            # must have a node at or near min after a boost, or end at min
            if nodes[-1].temp is None or nodes[-1].temp > mn + 0.05:
                # allow if any later setback equals min
                if not any(n.temp is not None and abs(n.temp - mn) < 0.05 for n in nodes[1:]):
                    out.append(
                        Finding(
                            "builtin",
                            "cycle_closed",
                            "error",
                            key,
                            day,
                            f"boosts to {mx} but never returns to setback {mn}",
                        )
                    )
    if "heat" in modes and "off" in modes:
        if modes[-1] != "off" and not any(m == "off" for m in modes[1:]):
            out.append(
                Finding(
                    "builtin",
                    "cycle_closed",
                    "error",
                    key,
                    day,
                    "enters heat but never returns to off",
                )
            )
    return out


def _anchor_spec_name(cfg: ScheduleConfig, anchor: str, day: str) -> Optional[str]:
    """Specifier name referenced by a ramp anchor token.

    Args:
        cfg: Config.
        anchor: by/time token.
        day: Day name.
    Returns:
        Specifier name or None for plain clocks.
    """
    if not anchor or re.match(r"^\d{1,2}:\d{2}$", anchor):
        return None
    if anchor.startswith("LT") and not anchor.startswith("LT_"):
        names = [
            n
            for n, v in cfg.specifiers.items()
            if not isinstance(v, str) and n.startswith("LT_") and day in v.days
        ]
        return names[0] if len(names) == 1 else None
    # longest matching specifier prefix
    names = sorted(
        (n for n, v in cfg.specifiers.items() if not isinstance(v, str)),
        key=len,
        reverse=True,
    )
    for name in names:
        if anchor == name or anchor.startswith(name + ".") or anchor.startswith(name + "+") or anchor.startswith(name + "-"):
            return name
    return None


def lint_ramp_geometry(cfg: ScheduleConfig, key: str, day: str, nodes: List[ResolvedNode]) -> List[Finding]:
    """Validate raw ramp definitions and containment.

    Args:
        cfg: Config.
        key: Key.
        day: Day.
        nodes: Resolved nodes for this day.
    Returns:
        Findings.
    """
    out: List[Finding] = []
    kind = "weekday" if day in WEEKDAY else "weekend"
    sched = cfg.schedules.get(key) or {}
    mode = sched.get("mode") or "5/2"
    if mode == "all_days":
        raw_nodes = list(sched.get("all_days") or [])
    elif mode == "5/2":
        raw_nodes = list(sched.get(kind) or [])
    else:
        raw_nodes = list(sched.get(day) or [])

    prev_temp = None
    ramp_index = 0
    for raw in raw_nodes:
        if "ramp_to" not in raw:
            if raw.get("temp") is not None:
                prev_temp = float(raw["temp"])
            continue
        has_time = raw.get("time") is not None
        has_by = raw.get("by") is not None
        if has_time and has_by:
            out.append(
                Finding(
                    "builtin",
                    "ramp_geometry",
                    "error",
                    key,
                    day,
                    "ramp sets both time and by",
                )
            )
        if prev_temp is None:
            out.append(
                Finding(
                    "builtin",
                    "ramp_geometry",
                    "error",
                    key,
                    day,
                    "ramp has no prior temp",
                )
            )
        elif float(raw["ramp_to"]) <= prev_temp:
            out.append(
                Finding(
                    "builtin",
                    "ramp_geometry",
                    "error",
                    key,
                    day,
                    f"ramp_to {raw['ramp_to']} <= prev {prev_temp}",
                )
            )

        anchor = str(raw.get("by") or raw.get("time") or "")
        spec_name = _anchor_spec_name(cfg, anchor, day)
        if spec_name:
            try:
                spec_val = resolve_alias(cfg.specifiers, spec_name)
                start = parse_clock(spec_val.start)
                end = parse_clock(spec_val.end)
                # nodes belonging to this ramp share source prefix with anchor
                prefix = f"ramp:{anchor}"
                for n in nodes:
                    if not n.source.startswith(prefix):
                        continue
                    if not _minute_in_window(n.minutes, start, end):
                        out.append(
                            Finding(
                                "builtin",
                                "ramp_geometry",
                                "error",
                                key,
                                day,
                                f"ramp node {n.clock} outside {spec_name}",
                            )
                        )
            except Exception as exc:
                out.append(
                    Finding(
                        "builtin",
                        "ramp_geometry",
                        "error",
                        key,
                        day,
                        f"containment check failed: {exc}",
                    )
                )
        ramp_index += 1
        prev_temp = float(raw["ramp_to"])
    return out


def _minute_in_window(minute: int, start: int, end: int) -> bool:
    """Check minute inside possibly overnight window [start, end).

    Args:
        minute: Minute of day.
        start: Window start.
        end: Window end.
    Returns:
        True if inside (end inclusive for setback boundary checks use <= end).
    """
    if start == end:
        return minute == start
    if start < end:
        return start <= minute <= end
    # overnight
    return minute >= start or minute <= end


def lint_midnight_transition(cfg: ScheduleConfig, key: str) -> List[Finding]:
    """Compare fri->sat and sun->mon vs steady neighbors.

    Args:
        cfg: Config.
        key: Schedule key.
    Returns:
        Findings.
    """
    by_day = resolve_all_days(cfg, key)
    out: List[Finding] = []

    def first_temp(day: str) -> Optional[float]:
        nodes = by_day[day]
        for n in nodes:
            if n.temp is not None:
                return n.temp
            if n.hvac_mode == "off":
                return None
        return None

    def has_midnight_node(day: str) -> bool:
        return any(n.minutes == 0 for n in by_day[day])

    # weekend days should have explicit 00:00 when LT doesn't cover midnight
    for day in WEEKEND:
        # sat/sun midnight is HT for this house
        if not has_midnight_node(day):
            out.append(
                Finding(
                    "builtin",
                    "midnight_transition",
                    "error",
                    key,
                    day,
                    "missing 00:00 HT reset node",
                )
            )

    fri = first_temp("fri")
    sat = first_temp("sat")
    sun = first_temp("sun")
    mon = first_temp("mon")
    tue = first_temp("tue")
    # sat should match sun steady; fri->sat may differ (that's the point of reset)
    if sat is not None and sun is not None and abs(sat - sun) > 0.05:
        # soft: weekend should be consistent at 00:00
        out.append(
            Finding(
                "builtin",
                "midnight_transition",
                "warn",
                key,
                "sat",
                f"sat 00:00 temp {sat} != sun {sun}",
            )
        )
    if mon is not None and tue is not None and abs(mon - tue) > 0.05:
        out.append(
            Finding(
                "builtin",
                "midnight_transition",
                "warn",
                key,
                "mon",
                f"mon first temp {mon} != tue {tue}",
            )
        )
    return out


def _lt_windows(cfg: ScheduleConfig, day: str) -> List[Tuple[int, int]]:
    """LT windows for a day as (start,end) minutes.

    Args:
        cfg: Config.
        day: Day.
    Returns:
        List of windows.
    """
    wins = []
    for name, value in cfg.specifiers.items():
        if isinstance(value, str):
            continue
        if not name.startswith("LT_"):
            continue
        if day not in value.days:
            continue
        wins.append((parse_clock(value.start), parse_clock(value.end)))
    return wins


def _in_any_window(minute: int, windows: Sequence[Tuple[int, int]]) -> bool:
    """Whether minute is inside any window.

    Args:
        minute: Minute.
        windows: Windows.
    Returns:
        True if inside.
    """
    return any(_minute_in_window(minute, s, e) for s, e in windows)


def lint_rule_threshold_sanity(cfg: ScheduleConfig) -> List[Finding]:
    """Catch above_c below daytime hold.

    Args:
        cfg: Config.
    Returns:
        Findings.
    """
    out: List[Finding] = []
    rules = list(cfg.rules.get("user") or [])
    for rule in rules:
        if rule.get("type") not in ("no_overlap_above", "max_temp_outside"):
            continue
        above = rule.get("above_c")
        entities = list(rule.get("entities") or [])
        thresholds: Dict[str, float] = {}
        if isinstance(above, dict):
            thresholds = {k: float(v) for k, v in above.items()}
        elif above is not None:
            thresholds = {e: float(above) for e in entities}

        for ent in entities:
            key = ent if ent in cfg.schedules else None
            # map group members back - only climate keys in schedules
            if key is None:
                continue
            thr = thresholds.get(ent)
            if thr is None:
                continue
            # measure minutes above thr outside LT across weekday
            for day in DAYS:
                try:
                    nodes = resolve_day_nodes(cfg, key, day)
                except Exception:
                    continue
                lt = _lt_windows(cfg, day)
                timeline = _effective_temp_timeline(nodes)
                # sample each 15 min
                above_mins = 0
                hold = None
                for m in range(0, 1440, 15):
                    t = _temp_at(timeline, m)
                    if t is None:
                        continue
                    if t > thr:
                        hold = max(hold or t, t)
                        if not _in_any_window(m, lt):
                            above_mins += 15
                if above_mins >= 6 * 60:
                    out.append(
                        Finding(
                            "builtin",
                            "rule_threshold_sanity",
                            "error",
                            ent,
                            day,
                            f"above_c={thr} below daytime hold (~{hold}); "
                            f"outside LT for {above_mins} min; suggest >= {hold}",
                        )
                    )
    return out


def lint_max_step_up(cfg: ScheduleConfig, rule: Dict[str, Any]) -> List[Finding]:
    """User max_step_up rule.

    Args:
        cfg: Config.
        rule: Rule dict.
    Returns:
        Findings.
    """
    rid = rule.get("id", "max_step_up")
    limit = float(rule.get("limit_c", 2.5))
    gap = int(rule.get("min_gap_minutes") or 0)
    window_c = rule.get("window_c")
    window_minutes = rule.get("window_minutes")
    out: List[Finding] = []
    for ent in rule.get("entities") or []:
        if ent not in cfg.schedules and not any(
            ent in ((cfg.groups.get(k.split(':',1)[1]) or {}).get('entities') or [])
            for k in cfg.schedules if k.startswith('group:')
        ):
            key = ent
        else:
            key = ent if ent in cfg.schedules else ent
        # prefer direct schedule key
        sched_key = key if key in cfg.schedules else None
        if sched_key is None:
            continue
        for day in DAYS:
            nodes = resolve_day_nodes(cfg, sched_key, day)
            prev_t = None
            prev_up_m = None
            ups: List[Tuple[int, float]] = []
            for n in nodes:
                if n.temp is None:
                    continue
                if prev_t is not None and n.temp > prev_t:
                    delta = n.temp - prev_t
                    if delta > limit + 1e-6:
                        out.append(
                            Finding(
                                "user",
                                rid,
                                "error",
                                sched_key,
                                day,
                                f"step +{delta} > {limit} at {n.clock}",
                            )
                        )
                    if gap and prev_up_m is not None:
                        dm = (n.minutes - prev_up_m) % 1440
                        if 0 < dm < gap:
                            out.append(
                                Finding(
                                    "user",
                                    rid,
                                    "error",
                                    sched_key,
                                    day,
                                    f"up-steps {dm} min apart < {gap} at {n.clock}",
                                )
                            )
                    prev_up_m = n.minutes
                    ups.append((n.minutes, delta))
                prev_t = n.temp
            if window_c is not None and window_minutes is not None:
                wmin = int(window_minutes)
                wlim = float(window_c)
                for i, (m0, _) in enumerate(ups):
                    total = 0.0
                    for m1, d in ups[i:]:
                        if (m1 - m0) % 1440 > wmin:
                            break
                        total += d
                    if total > wlim + 1e-6:
                        out.append(
                            Finding(
                                "user",
                                rid,
                                "error",
                                sched_key,
                                day,
                                f"rise {total} > {wlim} within {wmin} min",
                            )
                        )
    return out


def lint_max_temp_outside(cfg: ScheduleConfig, rule: Dict[str, Any]) -> List[Finding]:
    """User max_temp_outside rule.

    Args:
        cfg: Config.
        rule: Rule dict.
    Returns:
        Findings.
    """
    rid = rule.get("id", "max_temp_outside")
    above = float(rule.get("above_c", 19))
    windows = list(rule.get("windows") or [])
    out: List[Finding] = []
    for ent in rule.get("entities") or []:
        if ent not in cfg.schedules:
            continue
        for day in DAYS:
            nodes = resolve_day_nodes(cfg, ent, day)
            wins: List[Tuple[int, int]] = []
            for w in windows:
                if w == "LT":
                    wins.extend(_lt_windows(cfg, day))
                elif w == "HT":
                    # invert LT
                    pass
                else:
                    try:
                        spec = resolve_alias(cfg.specifiers, w)
                    except ValueError:
                        continue
                    if day not in spec.days:
                        continue
                    wins.append((parse_clock(spec.start), parse_clock(spec.end)))
            timeline = _effective_temp_timeline(nodes)
            for m in range(0, 1440, 15):
                t = _temp_at(timeline, m)
                if t is None or t <= above:
                    continue
                if not _in_any_window(m, wins):
                    out.append(
                        Finding(
                            "user",
                            rid,
                            "error",
                            ent,
                            day,
                            f"temp {t} > {above} outside windows at {m // 60:02d}:{m % 60:02d}",
                        )
                    )
                    break
    return out


def lint_no_overlap_above(cfg: ScheduleConfig, rule: Dict[str, Any]) -> List[Finding]:
    """User no_overlap_above across entities and automations.

    Args:
        cfg: Config.
        rule: Rule dict.
    Returns:
        Findings.
    """
    rid = rule.get("id", "no_overlap_above")
    entities = list(rule.get("entities") or [])
    above = rule.get("above_c", 19)
    thresholds: Dict[str, float] = {}
    if isinstance(above, dict):
        thresholds = {k: float(v) for k, v in above.items()}
    else:
        thresholds = {e: float(above) for e in entities}

    out: List[Finding] = []
    for day in DAYS:
        # active consumers per minute
        active: Dict[int, List[str]] = {m: [] for m in range(0, 1440, 15)}
        for ent in entities:
            if ent not in cfg.schedules:
                continue
            nodes = resolve_day_nodes(cfg, ent, day)
            timeline = _effective_temp_timeline(nodes)
            thr = thresholds.get(ent, 19)
            for m in range(0, 1440, 15):
                t = _temp_at(timeline, m)
                if t is not None and t > thr:
                    active[m].append(ent)

        # automation windows
        for auto in rule.get("automations") or []:
            name = auto.get("name")
            dur = int(auto.get("duration_minutes") or 45)
            auto_cfg = cfg.automations.get(name) or {}
            for trig in auto_cfg.get("triggers") or []:
                days = list(trig.get("weekday") or DAYS)
                if day not in days:
                    continue
                for _, clock in resolve_automation_trigger(cfg, str(trig["at"]), [day]):
                    start = parse_clock(clock)
                    for dm in range(0, dur, 15):
                        m = wrap_add(start, dm)
                        m = m - (m % 15)
                        if m in active:
                            active[m].append(f"automation:{name}")

        for m, ents in active.items():
            # unique
            uniq = list(dict.fromkeys(ents))
            if len(uniq) > 1:
                out.append(
                    Finding(
                        "user",
                        rid,
                        "error",
                        ",".join(uniq),
                        day,
                        f"overlap at {m // 60:02d}:{m % 60:02d}: {uniq}",
                    )
                )
                # one per day enough to avoid spam? report a few
                if sum(1 for f in out if f.day == day and f.rule_id == rid) >= 3:
                    break
    return out


def lint_forbid_node_keys(cfg: ScheduleConfig, rule: Dict[str, Any]) -> List[Finding]:
    """Reject forbidden keys on entity nodes.

    Args:
        cfg: Config.
        rule: Rule dict.
    Returns:
        Findings.
    """
    rid = rule.get("id", "forbid_node_keys")
    keys = set(rule.get("keys") or [])
    out: List[Finding] = []
    for ent in rule.get("entities") or []:
        if ent not in cfg.schedules:
            continue
        sched = cfg.schedules[ent]
        for section in ("weekday", "weekend", "all_days", *DAYS):
            for raw in sched.get(section) or []:
                for k in keys:
                    if k in raw and raw[k] is not None:
                        out.append(
                            Finding(
                                "user",
                                rid,
                                rule.get("severity", "error"),
                                ent,
                                section,
                                f"forbidden key {k}={raw[k]!r}",
                            )
                        )
    return out


BUILTIN_LINTERS: Dict[str, Callable[..., List[Finding]]] = {
    "grid_15min": lint_grid_15min,
    "unique_times": lint_unique_times,
    "temp_bounds": lint_temp_bounds,
    "temp_step_multiple": lint_temp_step_multiple,
    "noop_nodes": lint_noop_nodes,
    "cycle_closed": lint_cycle_closed,
    "ramp_geometry": lint_ramp_geometry,
}


def validate(cfg: ScheduleConfig, *, strict: bool = False, profile: Optional[str] = None) -> List[Finding]:
    """Run all linters.

    Args:
        cfg: Config.
        strict: Promote warn to error for exit status.
        profile: Optional profile to include.
    Returns:
        All findings.
    """
    disabled = _disabled(cfg)
    findings: List[Finding] = []
    keys = schedule_keys(cfg, profile)

    for key in keys:
        for day in DAYS:
            try:
                nodes = resolve_day_nodes(cfg, key, day, profile=profile)
            except Exception as exc:
                findings.append(
                    Finding("builtin", "resolve", "error", key, day, str(exc))
                )
                continue
            for lid, fn in BUILTIN_LINTERS.items():
                if lid in disabled:
                    continue
                findings.extend(fn(cfg, key, day, nodes))
        if "midnight_transition" not in disabled:
            try:
                findings.extend(lint_midnight_transition(cfg, key))
            except Exception as exc:
                findings.append(
                    Finding("builtin", "midnight_transition", "error", key, "all", str(exc))
                )

    if "rule_threshold_sanity" not in disabled:
        findings.extend(lint_rule_threshold_sanity(cfg))

    for rule in cfg.rules.get("user") or []:
        rtype = rule.get("type")
        if rtype not in USER_TYPES:
            findings.append(
                Finding(
                    "user",
                    rule.get("id", "?"),
                    "error",
                    "-",
                    "-",
                    f"unknown rule type {rtype!r}",
                )
            )
            continue
        if rtype == "max_step_up":
            findings.extend(lint_max_step_up(cfg, rule))
        elif rtype == "max_temp_outside":
            findings.extend(lint_max_temp_outside(cfg, rule))
        elif rtype == "no_overlap_above":
            findings.extend(lint_no_overlap_above(cfg, rule))
        elif rtype == "forbid_node_keys":
            findings.extend(lint_forbid_node_keys(cfg, rule))

    if strict:
        for f in findings:
            if f.severity == "warn":
                f.severity = "error"
    return findings


def exit_code(findings: Sequence[Finding]) -> int:
    """Compute process exit code from findings.

    Args:
        findings: Findings list.
    Returns:
        0 if no errors else 1.
    """
    return 1 if any(f.severity == "error" for f in findings) else 0
