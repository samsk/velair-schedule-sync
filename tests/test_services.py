"""HA services payload parsing."""

from __future__ import annotations

from sync import _service_names, _services_by_domain


def test_service_names_from_dict() -> None:
    """Live HA returns services as a name -> schema dict."""
    assert _service_names({"set_daily_schedule": {}, "apply_schedule": {}}) == {
        "set_daily_schedule",
        "apply_schedule",
    }


def test_service_names_from_list() -> None:
    """Legacy list shape still works."""
    assert _service_names([{"service": "get_schedule"}, "sync_all"]) == {
        "get_schedule",
        "sync_all",
    }


def test_services_by_domain() -> None:
    """Domain map uses dict service names."""
    payload = [
        {
            "domain": "velair",
            "services": {
                "apply_schedule": {},
                "set_daily_schedule": {},
                "activate_profile": {},
            },
        },
        {"domain": "switch", "services": {"turn_off": {}, "turn_on": {}}},
    ]
    by_domain = _services_by_domain(payload)
    assert "set_daily_schedule" in by_domain["velair"]
    assert "turn_off" in by_domain["switch"]
    assert _services_by_domain({}) == {}
