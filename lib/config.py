"""Load and hold schedule.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml


@dataclass
class Specifier:
    """Named time window."""

    name: str
    days: List[str]
    start: str
    end: str


@dataclass
class ScheduleConfig:
    """Parsed schedule.yaml root."""

    path: Path
    raw: Dict[str, Any]
    version: int
    context: Dict[str, Any]
    specifiers: Dict[str, Union[Specifier, str]]
    rules: Dict[str, Any]
    reviews: List[Dict[str, Any]]
    schedules: Dict[str, Dict[str, Any]]
    profiles: Dict[str, Dict[str, Any]]
    groups: Dict[str, Dict[str, Any]]
    automations: Dict[str, Dict[str, Any]]
    legacy_disable: List[str]
    backup: Dict[str, Any]
    expectations: List[Dict[str, Any]] = field(default_factory=list)


def load_config(path: Path) -> ScheduleConfig:
    """Load schedule yaml into typed config.

    Args:
        path: Path to schedule.yaml.
    Returns:
        ScheduleConfig instance.
    """
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError("schedule.yaml must be a mapping")
    if raw.get("version") != 1:
        raise ValueError(f"unsupported version: {raw.get('version')!r}")

    specs_raw = raw.get("specifiers") or {}
    specs: Dict[str, Union[Specifier, str]] = {}
    for name, value in specs_raw.items():
        if isinstance(value, str):
            specs[name] = value
        elif isinstance(value, dict):
            specs[name] = Specifier(
                name=name,
                days=list(value["days"]),
                start=str(value["start"]),
                end=str(value["end"]),
            )
        else:
            raise ValueError(f"bad specifier {name!r}")

    context = raw.get("context") or {}
    expectations = list(context.get("expectations") or raw.get("expectations") or [])

    return ScheduleConfig(
        path=path,
        raw=raw,
        version=1,
        context=context,
        specifiers=specs,
        rules=raw.get("rules") or {},
        reviews=list(raw.get("reviews") or []),
        schedules=dict(raw.get("schedules") or {}),
        profiles=dict(raw.get("profiles") or {}),
        groups=dict(raw.get("groups") or {}),
        automations=dict(raw.get("automations") or {}),
        legacy_disable=list(raw.get("legacy_disable") or []),
        backup=dict(raw.get("backup") or {}),
        expectations=expectations,
    )


def save_config(cfg: ScheduleConfig) -> None:
    """Write raw mapping back to disk.

    Args:
        cfg: Config whose raw dict is written to cfg.path.
    """
    cfg.path.write_text(yaml.safe_dump(cfg.raw, sort_keys=False, allow_unicode=True))


def resolve_alias(
    specs: Dict[str, Union[Specifier, str]], name: str, seen: Optional[set] = None
) -> Specifier:
    """Follow alias chain to a concrete Specifier.

    Args:
        specs: Specifier map.
        name: Specifier or alias name.
        seen: Cycle detection set.
    Returns:
        Concrete Specifier.
    """
    seen = seen or set()
    if name in seen:
        raise ValueError(f"specifier cycle: {name}")
    seen.add(name)
    value = specs.get(name)
    if value is None:
        raise ValueError(f"unknown specifier: {name}")
    if isinstance(value, Specifier):
        return value
    return resolve_alias(specs, value, seen)


def default_step_c(cfg: ScheduleConfig, entity: str) -> float:
    """Default ramp step from entity max_step_up rule.

    Args:
        cfg: Loaded config.
        entity: Climate entity id.
    Returns:
        Step Celsius, default 2.5.
    """
    for rule in cfg.rules.get("user") or []:
        if rule.get("type") != "max_step_up":
            continue
        if entity in (rule.get("entities") or []):
            return float(rule.get("limit_c", 2.5))
    return 2.5


def climate_entities(cfg: ScheduleConfig) -> List[str]:
    """List climate entity ids referenced by schedules and groups.

    Args:
        cfg: Loaded config.
    Returns:
        Deduplicated entity ids.
    """
    out: List[str] = []
    for key in cfg.schedules:
        if key.startswith("group:"):
            name = key.split(":", 1)[1]
            ents = (cfg.groups.get(name) or {}).get("entities") or []
            out.extend(ents)
        else:
            out.append(key)
    for group in cfg.groups.values():
        out.extend(group.get("entities") or [])
    # unique preserve order
    seen = set()
    uniq = []
    for e in out:
        if e not in seen:
            seen.add(e)
            uniq.append(e)
    return uniq
