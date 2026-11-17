#!/usr/bin/env python3
"""Export resolved apply payloads as JSON for browser/CDP cutover."""

from __future__ import annotations

import json
from pathlib import Path

from lib.config import load_config
from lib.resolve import nodes_to_cs_payload, resolve_day_nodes, resolve_time_triggers, schedule_keys
from lib.timeutil import DAYS

ROOT = Path(__file__).resolve().parent


def main() -> None:
    """Write payloads/apply.json with set_schedule bodies.

    Returns:
        None. Writes payloads/apply.json.
    """
    cfg = load_config(ROOT / "schedule.yaml")
    out = {"schedules": {}, "groups": cfg.groups, "legacy_disable": cfg.legacy_disable}
    for key in schedule_keys(cfg):
        sched = cfg.schedules[key]
        mode = sched.get("mode") or "5/2"
        entry = {"mode": mode, "days": {}}
        if mode == "5/2":
            for label, rep in (("weekday", "mon"), ("weekend", "sat")):
                nodes = resolve_day_nodes(cfg, key, rep)
                entry["days"][label] = nodes_to_cs_payload(nodes)
        elif mode == "all_days":
            nodes = resolve_day_nodes(cfg, key, "mon")
            entry["days"]["all_days"] = nodes_to_cs_payload(nodes)
        else:
            for day in DAYS:
                nodes = resolve_day_nodes(cfg, key, day)
                entry["days"][day] = nodes_to_cs_payload(nodes)
        out["schedules"][key] = entry

    autos = {}
    for name, auto in (cfg.automations or {}).items():
        if not isinstance(auto, dict):
            continue
        item = dict(auto)
        if auto.get("triggers"):
            item["resolved_triggers"] = resolve_time_triggers(cfg, auto)
        autos[name] = item
    out["automations"] = autos

    dest = ROOT / "payloads"
    dest.mkdir(exist_ok=True)
    (dest / "apply.json").write_text(json.dumps(out, indent=2))
    print(f"wrote {dest / 'apply.json'}")


if __name__ == "__main__":
    main()
