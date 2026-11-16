"""Velair websocket schedule read and day writes."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .ha import HaClient

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday")
WEEKEND = ("saturday", "sunday")
ALL_DAYS = WEEKDAYS + WEEKEND
DAY_NAME = {
    "mon": "monday",
    "tue": "tuesday",
    "wed": "wednesday",
    "thu": "thursday",
    "fri": "friday",
    "sat": "saturday",
    "sun": "sunday",
}


def fetch_schedule(client: HaClient) -> Dict[str, Any]:
    """Read the full Velair schedule.

    Args:
        client: HA client.
    Returns:
        velair/get_schedule result.
    """
    data = client.ws_command("velair/get_schedule")
    if not isinstance(data, dict):
        raise RuntimeError(f"velair/get_schedule returned {type(data).__name__}")
    return data


def room_group_members(schedule: Dict[str, Any], name: str) -> Optional[List[str]]:
    """Members of a Velair room group with this name.

    Args:
        schedule: get_schedule result.
        name: Group name from yaml.
    Returns:
        Member entity ids, or None when the group is absent.
    """
    groups = schedule.get("room_groups") or {}
    items: List[Any]
    if isinstance(groups, dict):
        items = list(groups.values())
    elif isinstance(groups, list):
        items = groups
    else:
        return None
    for group in items:
        if not isinstance(group, dict):
            continue
        if group.get("name") != name and group.get("id") != name:
            continue
        members = group.get("members") or group.get("entities") or []
        return [str(m) for m in members]
    return None


def zone_schedule(schedule: Dict[str, Any], entity_id: str) -> Dict[str, List[Dict[str, Any]]]:
    """Saved blocks for one managed climate.

    Args:
        schedule: get_schedule result.
        entity_id: Climate entity id.
    Returns:
        Weekday -> block list. Empty when the zone has no days.
    """
    zones = schedule.get("zones") or {}
    zone = zones.get(entity_id) or {}
    raw = zone.get("schedule") or {}
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, List[Dict[str, Any]]] = {}
    for day, blocks in raw.items():
        out[str(day)] = list(blocks) if isinstance(blocks, list) else []
    return out


def days_for_label(label: str) -> List[str]:
    """Velair weekday names covered by a desired-schedule label.

    Args:
        label: weekday, weekend, all_days, or mon..sun.
    Returns:
        Full weekday names.
    """
    if label == "weekday":
        return list(WEEKDAYS)
    if label == "weekend":
        return list(WEEKEND)
    if label == "all_days":
        return list(ALL_DAYS)
    if label in DAY_NAME:
        return [DAY_NAME[label]]
    return [label]


def node_to_block(node: Dict[str, Any]) -> Dict[str, Any]:
    """Map one resolved node to a Velair schedule block.

    Args:
        node: {time, temp?, hvac_mode?, preset_mode?, ...}.
    Returns:
        Block with start/action.
    """
    block: Dict[str, Any] = {"start": node["time"]}
    hvac = node.get("hvac_mode")
    temp = node.get("temp")
    extras = {
        key: node[key]
        for key in ("preset_mode", "fan_mode", "swing_mode")
        if node.get(key) is not None
    }
    if hvac == "off":
        block["action"] = "turn_off"
        return block
    if temp is None and hvac and not extras:
        block["action"] = "set_hvac_mode"
        block["hvac_mode"] = hvac
        return block
    if temp is None:
        block["action"] = "set_climate_options"
        block.update(extras)
        return block
    block["action"] = "set_temperature"
    block["temperature"] = temp
    if hvac:
        block["hvac_mode"] = hvac
    block.update(extras)
    return block


def nodes_to_blocks(nodes: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Map resolved nodes to Velair blocks.

    Args:
        nodes: Desired nodes.
    Returns:
        Block list.
    """
    return [node_to_block(node) for node in nodes]


def block_to_node(block: Dict[str, Any]) -> Dict[str, Any]:
    """Map a saved block to the node shape used for diffs.

    Args:
        block: Velair block.
    Returns:
        {time, temp?, hvac_mode?, preset_mode?}.
    """
    action = block.get("action") or "set_temperature"
    node: Dict[str, Any] = {"time": block.get("start")}
    if action == "turn_off":
        node["hvac_mode"] = "off"
        return node
    if block.get("temperature") is not None:
        node["temp"] = block["temperature"]
    if block.get("hvac_mode"):
        node["hvac_mode"] = block["hvac_mode"]
    if block.get("preset_mode"):
        node["preset_mode"] = block["preset_mode"]
    if block.get("fan_mode"):
        node["fan_mode"] = block["fan_mode"]
    if block.get("swing_mode"):
        node["swing_mode"] = block["swing_mode"]
    return node


def blocks_to_nodes(blocks: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize saved blocks for comparison.

    Args:
        blocks: Velair blocks.
    Returns:
        Node dicts.
    """
    return [block_to_node(b) for b in blocks if isinstance(b, dict)]


def shared_nodes(
    schedules: Sequence[Dict[str, List[Dict[str, Any]]]], days: Sequence[str]
) -> Optional[List[Dict[str, Any]]]:
    """Return nodes when every zone has the same blocks on these days.

    Args:
        schedules: Per-entity weekday maps.
        days: Weekday names.
    Returns:
        Shared node list, or None when they differ.
    """
    found: Optional[List[Dict[str, Any]]] = None
    for sched in schedules:
        for day in days:
            nodes = blocks_to_nodes(sched.get(day) or [])
            if found is None:
                found = nodes
            elif nodes != found:
                return None
    return found or []


def set_day(
    client: HaClient,
    entity_id: str,
    weekday: str,
    blocks: List[Dict[str, Any]],
    revision: Optional[int] = None,
) -> Any:
    """Replace one weekday on one climate.

    Args:
        client: HA client.
        entity_id: Managed climate.
        weekday: Full weekday name.
        blocks: Velair blocks.
        revision: Optional configuration revision.
    Returns:
        Service response.
    """
    data: Dict[str, Any] = {
        "entity_id": entity_id,
        "weekday": weekday,
        "blocks": blocks,
    }
    if revision is not None:
        data["expected_revision"] = revision
    return client.call_service("velair", "set_daily_schedule", data, write=True, return_response=False)


def set_temp_step(client: HaClient, entity_id: str, step: float) -> None:
    """Save a fallback target step when the climate does not publish one.

    Args:
        client: HA client.
        entity_id: Managed climate.
        step: Step in degrees, such as 0.5.
    """
    client.ws_command(
        "velair/update_zone_target_temp_step",
        {"entity_id": entity_id, "target_temp_step": step},
        write=True,
    )
