#!/usr/bin/env python3
"""Velair heating schedule sync CLI."""

from __future__ import annotations

import argparse
import copy
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from lib import cs_api, velair_api
from lib.collect import (
    check_collect,
    compute_history_results,
    compute_results,
    history_entity_ids,
    stat_ids,
)
from lib.config import ScheduleConfig, climate_entities, load_config, save_config
from lib.ha import HaClient, load_connection
from lib.lint import exit_code, validate
from lib.resolve import (
    format_nodes,
    nodes_to_cs_payload,
    resolve_day_nodes,
    resolve_time_triggers,
    schedule_keys,
)
from lib.timeutil import DAYS, expand_days

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "schedule.yaml"
REQUIRED_SERVICES = [
    ("climate_scheduler", "get_schedule"),
    ("climate_scheduler", "set_schedule"),
    ("climate_scheduler", "set_group_schedule"),
    ("climate_scheduler", "list_groups"),
    ("climate_scheduler", "create_group"),
    ("climate_scheduler", "enable_schedule"),
    ("climate_scheduler", "sync_all"),
]
VELAIR_SERVICES = [
    ("velair", "apply_schedule"),
    ("velair", "set_daily_schedule"),
    ("velair", "activate_profile"),
]


def _service_names(raw: Any) -> set:
    """Service names from an HA domain entry.

    Args:
        raw: `services` field: dict of names or list of `{service: ...}`.
    Returns:
        Service name set.
    """
    if isinstance(raw, dict):
        return set(raw)
    names = set()
    if isinstance(raw, list):
        for svc in raw:
            if isinstance(svc, str):
                names.add(svc)
            elif isinstance(svc, dict) and svc.get("service"):
                names.add(str(svc["service"]))
    return names


def _services_by_domain(payload: Any) -> Dict[str, set]:
    """Map domain to service names from GET /api/services.

    Args:
        payload: HA services list.
    Returns:
        Domain -> names.
    """
    out: Dict[str, set] = {}
    if not isinstance(payload, list):
        return out
    for item in payload:
        if not isinstance(item, dict) or not item.get("domain"):
            continue
        out[str(item["domain"])] = _service_names(item.get("services"))
    return out


def _cfg(path: Optional[str]) -> ScheduleConfig:
    """Load config from path.

    Args:
        path: Optional schedule path.
    Returns:
        ScheduleConfig.
    """
    return load_config(Path(path) if path else DEFAULT_CONFIG)


def cmd_test(args: argparse.Namespace) -> int:
    """Check HA connection and prerequisites.

    Args:
        args: Parsed CLI args.
    Returns:
        Exit code.
    """
    cfg = _cfg(args.config)
    client = load_connection(args.url, args.token, ROOT)
    client.allow_writes = False
    info = client.get_config()
    print(f"HA version: {info.get('version') or info}")
    by_domain = _services_by_domain(client.get_services())
    required = REQUIRED_SERVICES
    if "climate_scheduler" not in by_domain and "velair" in by_domain:
        required = VELAIR_SERVICES
        print("scheduler: velair (climate_scheduler not installed)")
    missing = False
    for domain, service in required:
        ok = service in by_domain.get(domain, set())
        print(f"service {domain}.{service}: {'ok' if ok else 'MISSING'}")
        if not ok:
            missing = True
    if missing:
        return 1
    for ent in climate_entities(cfg):
        state = client.get_state(ent)
        if state is None:
            print(f"entity {ent}: MISSING")
            return 1
        step = (state.get("attributes") or {}).get("target_temp_step")
        print(f"entity {ent}: ok target_temp_step={step}")
    status_sensor = (cfg.scheduler or {}).get("status_sensor")
    if status_sensor:
        paused = (cfg.scheduler or {}).get("paused_state", "paused")
        status = client.get_state(str(status_sensor))
        if status:
            print(f"scheduler status: {status.get('state')}")
            if status.get("state") == paused:
                print("NOTE: scheduler paused; unpause before apply --yes")
        else:
            print(f"scheduler status sensor missing: {status_sensor}")
    print("test ok (0 writes)")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """Validate schedule offline.

    Args:
        args: CLI args.
    Returns:
        Exit code.
    """
    cfg = _cfg(args.config)
    findings = validate(
        cfg,
        strict=args.strict,
        profile=args.profile,
        climate_profile=args.climate_profile,
    )
    for f in findings:
        print(f.format())
    code = exit_code(findings)
    if code == 0:
        print("validate ok")
    return code


def cmd_show(args: argparse.Namespace) -> int:
    """Print resolved schedule for entity/group and day.

    Args:
        args: CLI args.
    Returns:
        Exit code.
    """
    from lib.climate_profiles import resolve_climate_profile_day

    cfg = _cfg(args.config)
    if args.entity:
        key = args.entity
    elif args.group:
        key = f"group:{args.group}"
    else:
        print("need --entity or --group", file=sys.stderr)
        return 2
    if args.profile and args.climate_profile:
        print("use only one of --profile and --climate-profile", file=sys.stderr)
        return 2
    days = expand_days(args.day)
    for day in days:
        if args.climate_profile:
            nodes = resolve_climate_profile_day(cfg, args.climate_profile, key, day)
            label = f"climate_profile={args.climate_profile}"
        else:
            nodes = resolve_day_nodes(cfg, key, day, profile=args.profile)
            label = day
        print(f"# {key} {label if args.climate_profile else day}")
        if args.climate_profile:
            print(f"# day {day}")
        print(format_nodes(nodes, args.format), end="")
    return 0


def _backup_payload(cfg: ScheduleConfig, client: HaClient) -> Dict[str, Any]:
    """Collect backup snapshot.

    Args:
        cfg: Config.
        client: HA client.
    Returns:
        Snapshot dict.
    """
    snap: Dict[str, Any] = {
        "created": datetime.now(timezone.utc).isoformat(),
        "legacy_switches": {},
        "climate_schedules": {},
        "groups": None,
        "automations": {},
    }
    for sw in cfg.legacy_disable:
        st = client.get_state(sw)
        snap["legacy_switches"][sw] = st
    if _uses_velair(client):
        live = velair_api.fetch_schedule(client)
        snap["velair_revision"] = live.get("configuration_revision")
        snap["velair_global"] = live.get("global")
        snap["velair_schedules"] = {
            eid: (zone or {}).get("schedule")
            for eid, zone in (live.get("zones") or {}).items()
            if isinstance(zone, dict)
        }
        snap["profiles"] = live.get("profiles") or []
        snap["modes"] = live.get("modes") or []
        snap["active_profile_ids"] = velair_api.active_profile_ids(live)
    else:
        for key in schedule_keys(cfg):
            try:
                snap["climate_schedules"][key] = cs_api.get_schedule(client, key)
            except Exception as exc:
                snap["climate_schedules"][key] = {"error": str(exc)}
        try:
            snap["groups"] = cs_api.list_groups(client)
        except Exception as exc:
            snap["groups_error"] = str(exc)
    for name, auto in cfg.automations.items():
        aid = auto.get("id")
        if not aid:
            snap["automations"][name] = None
            continue
        try:
            snap["automations"][name] = client.get_automation_config(str(aid))
        except Exception as exc:
            snap["automations"][name] = {"error": str(exc)}
    return snap


def cmd_backup(args: argparse.Namespace) -> int:
    """Write backups/<timestamp>.yaml.

    Args:
        args: CLI args.
    Returns:
        Exit code.
    """
    cfg = _cfg(args.config)
    client = load_connection(args.url, args.token, ROOT)
    client.allow_writes = False
    snap = _backup_payload(cfg, client)
    backups = ROOT / "backups"
    backups.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = backups / f"{stamp}.yaml"
    path.write_text(yaml.safe_dump(snap, sort_keys=False))
    cfg.raw.setdefault("backup", {})["last"] = str(path.relative_to(ROOT))
    save_config(cfg)
    print(f"backup written: {path}")
    print("0 writes")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    """Restore from a backup file.

    Args:
        args: CLI args.
    Returns:
        Exit code.
    """
    cfg = _cfg(args.config)
    path = Path(args.file)
    if not path.is_absolute():
        path = ROOT / path
    snap = yaml.safe_load(path.read_text())
    dry = not args.yes
    client = load_connection(args.url, args.token, ROOT)
    client.allow_writes = not dry

    for sw, st in (snap.get("legacy_switches") or {}).items():
        state = (st or {}).get("state")
        print(f"legacy {sw} -> {state}")
        if not dry and state in ("on", "off"):
            client.call_service("switch", f"turn_{state}", {"entity_id": sw})

    for key, data in (snap.get("climate_schedules") or {}).items():
        payload = _normalize_get_schedule(data)
        if payload is None:
            print(f"skip {key}: no saved schedule")
            continue
        schedule = payload.get("schedule") or payload
        if not isinstance(schedule, dict):
            print(f"skip {key}: unexpected shape")
            continue
        print(f"restore schedule {key}: keys={list(schedule)}")
        if dry:
            continue
        is_group = key.startswith("group:")
        mode = "5/2"
        for day, nodes in schedule.items():
            if day in ("enabled", "schedule_mode", "active_profile"):
                if day == "schedule_mode":
                    mode = nodes
                continue
            if not isinstance(nodes, list):
                continue
            cs_api.set_day_schedule(client, key, day, nodes, mode, is_group=is_group)

    for name, conf in (snap.get("automations") or {}).items():
        if not conf or "error" in conf:
            print(f"skip automation {name}")
            continue
        aid = (cfg.automations.get(name) or {}).get("id") or conf.get("id")
        print(f"restore automation {name} id={aid}")
        if not dry and aid:
            client.set_automation_config(str(aid), conf)

    print("0 writes" if dry else f"writes={client.writes}")
    return 0


def _normalize_get_schedule(data: Any) -> Optional[Dict[str, Any]]:
    """Normalize get_schedule service response.

    Args:
        data: Raw service response.
    Returns:
        {schedule, enabled} or None.
    """
    if data is None:
        return None
    if isinstance(data, list):
        if not data:
            return None
        data = data[0]
    if not isinstance(data, dict):
        return None
    # sometimes wrapped under response
    if "schedule" in data:
        return data
    if "response" in data and isinstance(data["response"], dict):
        return data["response"]
    return None


def _desired_schedule_map(
    cfg: ScheduleConfig, key: str, profile: Optional[str]
) -> Dict[str, List[Dict[str, Any]]]:
    """Build day->nodes payload for set_schedule.

    Args:
        cfg: Config.
        key: Schedule key.
        profile: Optional profile.
    Returns:
        Day map for CS.
    """
    sched = cfg.schedules.get(key) or {}
    mode = sched.get("mode") or "5/2"
    out: Dict[str, List[Dict[str, Any]]] = {}
    if mode == "5/2":
        for label, rep in (("weekday", "mon"), ("weekend", "sat")):
            nodes = resolve_day_nodes(cfg, key, rep, profile=profile)
            out[label] = nodes_to_cs_payload(nodes)
    elif mode == "all_days":
        nodes = resolve_day_nodes(cfg, key, "mon", profile=profile)
        out["all_days"] = nodes_to_cs_payload(nodes)
    else:
        for day in DAYS:
            nodes = resolve_day_nodes(cfg, key, day, profile=profile)
            out[day] = nodes_to_cs_payload(nodes)
    return out


def _diff_nodes(desired: List[Dict[str, Any]], current: Any) -> List[str]:
    """Human-readable node diffs.

    Args:
        desired: Desired nodes.
        current: Current nodes from HA.
    Returns:
        Diff lines.
    """
    cur = current or []
    if not isinstance(cur, list):
        return [f"  current shape unexpected: {type(current)}"]
    lines = []
    # compare by time
    cur_by = {n.get("time"): n for n in cur if isinstance(n, dict)}
    des_by = {n.get("time"): n for n in desired}
    for t in sorted(set(cur_by) | set(des_by)):
        a = des_by.get(t)
        b = cur_by.get(t)
        if a != b:
            lines.append(f"  {t}: desired={a} current={b}")
    return lines


def _note_backup(cfg: ScheduleConfig, rel: str) -> None:
    """Record the latest backup path without rewriting the schedule file.

    Args:
        cfg: Config.
        rel: Backup path relative to the repo.
    """
    lines = cfg.path.read_text().splitlines(keepends=True)
    found = False
    for index, line in enumerate(lines):
        if line.startswith("  last:"):
            lines[index] = f"  last: {rel}\n"
            found = True
            break
    if not found:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.extend(["backup:\n", f"  last: {rel}\n"])
    cfg.path.write_text("".join(lines))
    cfg.raw.setdefault("backup", {})["last"] = rel


def _revision_from(result: Any) -> Optional[int]:
    """Read configuration_revision from a Velair service response.

    Args:
        result: HA service response.
    Returns:
        Revision, or None.
    """
    data = result
    if isinstance(result, dict) and isinstance(result.get("service_response"), dict):
        data = result["service_response"]
    if isinstance(data, dict) and isinstance(data.get("configuration_revision"), int):
        return int(data["configuration_revision"])
    return None


def _is_revision_conflict(exc: BaseException) -> bool:
    """True when Velair rejected a stale configuration_revision.

    Args:
        exc: Raised error.
    Returns:
        Whether the message is a revision conflict.
    """
    text = str(exc).lower()
    return "configuration changed" in text or "revision" in text


def _set_day_refreshing(
    client: HaClient,
    entity_id: str,
    day: str,
    blocks: List[Dict[str, Any]],
    revision: Optional[int],
) -> Optional[int]:
    """Write one day, refresh revision, retry once on conflict.

    Args:
        client: HA client.
        entity_id: Climate entity.
        day: Full weekday name.
        blocks: Velair blocks.
        revision: Expected configuration revision.
    Returns:
        Latest revision after the write.
    """
    try:
        result = velair_api.set_day(client, entity_id, day, blocks, revision)
        return _revision_from(result) or revision
    except Exception as exc:
        if not _is_revision_conflict(exc):
            raise
        live = velair_api.fetch_schedule(client)
        try:
            revision = int(live.get("configuration_revision"))
        except (TypeError, ValueError):
            revision = None
        result = velair_api.set_day(client, entity_id, day, blocks, revision)
        return _revision_from(result) or revision


def _temp_step(
    cfg: ScheduleConfig, key: str, desired: Dict[str, List[Dict[str, Any]]]
) -> Optional[float]:
    """Step Velair should use when snapping targets.

    Args:
        cfg: Config.
        key: Schedule key.
        desired: Day label -> nodes.
    Returns:
        Explicit temp_step, 0.5 when any target is fractional, or None.
    """
    raw = (cfg.schedules.get(key) or {}).get("temp_step")
    if raw is not None:
        return float(raw)
    for nodes in desired.values():
        for node in nodes:
            temp = node.get("temp")
            if isinstance(temp, (int, float)) and float(temp) % 1:
                return 0.5
    return None


def _uses_velair(client: HaClient) -> bool:
    """True when Velair is installed and Climate Scheduler is not.

    Args:
        client: HA client.
    Returns:
        Whether apply should use the Velair API.
    """
    domains = _services_by_domain(client.get_services())
    return "velair" in domains and "climate_scheduler" not in domains


def _velair_targets(cfg: ScheduleConfig, key: str, live: Dict[str, Any]) -> List[str]:
    """Climate entities that receive one yaml schedule key.

    Args:
        cfg: Config.
        key: climate.* or group:Name.
        live: Velair schedule.
    Returns:
        Entity ids.
    """
    if not key.startswith("group:"):
        return [key]
    name = key.split(":", 1)[1]
    members = velair_api.room_group_members(live, name)
    if members:
        print(f"group {name}: exists members={members}")
        return members
    members = list((cfg.groups.get(name) or {}).get("entities") or [])
    print(f"group {name}: no Velair room group; same blocks -> {members}")
    return members


def _apply_velair(
    cfg: ScheduleConfig, client: HaClient, dry: bool, profile: Optional[str]
) -> int:
    """Diff and push schedules through Velair.

    Args:
        cfg: Config.
        client: HA client.
        dry: Skip writes.
        profile: Optional profile name.
    Returns:
        Exit code.
    """
    live = velair_api.fetch_schedule(client)
    mode = (live.get("global") or {}).get("mode")
    print(f"velair scheduler: {mode} revision={live.get('configuration_revision')}")
    if mode == "paused":
        print("NOTE: scheduler is paused; saved blocks do not run until resumed")
    zones = live.get("zones") or {}
    revision: Optional[int] = None
    if live.get("configuration_revision") is not None:
        try:
            revision = int(live.get("configuration_revision"))
        except (TypeError, ValueError):
            revision = None

    for key in schedule_keys(cfg, profile):
        targets = _velair_targets(cfg, key, live)
        missing = [ent for ent in targets if ent not in zones]
        if missing:
            print(f"{key}: not managed by Velair: {missing}")
            return 1
        desired = _desired_schedule_map(cfg, key, profile)
        saved = [velair_api.zone_schedule(live, ent) for ent in targets]
        print(f"## {key}")
        if not dry:
            step = _temp_step(cfg, key, desired)
            if step is not None:
                for ent in targets:
                    zone = zones.get(ent) or {}
                    current = zone.get("target_temp_step_override")
                    if current is not None and float(current) == float(step):
                        print(f"{ent}: target_temp_step={step} (unchanged)")
                        continue
                    print(f"{ent}: target_temp_step={step}")
                    result = velair_api.set_temp_step(client, ent, float(step))
                    revision = _revision_from(result) or revision
        for label, nodes in desired.items():
            days = velair_api.days_for_label(label)
            blocks = velair_api.nodes_to_blocks(nodes)
            shared = velair_api.shared_nodes(saved, days)
            to_write: List[tuple[str, str]] = []
            if shared is None:
                for ent, sched in zip(targets, saved):
                    for day in days:
                        diffs = _diff_nodes(
                            nodes, velair_api.blocks_to_nodes(sched.get(day) or [])
                        )
                        print(f"{ent} {day}:")
                        print("\n".join(diffs) if diffs else "  unchanged")
                        if diffs:
                            to_write.append((ent, day))
            else:
                diffs = _diff_nodes(nodes, shared)
                shown = (
                    ",".join(days)
                    if label not in ("weekday", "weekend", "all_days")
                    else label
                )
                if diffs:
                    print(f"{shown}:")
                    print("\n".join(diffs))
                    for ent in targets:
                        for day in days:
                            to_write.append((ent, day))
                else:
                    print(f"{shown}: unchanged")
            if dry:
                continue
            for ent, day in to_write:
                revision = _set_day_refreshing(client, ent, day, blocks, revision)

    # Seasonal climate profiles only on a normal apply (not --profile overlay).
    if profile is None and cfg.climate_profiles:
        if not dry:
            live = velair_api.fetch_schedule(client)
            try:
                revision = int(live.get("configuration_revision"))
            except (TypeError, ValueError):
                revision = None
        code = _apply_climate_profiles(cfg, client, live, dry, revision)
        if code != 0:
            return code

    _apply_automations(cfg, client, dry)
    for sw in cfg.legacy_disable:
        st = client.get_state(sw)
        cur = (st or {}).get("state")
        if cur == "on":
            print(f"legacy {sw}: on -> off")
            if not dry:
                client.call_service("switch", "turn_off", {"entity_id": sw})
        else:
            print(f"legacy {sw}: {cur or 'missing'}")
    print("0 writes" if dry else f"writes={client.writes}")
    return 0


def _apply_climate_profiles(
    cfg: ScheduleConfig,
    client: HaClient,
    live: Dict[str, Any],
    dry: bool,
    revision: Optional[int],
) -> int:
    """Upsert Velair climate profiles and modes without activating.

    Args:
        cfg: Config.
        client: HA client.
        live: Current get_schedule payload.
        dry: Dry-run flag.
        revision: Expected configuration revision.
    Returns:
        Exit code.
    """
    from lib.climate_profiles import (
        parse_climate_profiles,
        set_mode_body,
        set_profile_body,
    )

    try:
        specs = parse_climate_profiles(cfg)
    except ValueError as exc:
        print(f"climate_profiles: {exc}")
        return 1
    known = set(velair_api.known_profile_keys(live))
    active = set(velair_api.active_profile_ids(live))
    yaml_keys = {s.key for s in specs}
    for orphan in sorted(known - yaml_keys):
        print(f"climate_profile {orphan}: in Velair but not yaml; left in place")
    zones = live.get("zones") or {}
    for spec in specs:
        body = set_profile_body(cfg, spec)
        zone_ids = sorted(body["zones"].keys())
        missing = [z for z in zone_ids if z not in zones]
        if missing:
            print(f"climate_profile {spec.key}: not managed by Velair: {missing}")
            return 1
        action = "update" if spec.key in known else "create"
        print(
            f"climate_profile {spec.key}: {action} zones={zone_ids} "
            f"mode={spec.mode!r} priority={spec.priority}"
        )
        if spec.key in active:
            print(
                f"climate_profile {spec.key}: ACTIVE; saving changes the running week"
            )
        if dry:
            continue
        try:
            result = velair_api.set_profile(
                client, body, expected_revision=revision
            )
        except Exception as exc:
            print(f"climate_profile {spec.key}: set_profile failed: {exc}")
            return 1
        revision = _revision_from(result) or revision
        try:
            mode_result = velair_api.set_mode(client, set_mode_body(spec))
        except Exception as exc:
            print(f"climate_profile {spec.key}: set_mode failed: {exc}")
            return 1
        revision = _revision_from(mode_result) or revision
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    """Apply schedule to Climate Scheduler.

    Args:
        args: CLI args.
    Returns:
        Exit code.
    """
    cfg = _cfg(args.config)
    findings = validate(cfg, strict=False, profile=args.profile)
    errors = [f for f in findings if f.severity == "error"]
    for f in findings:
        print(f.format())
    if errors:
        print("validate failed; abort apply")
        return 1
    if args.profile and cfg.climate_profiles:
        print("NOTE: --profile skips climate_profiles push")

    dry = not args.yes
    client = load_connection(args.url, args.token, ROOT)
    client.allow_writes = not dry

    if args.yes:
        # backup first
        snap = _backup_payload(cfg, client)
        backups = ROOT / "backups"
        backups.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = backups / f"{stamp}.yaml"
        # temporary allow reads only already; backup uses write=False
        path.write_text(yaml.safe_dump(snap, sort_keys=False))
        rel = str(path.relative_to(ROOT))
        _note_backup(cfg, rel)
        print(f"backup: {path}")

    if _uses_velair(client):
        return _apply_velair(cfg, client, dry, args.profile)

    try:
        existing_groups = cs_api.list_groups(client)
    except Exception as exc:
        print(f"list_groups failed: {exc}")
        existing_groups = []
    for name, gdef in cfg.groups.items():
        if name not in existing_groups:
            print(f"group {name}: to create entities={gdef.get('entities')}")
            if not dry:
                cs_api.create_group(client, name, list(gdef.get("entities") or []))
        else:
            print(f"group {name}: exists")

    for key in schedule_keys(cfg, args.profile):
        desired = _desired_schedule_map(cfg, key, args.profile)
        try:
            raw = cs_api.get_schedule(client, key)
            current = _normalize_get_schedule(raw) or raw
        except Exception as exc:
            print(f"{key}: get_schedule failed ({exc}); local desired:")
            for day, nodes in desired.items():
                if day == "schedule_mode":
                    continue
                print(f"  {day}: {nodes}")
            print("read API changed or unavailable; abort")
            return 1

        cur_sched = cs_api.parse_schedule_nodes(current)
        print(f"## {key}")
        for day, nodes in desired.items():
            diffs = _diff_nodes(nodes, cur_sched.get(day))
            if diffs:
                print(f"{day}:")
                print("\n".join(diffs))
            else:
                print(f"{day}: unchanged")

        if dry:
            continue

        mode = (cfg.schedules.get(key) or {}).get("mode") or "5/2"
        is_group = key.startswith("group:")
        profile = cs_api.profile_name_for(key)
        for day, nodes in desired.items():
            cs_api.set_day_schedule(
                client, key, day, nodes, mode, is_group=is_group, profile=profile
            )
        cs_api.enable_schedule(client, key, is_group=is_group)

        step = (cfg.schedules.get(key) or {}).get("temp_step")
        if step is not None and not key.startswith("group:"):
            print(f"{key}: temp_step={step} (snap only; no settable helper found)")

    _apply_automations(cfg, client, dry)

    for sw in cfg.legacy_disable:
        st = client.get_state(sw)
        cur = (st or {}).get("state")
        if cur == "on":
            print(f"legacy {sw}: on -> off")
            if not dry:
                client.call_service("switch", "turn_off", {"entity_id": sw})
        else:
            print(f"legacy {sw}: {cur or 'missing'}")

    if not dry:
        cs_api.sync_all(client)

    print("0 writes" if dry else f"writes={client.writes}")
    return 0


def _yaml_label_specs(auto: Dict[str, Any]) -> List[Any]:
    """Read optional labels list from an automation block.

    Args:
        auto: Automation yaml dict.
    Returns:
        Label specs (strings or maps).
    """
    return list(auto.get("labels") or [])


def _ensure_label_id(client: HaClient, spec: Any, known: Dict[str, Dict[str, Any]]) -> str:
    """Resolve one yaml label spec to a registry label_id, creating if needed.

    Args:
        client: HA client.
        spec: String label_id or {name, icon} map.
        known: label_id -> registry entry (updated in place).
    Returns:
        label_id.
    """
    if isinstance(spec, str):
        label_id = spec
        if label_id in known:
            return label_id
        by_name = {str(e.get("name") or ""): e for e in known.values()}
        if label_id in by_name:
            return str(by_name[label_id]["label_id"])
        created = client.create_label(label_id)
        lid = str(created.get("label_id") or label_id)
        known[lid] = created
        return lid

    if isinstance(spec, dict):
        name = str(spec.get("name") or "").strip()
        if not name:
            raise RuntimeError(f"label map needs name: {spec}")
        icon = spec.get("icon")
        by_name = {str(e.get("name") or ""): e for e in known.values()}
        if name in by_name:
            return str(by_name[name]["label_id"])
        if name in known:
            return name
        created = client.create_label(name, icon=str(icon) if icon else None)
        lid = str(created.get("label_id") or name)
        known[lid] = created
        return lid

    raise RuntimeError(f"unsupported label spec: {spec!r}")


def _apply_automation_labels(
    client: HaClient,
    automation_id: str,
    auto: Dict[str, Any],
    dry: bool,
) -> None:
    """Union existing, managed, and yaml labels onto an automation entity.

    Args:
        client: HA client.
        automation_id: Automation unique id.
        auto: Automation yaml block.
        dry: Dry-run flag.
    """
    wanted_specs: List[Any] = ["managed", *_yaml_label_specs(auto)]
    entity_id = client.find_automation_entity(str(automation_id))
    existing: List[str] = []
    if entity_id:
        try:
            existing = client.get_entity_labels(entity_id)
        except Exception as exc:
            print(f"labels {automation_id}: entity read failed: {exc}")
            existing = []
    else:
        print(f"labels {automation_id}: entity not found yet")

    if dry:
        print(
            f"labels {automation_id} entity={entity_id or '?'} "
            f"existing={existing} add={wanted_specs}"
        )
        return

    if not entity_id:
        return

    known = {str(e.get("label_id")): e for e in client.list_labels()}
    if "managed" not in known:
        created = client.create_label("managed")
        known[str(created.get("label_id") or "managed")] = created

    resolved = [str(x) for x in existing]
    for spec in wanted_specs:
        lid = _ensure_label_id(client, spec, known)
        if lid not in resolved:
            resolved.append(lid)
    print(f"labels {automation_id} entity={entity_id} -> {resolved}")
    client.set_entity_labels(entity_id, resolved)


def _apply_outdoor_gate(
    name: str, auto: Dict[str, Any], client: HaClient, dry: bool
) -> None:
    """Upsert an outdoor_gate automation.

    Args:
        name: Automation key in yaml.
        auto: Automation block.
        client: HA client.
        dry: Dry-run flag.
    """
    entity = auto["entity"]
    sensor = auto["outdoor_sensor"]
    min_c = auto["min_outdoor_c"]
    alias = auto.get("alias") or name
    fallback = auto.get("fallback_script")
    conf = {
        "alias": alias,
        "mode": "single",
        "triggers": [
            {"trigger": "state", "entity_id": entity},
            {
                "trigger": "numeric_state",
                "entity_id": sensor,
                "below": min_c,
            },
        ],
        "conditions": [
            {
                "condition": "numeric_state",
                "entity_id": sensor,
                "below": min_c,
            },
            {
                "condition": "not",
                "conditions": [
                    {"condition": "state", "entity_id": entity, "state": "off"}
                ],
            },
        ],
        "actions": [
            {
                "action": "climate.set_hvac_mode",
                "target": {"entity_id": entity},
                "data": {"hvac_mode": "off"},
            }
        ],
    }
    if fallback:
        conf["actions"].append(
            {
                "action": "script.turn_on",
                "target": {"entity_id": fallback},
            }
        )
    aid = auto.get("id")
    print(f"automation {name} id={aid}")
    if not dry:
        if not aid:
            aid = f"{name}_{int(datetime.now().timestamp())}"
            conf["id"] = aid
            client.set_automation_config(str(aid), conf)
            print(f"created {name} id={aid}; set automations.{name}.id")
        else:
            try:
                existing = client.get_automation_config(str(aid))
            except Exception:
                existing = {}
            merged = existing if isinstance(existing, dict) else {}
            for old in ("trigger", "condition", "action"):
                merged.pop(old, None)
            merged.update(conf)
            client.set_automation_config(str(aid), merged)
        _apply_automation_labels(client, str(aid), auto, dry=False)
    else:
        _apply_automation_labels(client, str(aid or name), auto, dry=True)


def _apply_time_switch(
    cfg: ScheduleConfig,
    name: str,
    auto: Dict[str, Any],
    client: HaClient,
    dry: bool,
) -> None:
    """Upsert a time_switch automation.

    Args:
        cfg: Config.
        name: Automation key in yaml.
        auto: Automation block.
        client: HA client.
        dry: Dry-run flag.
    """
    aid = auto.get("id")
    if not aid:
        print(f"automation {name}: missing id; skip")
        return
    print(f"automation {name} id={aid}")
    new_triggers = resolve_time_triggers(cfg, auto)
    print(f"{name} triggers -> {new_triggers}")
    entity = auto.get("entity")
    temp_sensor = auto.get("temp_sensor")
    below_c = auto.get("below_c")
    alias = auto.get("alias") or name
    full = entity and temp_sensor is not None and below_c is not None
    if full:
        domain = str(entity).split(".", 1)[0]
        conditions = [
            {
                "condition": "numeric_state",
                "entity_id": temp_sensor,
                "below": below_c,
            }
        ]
        actions = [
            {
                "action": f"{domain}.turn_on",
                "target": {"entity_id": entity},
            }
        ]
        print(f"{name} condition -> {conditions}")
        print(f"{name} action -> {actions}")
    else:
        conditions = None
        actions = None
    if not dry:
        try:
            existing = client.get_automation_config(str(aid))
        except Exception:
            existing = None
        if not isinstance(existing, dict):
            existing = {"id": str(aid), "mode": "single"}
            print(f"created {name} id={aid}")
        for old in ("trigger", "condition", "action"):
            existing.pop(old, None)
        existing["alias"] = alias
        existing["mode"] = existing.get("mode") or "single"
        existing["triggers"] = new_triggers
        if conditions is not None:
            existing["conditions"] = conditions
        if actions is not None:
            existing["actions"] = actions
        client.set_automation_config(str(aid), existing)
        _apply_automation_labels(client, str(aid), auto, dry=False)
    else:
        _apply_automation_labels(client, str(aid), auto, dry=True)


def _apply_season_select(
    cfg: ScheduleConfig,
    name: str,
    auto: Dict[str, Any],
    client: HaClient,
    dry: bool,
) -> None:
    """Upsert season_select automation.

    Args:
        cfg: Config.
        name: Automation key.
        auto: Automation block.
        client: HA client.
        dry: Dry-run flag.
    """
    from lib.climate_profiles import (
        PROTECTED_AUTOMATION_IDS,
        SEASON_SELECT_ID,
        season_select_automation,
    )

    aid = str(auto.get("id") or SEASON_SELECT_ID)
    if aid in PROTECTED_AUTOMATION_IDS:
        raise RuntimeError(f"automations.{name}: refuses protected id {aid}")
    conf = season_select_automation(cfg, auto)
    print(f"automation {name} id={aid}")
    print(f"{name} triggers -> {conf['triggers']}")
    print(f"{name} actions -> choose modes + Default")
    if not dry:
        try:
            existing = client.get_automation_config(str(aid))
        except Exception:
            existing = None
        if not isinstance(existing, dict):
            existing = {"id": str(aid), "mode": "single"}
            print(f"created {name} id={aid}")
        for old in ("trigger", "condition", "action"):
            existing.pop(old, None)
        existing.update(conf)
        client.set_automation_config(str(aid), existing)
        _apply_automation_labels(client, str(aid), auto, dry=False)
    else:
        _apply_automation_labels(client, str(aid or name), auto, dry=True)


def _apply_automations(cfg: ScheduleConfig, client: HaClient, dry: bool) -> None:
    """Upsert typed automations plus labels.

    Args:
        cfg: Config.
        client: HA client.
        dry: Dry-run flag.
    """
    for name, auto in (cfg.automations or {}).items():
        if not isinstance(auto, dict):
            continue
        atype = auto.get("type")
        if atype == "outdoor_gate":
            _apply_outdoor_gate(name, auto, client, dry)
        elif atype == "time_switch":
            _apply_time_switch(cfg, name, auto, client, dry)
        elif atype == "season_select":
            _apply_season_select(cfg, name, auto, client, dry)
        else:
            raise RuntimeError(f"automations.{name}: unknown type {atype!r}")


def cmd_compare(args: argparse.Namespace) -> int:
    """Compare resolved schedule temps to HA history.

    Args:
        args: CLI args.
    Returns:
        Exit code.
    """
    from lib.history import (
        DEFAULT_ACTUAL_ATTR,
        DEFAULT_SETPT_ATTR,
        compare_day,
        compare_override,
        day_window,
        format_compare_table,
        history_entity_id,
        local_tz,
        parse_date,
        parse_history,
    )

    cfg = _cfg(args.config)
    if not args.entity:
        print("need --entity", file=sys.stderr)
        return 2
    key = args.entity
    on_date = parse_date(args.date)
    day = args.day
    if day == "all":
        # Map calendar weekday to mon..sun token.
        day = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[on_date.weekday()]
    days = expand_days(day)
    if len(days) != 1:
        print("compare needs one day (mon..sun or weekday/weekend alias of one)", file=sys.stderr)
        return 2
    day = days[0]

    override = compare_override(cfg, key)
    hist_entity = str(override.get("entity") or history_entity_id(cfg, key))
    actual_attr = str(override.get("actual_attr") or DEFAULT_ACTUAL_ATTR)
    setpt_attr = str(override.get("setpt_attr") or DEFAULT_SETPT_ATTR)
    tz_name = override.get("timezone")
    zone = local_tz(str(tz_name) if tz_name else None)
    start, end = day_window(on_date, tz=zone, end_now_if_today=True)

    client = load_connection(args.url, args.token, ROOT)
    client.allow_writes = False
    try:
        raw = client.history(
            hist_entity,
            start.isoformat(),
            end.isoformat(),
        )
    except Exception as exc:
        print(f"history error: {exc}", file=sys.stderr)
        return 1
    samples = parse_history(raw, actual_attr=actual_attr, setpt_attr=setpt_attr)
    rows = compare_day(cfg, key, day, on_date, samples, tz=zone)
    print(f"# compare {key} {day} {on_date.isoformat()} history={hist_entity}")
    print(format_compare_table(rows, fmt=args.format), end="")
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    """Pull stats and append a reviews entry.

    Args:
        args: CLI args.
    Returns:
        Exit code.
    """
    from zoneinfo import ZoneInfo

    from lib.history import local_tz, parse_date

    cfg = _cfg(args.config)
    collect = cfg.collect or {}
    errors = check_collect(collect, known_specifiers=list(cfg.specifiers))
    if errors:
        for err in errors:
            print(f"collect schema: {err}")
        return 1
    statistic_ids = stat_ids(collect)
    hist_ids = history_entity_ids(collect)
    if not statistic_ids and not hist_ids:
        print("no collect.metrics or history_metrics defined; nothing to collect")
        return 1
    client = load_connection(args.url, args.token, ROOT)
    client.allow_writes = False
    since = args.since
    now_utc = datetime.now(timezone.utc)
    end = now_utc.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    start = f"{since}T00:00:00+00:00"
    period = str(collect.get("period") or "hour")
    stats: Dict[str, Any] = {}
    if statistic_ids:
        try:
            stats = client.statistics(statistic_ids, start, end, period=period)
        except Exception as exc:
            print(f"statistics error: {exc}")
            stats = {}

    results = compute_results(collect, stats)

    if hist_ids:
        try:
            ha_tz = str((client.get_config() or {}).get("time_zone") or "")
        except Exception:
            ha_tz = ""
        zone = ZoneInfo(ha_tz) if ha_tz else local_tz()
        since_d = parse_date(since)
        until_d = now_utc.astimezone(zone).date()
        bound_start = datetime.combine(since_d, datetime.min.time(), tzinfo=zone)
        bound_end = now_utc.astimezone(zone)
        histories: Dict[str, Any] = {}
        for eid in hist_ids:
            try:
                histories[eid] = client.history(
                    eid, bound_start.isoformat(), bound_end.isoformat()
                )
            except Exception as exc:
                print(f"history error {eid}: {exc}")
                histories[eid] = []
        hist_results = compute_history_results(
            cfg,
            collect,
            histories,
            since=since_d,
            until=until_d,
            bound_start=bound_start,
            bound_end=bound_end,
            tz=zone,
        )
        results.update(hist_results)

    entry = {
        "date": now_utc.strftime("%Y-%m-%d"),
        "period": f"{since}/{now_utc.strftime('%Y-%m-%d')}",
        "results": results,
        "verdict": None,
        "changes": None,
    }
    print(yaml.safe_dump(entry, sort_keys=False))
    if args.dry_run:
        print("dry-run: reviews not written")
        return 0
    reviews = list(cfg.raw.get("reviews") or [])
    reviews.append(entry)
    cfg.raw["reviews"] = reviews
    save_config(cfg)
    print(f"appended reviews entry ({len(reviews)} total)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser.

    Returns:
        ArgumentParser.
    """
    p = argparse.ArgumentParser(prog="velair", description="Velair heating schedule sync")
    p.add_argument("--config", default=str(DEFAULT_CONFIG), help="schedule.yaml path")
    p.add_argument("--url", default=None, help="HA URL override")
    p.add_argument("--token", default=None, help="HA token override")
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("test", help="Check HA connection")
    t.set_defaults(func=cmd_test)

    v = sub.add_parser("validate", help="Lint schedule offline")
    v.add_argument("--strict", action="store_true")
    v.add_argument("--profile", default=None)
    v.add_argument("--climate-profile", default=None)
    v.set_defaults(func=cmd_validate)

    s = sub.add_parser("show", help="Show resolved schedule")
    s.add_argument("--entity", default=None)
    s.add_argument("--group", default=None)
    s.add_argument("--day", default="all")
    s.add_argument("--format", choices=["ascii", "csv"], default="ascii")
    s.add_argument("--profile", default=None)
    s.add_argument("--climate-profile", default=None)
    s.set_defaults(func=cmd_show)

    cmp_ = sub.add_parser("compare", help="Compare schedule temps to HA history")
    cmp_.add_argument("--entity", required=True)
    cmp_.add_argument("--day", default="all", help="mon..sun, or all to use --date weekday")
    cmp_.add_argument("--date", required=True, help="YYYY-MM-DD")
    cmp_.add_argument("--format", choices=["ascii", "csv"], default="ascii")
    cmp_.set_defaults(func=cmd_compare)

    b = sub.add_parser("backup", help="Backup live schedules")
    b.set_defaults(func=cmd_backup)

    r = sub.add_parser("restore", help="Restore from backup")
    r.add_argument("file")
    r.add_argument("--yes", action="store_true")
    r.add_argument("--dry-run", action="store_true", default=True)
    r.set_defaults(func=cmd_restore)

    a = sub.add_parser("apply", help="Apply schedule to HA")
    a.add_argument("--yes", action="store_true")
    a.add_argument("--dry-run", action="store_true", default=True)
    a.add_argument("--profile", default=None)
    a.set_defaults(func=cmd_apply)

    c = sub.add_parser("collect", help="Collect stats into reviews")
    c.add_argument("--since", required=True)
    c.add_argument("--dry-run", action="store_true")
    c.set_defaults(func=cmd_collect)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry.

    Args:
        argv: Optional arg list.
    Returns:
        Exit code.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
