"""Shared pytest helpers."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def root() -> Path:
    """Repo root.

    Returns:
        Path to the repository.
    """
    return ROOT


@pytest.fixture
def schedule_raw(root: Path) -> dict:
    """Deep copy of schedule.yaml.

    Args:
        root: Repo root.
    Returns:
        Mutable mapping.
    """
    return copy.deepcopy(yaml.safe_load((root / "schedule.yaml").read_text()))
