# Velair heating schedule agent

Generic guide for creating and tuning heating schedules with this tool.
All house-specific facts (entity roles, design notes, tariff, sensors,
expectations) live in `schedule.yaml` under `context`. Read that file
fully before changing anything. The yaml is the source of truth, not the
Home Assistant UI.

## Config layout (`schedule.yaml`)

- `context` — human notes for the next run: `summary`, `tariff`,
  `house`, `entity_roles`, `design_notes`, `collect_sensors`, `data_used`,
  `expectations`. Prose; not enforced by the script.
- `specifiers` — named time windows (`start`, `end`, `days`). A string
  value is an alias. Low-tariff windows start with `LT_`.
- `rules` — machine-checked constraints in two layers: `disable` (turn off
  a built-in by id) and `user` (house constraints). See Linters.
- `schedules` — key is a climate entity id or `group:<Name>`, value
  `{mode, temp_step, weekday: [...], weekend: [...]}` (or `all_days`).
- `groups`, `profiles`, `automations`, `legacy_disable`, `backup`.
- `reviews` — append-only log written by `collect`.

## Schedule nodes

- Node: `{time, temp?, hvac_mode?, preset_mode?}`. Missing `temp` means
  "no change" (preset-only). `hvac_mode: "off"` pauses temp checks.
- `time` is a clock `HH:MM` or a specifier expression:
  - `NAME+H:MM` — offset after the window start (`+` is always from start)
  - `NAME-H:MM` — offset before the window end (`-` is always from end)
  - `NAME.start` / `NAME.end` — the boundaries (further `+`/`-` allowed)
  - bare `LT+`/`LT-` — only when exactly one `LT_` window covers that day
- A ramp node auto-expands into stepped nodes. Anchor on **either** `by`
  (end-anchored) or `time` (start-anchored), never both:
  `{ramp_to, by|time, step_c?, every?}`. `step_c` defaults to the entity's
  `max_step_up` limit, so a ramp can never break the step rule.
- Resolver snaps temps to `temp_step`, rounds times to the 15-minute grid
  (`.end` setbacks floor, all else ceil), and sorts each day.

## Linters (run by `validate` and before every `apply`)

- Built-in: `grid_15min`, `unique_times`, `temp_bounds`,
  `temp_step_multiple`, `ramp_geometry`, `rule_threshold_sanity`,
  `midnight_transition`, `cycle_closed`, `noop_nodes` (warn).
- User rule types: `max_step_up` (`limit_c`, `min_gap_minutes`,
  `window_c`/`window_minutes`), `max_temp_outside` (`windows`, `above_c`),
  `no_overlap_above` (`above_c` scalar or per-entity map, plus
  `automations`), `forbid_node_keys`, `min_temp_at`/`max_temp_at`.
- Add or tighten constraints in the `rules` block, never in the script.
- `validate --strict` promotes warnings to errors.

## Climate Scheduler notes (v1.15)

- Schedules live on named groups; a single entity becomes an auto group
  named after its friendly name.
- Profiles are **global**. Each circuit needs its own active profile, or
  every group sharing `Default` gets the same nodes. `apply` creates and
  activates a `velair_<slug>` profile per circuit automatically.
- `get_schedule` / `get_groups` return node lists as JSON strings per day.

## Edit style

Move specifier offsets or the specifier window instead of pasting new
clock times. Append `reviews` (verdict and what changed); never delete
earlier `reviews` or `data_used`.

## Loop

```
make validate
make show ARGS='--entity climate.hc1 --day weekday'
make test
make collect ARGS='--since <date>'
make apply            # dry-run diff
make apply-yes        # only when asked
```

Connection comes from `.env` (`HA_URL`, `HA_TOKEN`) or `--url`/`--token`.
Never commit `.env` or `backups/`. `collect` reads the sensor list from
`context.collect_sensors`.
