#!/usr/bin/env python3
"""Export resolved apply payloads as JSON for browser/CDP cutover."""

from __future__ import annotations

import json
from pathlib import Path

from lib.config import load_config
from lib.resolve import nodes_to_cs_payload, resolve_day_nodes, schedule_keys
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
    # tuv triggers exact
    from lib.resolve import resolve_automation_trigger

    tuv = []
    for trig in (cfg.automations.get("tuv_charge") or {}).get("triggers") or []:
        days = list(trig.get("weekday") or [])
        for day, clock in resolve_automation_trigger(cfg, str(trig["at"]), days):
            tuv.append({"day": day, "at": clock, "token": trig["at"]})
    out["tuv_triggers"] = tuv
    out["pool_gate"] = cfg.automations.get("pool_gate")
    dest = ROOT / "payloads"
    dest.mkdir(exist_ok=True)
    (dest / "apply.json").write_text(json.dumps(out, indent=2))
    print(f"wrote {dest / 'apply.json'}")


if __name__ == "__main__":
    main()
