# Changelog

All notable changes to this project are documented in this file.

## [Unreleased]

### Added

- Seasonal Velair `climate_profiles`: derive alternate weeks from Default
  (offset) or all-day holds (off season), with explicit priorities.
- `season_select` automation type that picks `select.velair_mode` from
  heating-season and outdoor conditions (both outdoor edges; no HA-start
  select).
- Apply upserts Velair profiles and modes without activating, selecting,
  or deleting them; `apply --profile` skips climate-profile push.
- `show --climate-profile` and strict schema/lint for profile derivation,
  reserved mode names, and priority rules.
- Backup records profiles, modes, and active profile ids.
- AGENTS.md guidance for seasonal profile tuning.

## [0.2.0] - 2026-11-17

### Added

- Top-level `collect` metrics/ratios and review append workflow.
- Automation and collect config schema validation.
- Expanded `schedule.example.yaml` and heat-pump tuning guidance in
  `AGENTS.md`.

### Changed

- Makefile picks the available Docker Compose binary.
- `export_payloads.py` resolves automation time triggers.
- README clarifies purpose and setup (copy example config).

## [0.1.1] - 2026-11-16

### Added

- Automation label create/attach for managed automations.
- Stronger TUV charge automation conditions and actions.
- Optional local Home Assistant via `compose.ha.yaml`.
- Velair websocket API helpers and fake-HA blackbox tests.
- Docker `make` targets for validate, show, apply, restore, and check.

### Changed

- Sync path uses Velair services with clearer error handling.

## [0.1.0] - 2026-11-15

### Added

- Initial schedule resolve/lint/apply toolchain around `schedule.yaml`.
