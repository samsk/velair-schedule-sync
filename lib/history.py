"""Schedule versus recorder history comparison helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any, Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo

from .config import ScheduleConfig
from .resolve import resolve_day_nodes

DEFAULT_ACTUAL_ATTR = "current_temperature"
DEFAULT_SETPT_ATTR = "temperature"


@dataclass(frozen=True)
class Sample:
    """One history point.

    Attributes:
        when: Sample timestamp (aware).
        state: Entity state string.
        actual: Room/actual temperature when present.
        setpt: Live setpoint when present.
        attrs: Raw attributes.
    """

    when: datetime
    state: str
    actual: Optional[float]
    setpt: Optional[float]
    attrs: Dict[str, Any]


@dataclass(frozen=True)
class CompareRow:
    """One schedule clock compared to history.

    Attributes:
        clock: Resolved HH:MM.
        sched_temp: Desired schedule temperature.
        live_setpt: Live setpoint from history.
        actual: Actual/room temperature from history.
        delta: actual minus sched_temp when both present.
    """

    clock: str
    sched_temp: float
    live_setpt: Optional[float]
    actual: Optional[float]
    delta: Optional[float]


def compare_override(
    cfg: ScheduleConfig, entity_key: str
) -> Dict[str, Any]:
    """Optional collect.compare override for one schedule key.

    Args:
        cfg: Config.
        entity_key: Schedule entity or group key.
    Returns:
        Override mapping (may be empty).
    """
    block = (cfg.collect or {}).get("compare") or {}
    if not isinstance(block, dict):
        return {}
    raw = block.get(entity_key)
    return dict(raw) if isinstance(raw, dict) else {}


def history_entity_id(cfg: ScheduleConfig, entity_key: str) -> str:
    """Entity id to fetch history for.

    Args:
        cfg: Config.
        entity_key: Schedule key.
    Returns:
        History entity id.
    """
    override = compare_override(cfg, entity_key)
    if override.get("entity"):
        return str(override["entity"])
    if entity_key.startswith("group:"):
        name = entity_key.split(":", 1)[1]
        members = (cfg.groups.get(name) or {}).get("entities") or []
        if members:
            return str(members[0])
    return entity_key


def _as_float(value: Any) -> Optional[float]:
    """Parse a numeric attribute.

    Args:
        value: Raw attribute.
    Returns:
        Float or None.
    """
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_history(
    rows: Sequence[Dict[str, Any]],
    *,
    actual_attr: str = DEFAULT_ACTUAL_ATTR,
    setpt_attr: str = DEFAULT_SETPT_ATTR,
) -> List[Sample]:
    """Normalize HA history rows into samples.

    Args:
        rows: History state dicts.
        actual_attr: Attribute for actual temperature.
        setpt_attr: Attribute for live setpoint.
    Returns:
        Samples sorted by time.
    """
    out: List[Sample] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        stamp = row.get("last_updated") or row.get("last_changed")
        if not stamp:
            continue
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        attrs = row.get("attributes") or {}
        if not isinstance(attrs, dict):
            attrs = {}
        # Plain sensors store the value in state.
        actual = _as_float(attrs.get(actual_attr))
        if actual is None and actual_attr == "state":
            actual = _as_float(row.get("state"))
        elif actual is None and not str(row.get("entity_id") or "").startswith(
            "climate."
        ):
            actual = _as_float(row.get("state"))
        out.append(
            Sample(
                when=when,
                state=str(row.get("state") or ""),
                actual=actual,
                setpt=_as_float(attrs.get(setpt_attr)),
                attrs=dict(attrs),
            )
        )
    out.sort(key=lambda s: s.when)
    return out


def value_at(samples: Sequence[Sample], when: datetime) -> Optional[Sample]:
    """Last sample at or before when.

    Args:
        samples: Sorted samples.
        when: Target timestamp.
    Returns:
        Matching sample or None.
    """
    if when.tzinfo is None:
        raise ValueError("when must be timezone-aware")
    best: Optional[Sample] = None
    for sample in samples:
        if sample.when <= when:
            best = sample
        else:
            break
    return best


def local_tz(name: Optional[str] = None) -> Any:
    """Resolve local timezone.

    Args:
        name: Optional IANA zone name.
    Returns:
        tzinfo instance.
    """
    if name:
        return ZoneInfo(name)
    local = datetime.now().astimezone().tzinfo
    return local or timezone.utc


def day_window(
    day: date,
    *,
    tz: Any = None,
    end_now_if_today: bool = True,
) -> tuple[datetime, datetime]:
    """Local start/end bounds for a calendar day.

    Args:
        day: Calendar date.
        tz: Local zone.
        end_now_if_today: Clip end to now when day is today.
    Returns:
        Aware start and end datetimes.
    """
    zone = tz or local_tz()
    start = datetime.combine(day, time(0, 0), tzinfo=zone)
    end = datetime.combine(day, time(23, 59, 59), tzinfo=zone)
    now = datetime.now(zone)
    if end_now_if_today and day == now.date() and now < end:
        end = now
    return start, end


def compare_day(
    cfg: ScheduleConfig,
    entity_key: str,
    day: str,
    on_date: date,
    samples: Sequence[Sample],
    *,
    tz: Any = None,
) -> List[CompareRow]:
    """Compare resolved schedule nodes to history samples.

    Args:
        cfg: Config.
        entity_key: Schedule entity/group key.
        day: mon..sun.
        on_date: Calendar date for sampling.
        samples: Parsed history samples.
        tz: Local zone.
    Returns:
        Compare rows for nodes that set temp.
    """
    zone = tz or local_tz()
    nodes = resolve_day_nodes(cfg, entity_key, day)
    rows: List[CompareRow] = []
    for node in nodes:
        if node.temp is None:
            continue
        hour, minute = divmod(int(node.minutes), 60)
        when = datetime.combine(on_date, time(hour, minute), tzinfo=zone)
        sample = value_at(samples, when)
        actual = sample.actual if sample else None
        setpt = sample.setpt if sample else None
        delta = None if actual is None else round(actual - float(node.temp), 3)
        rows.append(
            CompareRow(
                clock=node.clock,
                sched_temp=float(node.temp),
                live_setpt=setpt,
                actual=actual,
                delta=delta,
            )
        )
    return rows


def format_compare_table(
    rows: Sequence[CompareRow], *, fmt: str = "ascii"
) -> str:
    """Render compare rows.

    Args:
        rows: Compare rows.
        fmt: ascii or csv.
    Returns:
        Table text including trailing newline.
    """
    if fmt == "csv":
        lines = ["clock,sched,setpt,actual,delta"]
        for row in rows:
            lines.append(
                ",".join(
                    [
                        row.clock,
                        f"{row.sched_temp:.1f}",
                        "" if row.live_setpt is None else f"{row.live_setpt:.1f}",
                        "" if row.actual is None else f"{row.actual:.1f}",
                        "" if row.delta is None else f"{row.delta:+.1f}",
                    ]
                )
            )
        return "\n".join(lines) + "\n"

    header = f"{'clock':>5}  {'sched':>5}  {'setpt':>5}  {'actual':>6}  {'delta':>6}"
    lines = [header]
    for row in rows:
        setpt = "—" if row.live_setpt is None else f"{row.live_setpt:5.1f}"
        actual = "—" if row.actual is None else f"{row.actual:6.1f}"
        delta = "—" if row.delta is None else f"{row.delta:+6.1f}"
        lines.append(
            f"{row.clock:>5}  {row.sched_temp:5.1f}  {setpt:>5}  {actual:>6}  {delta:>6}"
        )
    return "\n".join(lines) + "\n"


def parse_date(value: str) -> date:
    """Parse YYYY-MM-DD.

    Args:
        value: Date string.
    Returns:
        date.
    """
    return date.fromisoformat(value)
