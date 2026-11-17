"""Config-driven statistic aggregation for collect."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

AGGS = {"state_sum", "change_sum", "delta", "mean", "state_mean"}


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


def check_collect(collect: Dict[str, Any]) -> List[str]:
    """Validate collect block schema.

    Args:
        collect: Top-level collect block (may be empty).
    Returns:
        Human-readable error strings (empty if ok).
    """
    if not collect:
        return []
    errors: List[str] = []
    metrics = collect.get("metrics") or {}
    if not isinstance(metrics, dict):
        return ["collect.metrics must be a mapping"]
    for name, spec in metrics.items():
        if not isinstance(spec, dict):
            errors.append(f"collect.metrics.{name}: must be a mapping")
            continue
        if not spec.get("stat"):
            errors.append(f"collect.metrics.{name}: missing stat")
        agg = spec.get("agg")
        if agg not in AGGS:
            errors.append(f"collect.metrics.{name}: unknown agg {agg!r}")
    metric_names = set(metrics)
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
