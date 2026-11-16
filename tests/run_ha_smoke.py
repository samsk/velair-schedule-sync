#!/usr/bin/env python3
"""Smoke-test against a local Home Assistant only."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / "tests" / "ha.env"


def load_env(path: Path) -> dict[str, str]:
    """Load KEY=VALUE lines.

    Args:
        path: Env file path.
    Returns:
        Env map.
    """
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def main() -> int:
    """Run test + apply --dry-run against localhost HA.

    Returns:
        Exit code.
    """
    if not ENV_PATH.exists():
        print(f"missing {ENV_PATH}; copy tests/ha.env.example", file=sys.stderr)
        return 2
    env = load_env(ENV_PATH)
    url = env.get("HA_URL") or ""
    token = env.get("HA_TOKEN") or ""
    host = (urlparse(url).hostname or "").lower()
    if host not in ("127.0.0.1", "localhost"):
        print(f"refusing non-local HA_URL host: {host!r}", file=sys.stderr)
        return 2
    if not token:
        print("HA_TOKEN missing in tests/ha.env", file=sys.stderr)
        return 2
    base = [
        sys.executable,
        str(ROOT / "sync.py"),
        "--url",
        url,
        "--token",
        token,
    ]
    for args in (["test"], ["apply", "--dry-run"]):
        print("+", " ".join(args))
        proc = subprocess.run(base + args, cwd=str(ROOT), check=False)
        if proc.returncode != 0:
            return proc.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
