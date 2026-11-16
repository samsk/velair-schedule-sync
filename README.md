# velair-schedule-sync

Declarative heating schedules for Home Assistant.
`schedule.yaml` is the source of truth; the UI is not.

This repository was built and iterated with AI coding assistants under human
direction. Maintainer review applies before anything runs on a live home.

## [Velair](https://github.com/cgonfer/velair)

[Velair](https://github.com/cgonfer/velair) is the local-first HA integration
that runs weekly climate schedules (modes, profiles, panel, services). This
repo does not replace it — it adds a **config-as-code layer** on top:

- One versioned file for the whole house (circuits, tariff windows, design
  notes, DHW/pool automations) instead of editing each climate in the panel.
- **Specifier times** (`LT_workday+2:00`, ramps, aliases) tied to low-tariff
  windows — move a window once, every offset follows.
- **Linters** (built-in + per-house rules) catch step jumps, LT leaks, and
  heat-pump overlap before apply.
- **AI loop**: `AGENTS.md` + `context` in yaml; `validate` / `show` / `collect`
  / `reviews` for measured tuning.
- Docker `make` targets: dry-run diff, backup/restore, push schedules and
  linked automations to HA.

Velair (or compatible scheduler services) still executes schedules in HA;
this tool authors and syncs what gets pushed.

## Intended use

Author and tune schedules with an AI agent:

1. Point the agent at `AGENTS.md` (generic how-to: specifiers, ramps,
   linters, apply loop) and `schedule.yaml` (this house: entity roles,
   tariff, design notes, sensors).
2. Change yaml only — windows, offsets, temps, rules. Do not paste clock
   times that a specifier already names.
3. `make validate` / `make show` until linters pass and resolved nodes
   look right, then `make apply` (dry-run) and `make apply-yes` when
   asked.
4. After a stretch of live data, `make collect` appends a `reviews`
   entry. The agent reads that plus `context` to decide the next tweak.

The agent should never commit `.env` or `backups/`.

## What it does

- Resolves named windows (`LT_workday`, aliases, `NAME+H:MM` / `NAME-H:MM`)
  and expands `ramp_to` into 15-minute stepped nodes.
- Lints in two layers: built-in rules (disable individually) and
  house `rules.user` (step size, overlap, max temp outside LT, …).
- Syncs each circuit via the scheduler API (per-circuit profiles where the
  backend requires them, e.g. global shared profiles in Climate Scheduler).
- Can disable legacy scheduler switches and keep related automations
  in the same yaml.

## Run

Copy `.env.example` to `.env` and set `HA_URL` / `HA_TOKEN`. All commands
run via docker compose.

```bash
make build
make validate
make show ARGS='--entity climate.hc1 --day weekday'
make check              # offline pytest + fake HA blackbox
make test
make apply              # dry-run diff
make apply-yes          # write to HA
make collect ARGS='--since 2026-11-15'
make ha-up              # optional local HA on 127.0.0.1:8123
make test-ha            # smoke test; needs tests/ha.env (localhost only)
make help
```

See `AGENTS.md` for the agent brief.
