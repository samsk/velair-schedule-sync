"""Climate Scheduler service helpers matching live API v1.15.1."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from .ha import HaClient


def schedule_id_for(key: str) -> str:
    """Map yaml schedule key to default schedule_id.

    Args:
        key: climate.* or group:Name.
    Returns:
        schedule_id string (entity or group name).
    """
    if key.startswith("group:"):
        return key.split(":", 1)[1]
    return key


def profile_name_for(key: str) -> str:
    """Unique global profile name for a schedule key.

    Climate Scheduler stores profiles globally; each circuit needs its own
    active profile or they all share Default.

    Args:
        key: climate.* or group:Name.
    Returns:
        Profile name like velair_hc1.
    """
    sid = schedule_id_for(key)
    slug = re.sub(r"[^a-z0-9]+", "_", sid.lower()).strip("_")
    if slug.startswith("climate_"):
        slug = slug[len("climate_") :]
    return f"velair_{slug}"


def unwrap_response(data: Any) -> Any:
    """Unwrap HA service_response envelope.

    Args:
        data: Raw POST /api/services response.
    Returns:
        Inner service_response or data.
    """
    if isinstance(data, dict) and "service_response" in data:
        return data["service_response"]
    if isinstance(data, list) and data and isinstance(data[0], dict):
        if "service_response" in data[0]:
            return data[0]["service_response"]
    return data


def get_groups_detail(client: HaClient) -> Dict[str, Any]:
    """Fetch full groups map.

    Args:
        client: HA client.
    Returns:
        Groups dict.
    """
    raw = client.call_service(
        "climate_scheduler", "get_groups", {}, write=False, return_response=True
    )
    data = unwrap_response(raw)
    if isinstance(data, dict) and "groups" in data:
        return data["groups"] or {}
    return data if isinstance(data, dict) else {}


def resolve_group_name(client: HaClient, key: str) -> str:
    """Resolve yaml key to Climate Scheduler group name.

    Args:
        client: HA client.
        key: climate.* or group:Name.
    Returns:
        Group name used as schedule_id.
    """
    if key.startswith("group:"):
        return key.split(":", 1)[1]
    groups = get_groups_detail(client)
    for name, gdef in groups.items():
        ents = (gdef or {}).get("entities") or []
        if key in ents:
            return name
    # fallback: entity id itself
    return key


def list_groups(client: HaClient) -> List[str]:
    """List group names.

    Args:
        client: HA client.
    Returns:
        Group name list.
    """
    raw = client.call_service(
        "climate_scheduler", "list_groups", {}, write=False, return_response=True
    )
    data = unwrap_response(raw)
    if isinstance(data, dict) and "groups" in data:
        data = data["groups"]
    if isinstance(data, list):
        out = []
        for item in data:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                out.append(item.get("name") or item.get("schedule_id") or "")
        return [x for x in out if x]
    if isinstance(data, dict):
        return list(data.keys())
    return []


def get_schedule(client: HaClient, key: str) -> Any:
    """Fetch schedule for entity or group.

    Args:
        client: HA client.
        key: climate.* or group:Name.
    Returns:
        Schedule payload.
    """
    sid = resolve_group_name(client, key)
    raw = client.call_service(
        "climate_scheduler",
        "get_schedule",
        {"schedule_id": sid},
        write=False,
        return_response=True,
    )
    return unwrap_response(raw)


def create_group(client: HaClient, name: str, entities: List[str]) -> None:
    """Create group and add entities.

    Args:
        client: HA client.
        name: Group name.
        entities: Climate entity ids.
    """
    client.call_service(
        "climate_scheduler", "create_group", {"schedule_id": name}, write=True
    )
    for ent in entities:
        client.call_service(
            "climate_scheduler",
            "add_to_group",
            {"schedule_id": name, "entity_id": ent},
            write=True,
        )


def ensure_profile(client: HaClient, group_name: str, profile: str) -> None:
    """Create profile if needed and set it active on the group.

    Args:
        client: HA client.
        group_name: CS group name.
        profile: Unique profile name.
    """
    client.call_service(
        "climate_scheduler",
        "create_profile",
        {"schedule_id": group_name, "profile_name": profile},
        write=True,
    )
    client.call_service(
        "climate_scheduler",
        "set_active_profile",
        {"schedule_id": group_name, "profile_name": profile},
        write=True,
    )


def set_day_schedule(
    client: HaClient,
    key: str,
    day: str,
    nodes: List[Dict[str, Any]],
    mode: str,
    *,
    is_group: bool,
    profile: Optional[str] = None,
) -> None:
    """Push one day of nodes to the group's active profile.

    Args:
        client: HA client.
        key: Schedule key.
        day: Day label.
        nodes: Node list.
        mode: schedule_mode.
        is_group: Unused (always group path in CS 1.15).
        profile: Optional unique profile; created/activated when set.
    """
    group_name = resolve_group_name(client, key)
    prof = profile or profile_name_for(key)
    ensure_profile(client, group_name, prof)
    # Prefer set_group_schedule; nodes as JSON string per service schema
    client.call_service(
        "climate_scheduler",
        "set_group_schedule",
        {
            "schedule_id": group_name,
            "day": day,
            "nodes": json.dumps(nodes),
            "schedule_mode": mode,
        },
        write=True,
    )


def enable_schedule(client: HaClient, key: str, *, is_group: bool) -> None:
    """Enable schedule or group.

    Args:
        client: HA client.
        key: Schedule key.
        is_group: Group vs entity (both use enable_group for CS groups).
    """
    group_name = resolve_group_name(client, key)
    client.call_service(
        "climate_scheduler",
        "enable_group",
        {"schedule_id": group_name},
        write=True,
    )


def sync_all(client: HaClient) -> None:
    """Force sync all.

    Args:
        client: HA client.
    """
    client.call_service("climate_scheduler", "sync_all", {}, write=True)


def parse_schedule_nodes(payload: Any) -> Dict[str, List[Dict[str, Any]]]:
    """Normalize get_schedule / group schedules to day->nodes.

    Args:
        payload: Service response.
    Returns:
        Day map with node lists.
    """
    if not isinstance(payload, dict):
        return {}
    schedules = payload.get("schedules") or payload.get("schedule") or {}
    if not isinstance(schedules, dict):
        return {}
    out: Dict[str, List[Dict[str, Any]]] = {}
    for day, nodes in schedules.items():
        if isinstance(nodes, str):
            try:
                nodes = json.loads(nodes)
            except json.JSONDecodeError:
                continue
        if isinstance(nodes, list):
            out[day] = nodes
    return out
