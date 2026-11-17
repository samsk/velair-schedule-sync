"""Unit tests for config-driven collect aggregation."""

from __future__ import annotations

from lib.collect import aggregate, check_collect, compute_results, stat_ids


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
