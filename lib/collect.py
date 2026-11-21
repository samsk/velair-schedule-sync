"""Config-driven statistic and history aggregation for collect."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .config import ScheduleConfig, Specifier, resolve_alias
from .history import Sample, parse_history, value_at
from .timeutil import DAYS, parse_clock

AGGS = {"state_sum", "change_sum", "delta", "mean", "state_mean"}
HISTORY_AGGS = {"mean", "min", "max", "hours_at_or_above"}
Interval = Tuple[datetime, datetime]


def stat_ids(collect: Dict[str, Any]) -> List[str]:
    """Unique statistic ids from collect.metrics.

    Args:
        collect: Top-level collect block.
    Returns:
        Deduplicated statistic id list.
    """
    out: List[str] = []
    seen = set()
    for spec in (collect.get("metrics") or {}).values():
        if not isinstance(spec, dict):
            continue
        sid = spec.get("stat")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        out.append(str(sid))
    return out


def history_entity_ids(collect: Dict[str, Any]) -> List[str]:
    """Unique entity ids from collect.history_metrics.

    Args:
        collect: Top-level collect block.
    Returns:
        Deduplicated entity ids.
    """
    out: List[str] = []
    seen = set()
    for spec in (collect.get("history_metrics") or {}).values():
        if not isinstance(spec, dict):
            continue
        eid = spec.get("entity")
        if not eid or eid in seen:
            continue
        seen.add(eid)
        out.append(str(eid))
    return out


def aggregate(rows: Sequence[Dict[str, Any]], agg: str) -> Optional[float]:
    """Aggregate statistic rows by named method.

    Args:
        rows: Recorder statistic rows.
        agg: One of AGGS.
    Returns:
        Aggregated float, or None if empty / unknown.
    """
    if agg == "state_sum":
        vals = [float(r["state"]) for r in rows if r.get("state") is not None]
        return float(sum(vals)) if vals else None
    if agg == "change_sum":
        total = 0.0
        found = False
        for row in rows:
            ch = row.get("change")
            if ch is not None:
                total += float(ch)
                found = True
        return total if found else None
    if agg == "delta":
        vals = [r.get("state") for r in rows if r.get("state") is not None]
        if len(vals) >= 2:
            return float(vals[-1]) - float(vals[0])
        changes = [r.get("change") for r in rows if r.get("change") is not None]
        if changes:
            return float(sum(changes))
        return None
    if agg == "mean":
        vals = [float(r["mean"]) for r in rows if r.get("mean") is not None]
        return float(sum(vals) / len(vals)) if vals else None
    if agg == "state_mean":
        vals = [float(r["state"]) for r in rows if r.get("state") is not None]
        return float(sum(vals) / len(vals)) if vals else None
    return None


def _den_value(results: Dict[str, Optional[float]], den: Any) -> Optional[float]:
    """Resolve ratio denominator from a metric name or list of names.

    Args:
        results: Metric results so far.
        den: Metric name or list of names.
    Returns:
        Denominator value, or None if any part missing.
    """
    if isinstance(den, list):
        total = 0.0
        for name in den:
            val = results.get(str(name))
            if val is None:
                return None
            total += float(val)
        return total
    val = results.get(str(den))
    return None if val is None else float(val)


def compute_results(
    collect: Dict[str, Any], stats: Dict[str, Any]
) -> Dict[str, Optional[float]]:
    """Compute metrics then ratios from statistic payload.

    Args:
        collect: Top-level collect block.
        stats: Map of statistic_id -> rows.
    Returns:
        Results map (metrics + ratios). Missing/zero den -> None.
    """
    results: Dict[str, Optional[float]] = {}
    for name, spec in (collect.get("metrics") or {}).items():
        if not isinstance(spec, dict):
            results[str(name)] = None
            continue
        sid = str(spec.get("stat") or "")
        agg = str(spec.get("agg") or "")
        rows = stats.get(sid) or []
        val = aggregate(rows, agg)
        results[str(name)] = None if val is None else round(float(val), 3)

    for name, spec in (collect.get("ratios") or {}).items():
        if not isinstance(spec, dict):
            results[str(name)] = None
            continue
        num = results.get(str(spec.get("num")))
        den = _den_value(results, spec.get("den"))
        if num is None or den is None or den == 0:
            results[str(name)] = None
        else:
            results[str(name)] = round(float(num) / float(den), 3)
    return results


def _day_token(d: date) -> str:
    """Map calendar date to mon..sun.

    Args:
        d: Calendar date.
    Returns:
        Day token.
    """
    return DAYS[d.weekday()]


def specifier_intervals(
    cfg: ScheduleConfig,
    name: str,
    since: date,
    until: date,
    tz: Any,
) -> List[Interval]:
    """Expand one specifier into local datetime intervals.

    Args:
        cfg: Config with specifiers.
        name: Specifier name.
        since: Inclusive start date.
        until: Inclusive end date.
        tz: Local timezone.
    Returns:
        List of [start, end) aware intervals.
    """
    spec: Specifier = resolve_alias(cfg.specifiers, name)
    start_m = parse_clock(spec.start)
    end_m = parse_clock(spec.end)
    out: List[Interval] = []
    d = since
    while d <= until:
        if _day_token(d) in spec.days:
            start = datetime.combine(
                d, time(start_m // 60, start_m % 60), tzinfo=tz
            )
            if end_m > start_m:
                end = datetime.combine(
                    d, time(end_m // 60, end_m % 60), tzinfo=tz
                )
            elif end_m == 0 and start_m == 0:
                end = start
            else:
                # Crosses midnight onto the next calendar day.
                end = datetime.combine(
                    d + timedelta(days=1),
                    time(end_m // 60, end_m % 60),
                    tzinfo=tz,
                )
            if end > start:
                out.append((start, end))
        d += timedelta(days=1)
    return out


def clip_intervals(
    intervals: Sequence[Interval], bound_start: datetime, bound_end: datetime
) -> List[Interval]:
    """Clip intervals to an overall collect bound.

    Args:
        intervals: Source intervals.
        bound_start: Inclusive lower bound.
        bound_end: Exclusive/upper bound.
    Returns:
        Clipped non-empty intervals.
    """
    out: List[Interval] = []
    for start, end in intervals:
        left = max(start, bound_start)
        right = min(end, bound_end)
        if right > left:
            out.append((left, right))
    return out


def _segments(
    samples: Sequence[Sample], start: datetime, end: datetime
) -> Iterable[Tuple[datetime, datetime, float]]:
    """Yield value segments inside [start, end).

    Args:
        samples: Sorted history samples.
        start: Interval start.
        end: Interval end.
    Yields:
        (seg_start, seg_end, value) triples.
    """
    if end <= start:
        return
    points = [s for s in samples if s.actual is not None]
    if not points:
        return
    # Seed with last known value at/before start, else first in-window sample.
    seed = value_at(points, start)
    if seed is None or seed.actual is None:
        first = next((s for s in points if start < s.when < end), None)
        if first is None:
            return
        cursor = first.when
        value = float(first.actual)
        rest = [s for s in points if s.when > first.when]
    else:
        cursor = start
        value = float(seed.actual)
        rest = [s for s in points if s.when > start]
    for sample in rest:
        if sample.when >= end:
            break
        if sample.when > cursor:
            yield cursor, sample.when, value
        cursor = sample.when
        value = float(sample.actual)
    if cursor < end:
        yield cursor, end, value


def aggregate_history(
    samples: Sequence[Sample],
    intervals: Sequence[Interval],
    agg: str,
    *,
    threshold: Optional[float] = None,
) -> Optional[float]:
    """Aggregate history samples over intervals.

    Args:
        samples: Sorted samples.
        intervals: Time windows.
        agg: One of HISTORY_AGGS.
        threshold: Required for hours_at_or_above.
    Returns:
        Aggregated float or None.
    """
    if not intervals:
        return None
    weighted = 0.0
    weight = 0.0
    vmin: Optional[float] = None
    vmax: Optional[float] = None
    hours_above = 0.0
    any_seg = False
    for start, end in intervals:
        for left, right, value in _segments(samples, start, end):
            dur = (right - left).total_seconds()
            if dur <= 0:
                continue
            any_seg = True
            weighted += value * dur
            weight += dur
            vmin = value if vmin is None else min(vmin, value)
            vmax = value if vmax is None else max(vmax, value)
            if threshold is not None and value >= threshold:
                hours_above += dur / 3600.0
    if not any_seg:
        return None
    if agg == "mean":
        return weighted / weight if weight else None
    if agg == "min":
        return vmin
    if agg == "max":
        return vmax
    if agg == "hours_at_or_above":
        return hours_above
    return None


def compute_history_results(
    cfg: ScheduleConfig,
    collect: Dict[str, Any],
    histories: Dict[str, List[Dict[str, Any]]],
    *,
    since: date,
    until: date,
    bound_start: datetime,
    bound_end: datetime,
    tz: Any,
) -> Dict[str, Optional[float]]:
    """Compute history_metrics from raw HA history payloads.

    Args:
        cfg: Config for specifier windows.
        collect: Collect block.
        histories: entity_id -> raw history rows.
        since: Inclusive start date.
        until: Inclusive end date.
        bound_start: Clip start.
        bound_end: Clip end.
        tz: Local timezone.
    Returns:
        History metric results.
    """
    results: Dict[str, Optional[float]] = {}
    for name, spec in (collect.get("history_metrics") or {}).items():
        if not isinstance(spec, dict):
            results[str(name)] = None
            continue
        eid = str(spec.get("entity") or "")
        agg = str(spec.get("agg") or "")
        windows = list(spec.get("windows") or [])
        threshold = spec.get("threshold")
        thr = float(threshold) if threshold is not None else None
        intervals: List[Interval] = []
        if windows:
            for win in windows:
                intervals.extend(specifier_intervals(cfg, str(win), since, until, tz))
        else:
            intervals = [(bound_start, bound_end)]
        intervals = clip_intervals(intervals, bound_start, bound_end)
        samples = parse_history(
            histories.get(eid) or [],
            actual_attr=str(spec.get("attr") or "state"),
            setpt_attr="temperature",
        )
        val = aggregate_history(samples, intervals, agg, threshold=thr)
        results[str(name)] = None if val is None else round(float(val), 3)
    return results


def check_collect(
    collect: Dict[str, Any],
    *,
    known_specifiers: Optional[Sequence[str]] = None,
) -> List[str]:
    """Validate collect block schema.

    Args:
        collect: Top-level collect block (may be empty).
        known_specifiers: Specifier names for window checks.
    Returns:
        Human-readable error strings (empty if ok).
    """
    if not collect:
        return []
    errors: List[str] = []
    metrics = collect.get("metrics") or {}
    if metrics and not isinstance(metrics, dict):
        return ["collect.metrics must be a mapping"]
    for name, spec in (metrics or {}).items():
        if not isinstance(spec, dict):
            errors.append(f"collect.metrics.{name}: must be a mapping")
            continue
        if not spec.get("stat"):
            errors.append(f"collect.metrics.{name}: missing stat")
        agg = spec.get("agg")
        if agg not in AGGS:
            errors.append(f"collect.metrics.{name}: unknown agg {agg!r}")

    history = collect.get("history_metrics") or {}
    if history and not isinstance(history, dict):
        errors.append("collect.history_metrics must be a mapping")
        history = {}
    known = set(known_specifiers or [])
    for name, spec in history.items():
        if not isinstance(spec, dict):
            errors.append(f"collect.history_metrics.{name}: must be a mapping")
            continue
        if not spec.get("entity"):
            errors.append(f"collect.history_metrics.{name}: missing entity")
        agg = spec.get("agg")
        if agg not in HISTORY_AGGS:
            errors.append(f"collect.history_metrics.{name}: unknown agg {agg!r}")
        if agg == "hours_at_or_above" and spec.get("threshold") is None:
            errors.append(
                f"collect.history_metrics.{name}: hours_at_or_above needs threshold"
            )
        windows = spec.get("windows")
        if windows is None:
            continue
        if not isinstance(windows, list) or not windows:
            errors.append(f"collect.history_metrics.{name}: windows must be a list")
            continue
        for win in windows:
            if known and str(win) not in known:
                errors.append(
                    f"collect.history_metrics.{name}: unknown window {win!r}"
                )

    metric_names = set(metrics or {})
    ratios = collect.get("ratios") or {}
    if ratios and not isinstance(ratios, dict):
        errors.append("collect.ratios must be a mapping")
        return errors
    for name, spec in (ratios or {}).items():
        if not isinstance(spec, dict):
            errors.append(f"collect.ratios.{name}: must be a mapping")
            continue
        num = spec.get("num")
        if num not in metric_names:
            errors.append(f"collect.ratios.{name}: unknown num {num!r}")
        den = spec.get("den")
        dens = den if isinstance(den, list) else [den]
        for d in dens:
            if d not in metric_names:
                errors.append(f"collect.ratios.{name}: unknown den {d!r}")
    return errors
