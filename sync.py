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

from lib import cs_api
from lib.config import ScheduleConfig, climate_entities, load_config, save_config
from lib.ha import HaClient, load_connection
from lib.lint import exit_code, validate
from lib.resolve import (
    format_nodes,
    nodes_to_cs_payload,
    resolve_automation_trigger,
    resolve_day_nodes,
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
    version = info.get("version") or info
    print(f"HA version: {version}")
    services = client.get_services()
    by_domain = {s["domain"]: {svc["service"] for svc in s.get("services", [])} for s in services}
    for domain, service in REQUIRED_SERVICES:
        ok = service in by_domain.get(domain, set())
        print(f"service {domain}.{service}: {'ok' if ok else 'MISSING'}")
        if not ok:
            return 1
    for ent in climate_entities(cfg):
        state = client.get_state(ent)
        if state is None:
            print(f"entity {ent}: MISSING")
            return 1
        step = (state.get("attributes") or {}).get("target_temp_step")
        print(f"entity {ent}: ok target_temp_step={step}")
    status = client.get_state("sensor.heating_scheduler_scheduler_status")
    if status:
        print(f"scheduler status: {status.get('state')}")
        if status.get("state") == "paused":
            print("NOTE: unpause Climate Scheduler in the Velair card before apply --yes")
    else:
        print("scheduler status sensor: missing")
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
    findings = validate(cfg, strict=args.strict, profile=args.profile)
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
    cfg = _cfg(args.config)
    if args.entity:
        key = args.entity
    elif args.group:
        key = f"group:{args.group}"
    else:
        print("need --entity or --group", file=sys.stderr)
        return 2
    days = expand_days(args.day)
    for day in days:
        nodes = resolve_day_nodes(cfg, key, day, profile=args.profile)
        print(f"# {key} {day}")
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
        cfg.raw.setdefault("backup", {})["last"] = str(path.relative_to(ROOT))
        save_config(cfg)
        print(f"backup: {path}")

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
        print(f"legacy {sw}: {cur} -> off")
        if not dry and cur != "off":
            client.call_service("switch", "turn_off", {"entity_id": sw})

    if not dry:
        cs_api.sync_all(client)

    print("0 writes" if dry else f"writes={client.writes}")
    return 0


def _apply_automations(cfg: ScheduleConfig, client: HaClient, dry: bool) -> None:
    """Upsert pool_gate and tuv_charge triggers.

    Args:
        cfg: Config.
        client: HA client.
        dry: Dry-run flag.
    """
    pool = cfg.automations.get("pool_gate") or {}
    if pool:
        entity = pool.get("entity", "climate.pool")
        sensor = pool.get("outdoor_sensor", "sensor.outdoor_temperature")
        min_c = pool.get("min_outdoor_c", 5)
        alias = pool.get("alias", "Pool outdoor gate")
        fallback = pool.get("fallback_script")
        conf = {
            "alias": alias,
            "mode": "single",
            "trigger": [
                {"platform": "state", "entity_id": entity},
                {
                    "platform": "numeric_state",
                    "entity_id": sensor,
                    "below": min_c,
                },
            ],
            "condition": [
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
            "action": [
                {
                    "action": "climate.set_hvac_mode",
                    "target": {"entity_id": entity},
                    "data": {"hvac_mode": "off"},
                }
            ],
        }
        if fallback:
            conf["action"].append(
                {
                    "action": "script.turn_on",
                    "target": {"entity_id": fallback},
                }
            )
        aid = pool.get("id")
        print(f"automation pool_gate id={aid}")
        if not dry:
            if not aid:
                # create with a generated id
                aid = f"pool_gate_{int(datetime.now().timestamp())}"
                conf["id"] = aid
                client.set_automation_config(str(aid), conf)
                cfg.raw["automations"]["pool_gate"]["id"] = aid
                save_config(cfg)
                print(f"created pool_gate id={aid}")
            else:
                # merge onto existing if present
                try:
                    existing = client.get_automation_config(str(aid))
                except Exception:
                    existing = {}
                merged = existing if isinstance(existing, dict) else {}
                merged.update(conf)
                client.set_automation_config(str(aid), merged)

    tuv = cfg.automations.get("tuv_charge") or {}
    aid = tuv.get("id")
    if aid:
        print(f"automation tuv_charge id={aid}")
        triggers = []
        for trig in tuv.get("triggers") or []:
            days = list(trig.get("weekday") or [])
            resolved = resolve_automation_trigger(cfg, str(trig["at"]), days)
            # collapse to unique clocks with weekday list
            clocks: Dict[str, List[str]] = {}
            for day, clock in resolved:
                clocks.setdefault(clock, []).append(day)
            for clock, wdays in clocks.items():
                triggers.append(
                    {
                        "platform": "time",
                        "at": clock,
                        # HA time trigger has no weekday; keep as separate automations
                        # via condition
                    }
                )
        # Prefer editing only time triggers while preserving conditions/actions
        if not dry:
            existing = client.get_automation_config(str(aid))
            if not isinstance(existing, dict):
                raise RuntimeError("tuv_charge config missing")
            # Build time triggers with weekday conditions using template/time + condition
            new_triggers = []
            new_conditions_note = []
            for trig in tuv.get("triggers") or []:
                days = list(trig.get("weekday") or [])
                for day, clock in resolve_automation_trigger(cfg, str(trig["at"]), days[:1]):
                    # one trigger per unique (clock) - use first day resolution (same clock)
                    pass
            # Simpler: unique absolute clocks from triggers
            seen_clock = []
            for trig in tuv.get("triggers") or []:
                days = list(trig.get("weekday") or DAYS)
                # resolve per first matching day of each kind
                for day in days:
                    _, clock = resolve_automation_trigger(cfg, str(trig["at"]), [day])[0]
                    if clock not in seen_clock:
                        seen_clock.append(clock)
                        new_triggers.append({"platform": "time", "at": clock})
                    break
            existing["trigger"] = new_triggers
            print(f"tuv_charge triggers -> {new_triggers}")
            client.set_automation_config(str(aid), existing)
        else:
            for trig in tuv.get("triggers") or []:
                days = list(trig.get("weekday") or [])
                for day, clock in resolve_automation_trigger(cfg, str(trig["at"]), days):
                    print(f"  would set trigger {day} at {clock} (exact)")


def cmd_collect(args: argparse.Namespace) -> int:
    """Pull stats and append a reviews entry.

    Args:
        args: CLI args.
    Returns:
        Exit code.
    """
    cfg = _cfg(args.config)
    client = load_connection(args.url, args.token, ROOT)
    client.allow_writes = False
    since = args.since
    end = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    start = f"{since}T00:00:00+00:00"
    statistic_ids = list(cfg.context.get("collect_sensors") or [])
    if not statistic_ids:
        print("no context.collect_sensors defined; nothing to collect")
        return 1
    try:
        stats = client.statistics(statistic_ids, start, end, period="hour")
    except Exception as exc:
        print(f"statistics error: {exc}")
        stats = {}

    eheater = stats.get("recording:reheaterexternal") or []
    eheater_kwh = 0.0
    for row in eheater:
        ch = row.get("change")
        if ch is not None:
            eheater_kwh += float(ch)

    low = stats.get("sensor.strecno_h1_low_tariff_meter_reading") or []
    high = stats.get("sensor.strecno_h1_high_tariff_meter_reading") or []

    def _delta(rows: List[Dict[str, Any]]) -> Optional[float]:
        vals = [r.get("state") for r in rows if r.get("state") is not None]
        if len(vals) < 2:
            changes = [r.get("change") for r in rows if r.get("change") is not None]
            if changes:
                return float(sum(changes))
            return None
        return float(vals[-1]) - float(vals[0])

    low_d = _delta(low)
    high_d = _delta(high)
    lt_share = None
    if low_d is not None and high_d is not None and (low_d + high_d) != 0:
        lt_share = round(low_d / (low_d + high_d), 3)

    results = {
        "eheater_lt_kwh": round(eheater_kwh, 3),
        "lt_share": lt_share,
        "low_tariff_delta": low_d,
        "high_tariff_delta": high_d,
    }
    entry = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "period": f"{since}/{datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        "results": results,
        "verdict": None,
        "changes": None,
    }
    reviews = list(cfg.raw.get("reviews") or [])
    reviews.append(entry)
    cfg.raw["reviews"] = reviews
    save_config(cfg)
    print(yaml.safe_dump(entry, sort_keys=False))
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
    v.set_defaults(func=cmd_validate)

    s = sub.add_parser("show", help="Show resolved schedule")
    s.add_argument("--entity", default=None)
    s.add_argument("--group", default=None)
    s.add_argument("--day", default="all")
    s.add_argument("--format", choices=["ascii", "csv"], default="ascii")
    s.add_argument("--profile", default=None)
    s.set_defaults(func=cmd_show)

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
