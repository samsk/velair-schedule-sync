"""Unit tests for config-driven collect aggregation."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from lib.collect import (
    aggregate,
    aggregate_history,
    check_collect,
    clip_intervals,
    compute_history_results,
    compute_results,
    history_entity_ids,
    specifier_intervals,
    stat_ids,
)
from lib.config import load_config
from lib.history import Sample

TZ = ZoneInfo("Europe/Bratislava")


def test_aggregate_methods() -> None:
    """Each agg mode returns expected value."""
    rows = [
        {"state": 1.0, "change": 1.0, "mean": 2.0},
        {"state": 3.0, "change": 2.0, "mean": 4.0},
    ]
    assert aggregate(rows, "state_sum") == 4.0
    assert aggregate(rows, "change_sum") == 3.0
    assert aggregate(rows, "delta") == 2.0
    assert aggregate(rows, "mean") == 3.0
    assert aggregate(rows, "state_mean") == 2.0
    assert aggregate([], "state_sum") is None
    assert aggregate(rows, "unknown") is None


def test_stat_ids_dedupe() -> None:
    """stat_ids returns unique stats in order."""
    collect = {
        "metrics": {
            "a": {"stat": "sensor.a", "agg": "state_sum"},
            "b": {"stat": "sensor.b", "agg": "mean"},
            "c": {"stat": "sensor.a", "agg": "delta"},
        }
    }
    assert stat_ids(collect) == ["sensor.a", "sensor.b"]


def test_history_entity_ids_dedupe() -> None:
    """history_entity_ids returns unique entities."""
    collect = {
        "history_metrics": {
            "m1": {"entity": "sensor.mod", "agg": "mean"},
            "m2": {"entity": "sensor.out", "agg": "max"},
            "m3": {"entity": "sensor.mod", "agg": "min"},
        }
    }
    assert history_entity_ids(collect) == ["sensor.mod", "sensor.out"]


def test_compute_results_ratio_list_den() -> None:
    """Ratio with list den sums metric parts."""
    collect = {
        "metrics": {
            "lt": {"stat": "s.lt", "agg": "delta"},
            "ht": {"stat": "s.ht", "agg": "delta"},
            "prod": {"stat": "s.prod", "agg": "state_sum"},
            "cons": {"stat": "s.cons", "agg": "state_sum"},
        },
        "ratios": {
            "lt_share": {"num": "lt", "den": ["lt", "ht"]},
            "cop": {"num": "prod", "den": "cons"},
        },
    }
    stats = {
        "s.lt": [{"state": 10.0}, {"state": 20.0}],
        "s.ht": [{"state": 0.0}, {"state": 5.0}],
        "s.prod": [{"state": 9.0}],
        "s.cons": [{"state": 3.0}],
    }
    out = compute_results(collect, stats)
    assert out["lt"] == 10.0
    assert out["ht"] == 5.0
    assert out["lt_share"] == 0.667
    assert out["cop"] == 3.0


def test_compute_results_zero_or_missing() -> None:
    """Zero or missing den yields None."""
    collect = {
        "metrics": {
            "prod": {"stat": "s.prod", "agg": "state_sum"},
            "cons": {"stat": "s.cons", "agg": "state_sum"},
        },
        "ratios": {"cop": {"num": "prod", "den": "cons"}},
    }
    assert compute_results(collect, {"s.prod": [{"state": 1}], "s.cons": []})["cop"] is None
    assert compute_results(
        collect, {"s.prod": [{"state": 1}], "s.cons": [{"state": 0}]}
    )["cop"] is None


def test_check_collect_errors() -> None:
    """Schema errors for bad agg and ratio refs."""
    errs = check_collect(
        {
            "metrics": {
                "a": {"stat": "s.a", "agg": "nope"},
                "b": {"agg": "state_sum"},
            },
            "ratios": {"r": {"num": "missing", "den": "a"}},
        }
    )
    assert any("unknown agg" in e for e in errs)
    assert any("missing stat" in e for e in errs)
    assert any("unknown num" in e for e in errs)


def test_check_collect_history_errors() -> None:
    """Schema errors for history_metrics windows and threshold."""
    errs = check_collect(
        {
            "history_metrics": {
                "mod": {"entity": "sensor.mod", "agg": "hours_at_or_above"},
                "out": {
                    "entity": "sensor.out",
                    "agg": "mean",
                    "windows": ["missing_win"],
                },
                "bad": {"agg": "mean"},
            }
        },
        known_specifiers=["LT_weekend_morning"],
    )
    assert any("needs threshold" in e for e in errs)
    assert any("unknown window" in e for e in errs)
    assert any("missing entity" in e for e in errs)


def _sample(stamp: str, value: float) -> Sample:
    """Build a numeric history sample.

    Args:
        stamp: ISO timestamp.
        value: Sensor value.
    Returns:
        Sample.
    """
    when = datetime.fromisoformat(stamp)
    return Sample(when=when, state=str(value), actual=value, setpt=None, attrs={})


def test_aggregate_history_mean_min_max_hours() -> None:
    """Time-weighted aggs over clipped windows."""
    samples = [
        _sample("2026-11-21T02:00:00+01:00", 0.0),
        _sample("2026-11-21T03:00:00+01:00", 50.0),
        _sample("2026-11-21T04:00:00+01:00", 100.0),
        _sample("2026-11-21T05:00:00+01:00", 100.0),
    ]
    intervals = [
        (
            datetime(2026, 11, 21, 2, 30, tzinfo=TZ),
            datetime(2026, 11, 21, 4, 30, tzinfo=TZ),
        )
    ]
    # 02:30-03:00 @0 (0.5h), 03:00-04:00 @50 (1h), 04:00-04:30 @100 (0.5h)
    mean = aggregate_history(samples, intervals, "mean")
    assert mean is not None
    assert abs(mean - 50.0) < 1e-6
    assert aggregate_history(samples, intervals, "min") == 0.0
    assert aggregate_history(samples, intervals, "max") == 100.0
    hours = aggregate_history(
        samples, intervals, "hours_at_or_above", threshold=95.0
    )
    assert hours is not None
    assert abs(hours - 0.5) < 1e-6


def test_clip_and_specifier_intervals(tmp_path: Path) -> None:
    """Specifier windows expand and clip to collect bounds."""
    path = tmp_path / "cfg.yaml"
    path.write_text(
        "\n".join(
            [
                "version: 1",
                "specifiers:",
                "  weekend_LT_early:",
                "    days: [sat, sun]",
                "    start: '02:30'",
                "    end: '04:00'",
                "schedules: {}",
                "rules: {disable: null, user: []}",
            ]
        ),
        encoding="utf-8",
    )
    cfg = load_config(path)
    intervals = specifier_intervals(
        cfg, "weekend_LT_early", date(2026, 11, 21), date(2026, 11, 22), TZ
    )
    assert len(intervals) == 2
    assert intervals[0][0] == datetime(2026, 11, 21, 2, 30, tzinfo=TZ)
    clipped = clip_intervals(
        intervals,
        datetime(2026, 11, 21, 3, 0, tzinfo=TZ),
        datetime(2026, 11, 21, 12, 0, tzinfo=TZ),
    )
    assert clipped == [
        (
            datetime(2026, 11, 21, 3, 0, tzinfo=TZ),
            datetime(2026, 11, 21, 4, 0, tzinfo=TZ),
        )
    ]


def test_compute_history_results(tmp_path: Path) -> None:
    """compute_history_results merges windowed metrics."""
    path = tmp_path / "cfg.yaml"
    path.write_text(
        "\n".join(
            [
                "version: 1",
                "specifiers:",
                "  LT_window:",
                "    days: [sat]",
                "    start: '02:30'",
                "    end: '08:15'",
                "schedules: {}",
                "rules: {disable: null, user: []}",
            ]
        ),
        encoding="utf-8",
    )
    cfg = load_config(path)
    collect = {
        "history_metrics": {
            "mod_mean": {
                "entity": "sensor.mod",
                "agg": "mean",
                "windows": ["LT_window"],
            },
            "mod_high_h": {
                "entity": "sensor.mod",
                "agg": "hours_at_or_above",
                "threshold": 95,
                "windows": ["LT_window"],
            },
        }
    }
    histories = {
        "sensor.mod": [
            {
                "entity_id": "sensor.mod",
                "state": "0",
                "attributes": {},
                "last_changed": "2026-11-21T02:00:00+01:00",
                "last_updated": "2026-11-21T02:00:00+01:00",
            },
            {
                "entity_id": "sensor.mod",
                "state": "99",
                "attributes": {},
                "last_changed": "2026-11-21T05:30:00+01:00",
                "last_updated": "2026-11-21T05:30:00+01:00",
            },
        ]
    }
    out = compute_history_results(
        cfg,
        collect,
        histories,
        since=date(2026, 11, 21),
        until=date(2026, 11, 21),
        bound_start=datetime(2026, 11, 21, 0, 0, tzinfo=TZ),
        bound_end=datetime(2026, 11, 21, 12, 0, tzinfo=TZ),
        tz=TZ,
    )
    assert out["mod_mean"] is not None
    assert out["mod_high_h"] is not None
    # 02:30-05:30 @0 (3h) + 05:30-08:15 @99 (2.75h)
    assert abs(out["mod_high_h"] - 2.75) < 1e-6
