# Velair heating schedule agent

Generic guide for creating and tuning heating schedules with this tool.
The goal: run several heating circuits on a heat pump so that heat goes
in during the low-tariff window, or in hours when the outdoor temperature
(and so COP) is higher, and the house then holds it through the day.

New house without `schedule.yaml`? Follow [`SETUP.md`](SETUP.md) first:
import from Home Assistant, then interview the user before writing config.

## Golden rules

- `schedule.yaml` is the source of truth, not the Home Assistant UI. Read
  it fully before changing anything, including `context`, `reviews` and
  `data_used`.
- All house facts (entity ids, sensors, tariff, targets) live in yaml.
  The script stays generic: never hardcode entity or statistic ids,
  aliases or house numbers in code. Add sensors under `collect`, add
  constraints under `rules`.
- Make small corrections, one idea per phase. Raise or move a few nodes,
  not the whole day. Defer the next idea to the next phase and say so in
  `context.expectations` or `reviews`.
- Never run `apply-yes` (or `restore --yes`) unless the user asks for it
  in this turn. A dry-run `apply` is always safe.
- Talking to Home Assistant is read-only except through `apply` and
  `restore`. Do not change entities or automations in the HA UI or
  through the API by hand.
- Never commit `.env`, `backups/` or `schedule.yaml`. Never auto-commit.

## Tuning workflow

1. **Read** `schedule.yaml`: `context.summary`, `entity_roles`,
   `design_notes`, `expectations`, the last few `reviews`.
2. **Measure.** Run `make collect ARGS='--since <date> --dry-run'` first;
   drop `--dry-run` to append a review. Compare against `expectations`.
   For room under/overshoot, run
   `make compare ARGS='--entity <climate> --day <day> --date YYYY-MM-DD'`
   (sched vs live setpt vs actual).
3. **Diagnose** before editing. Check the hour-by-hour story, not just
   daily totals: setpoints, supply and tank temps, pump modulation, heat
   pump mode, outdoor temp. Recorder statistics and history are fine for
   this, read-only.
4. **Note confounders** in `data_used.findings`: a house preheated before
   a deploy, a cold snap, a DHW cycle, a missed day. One day of data is a
   hint, not a verdict.
5. **Change** yaml with the edit style below. Never tune one circuit in
   isolation — see Whole-system scheduling. Then run
   `make validate ARGS='--strict'` and `make show` for each touched
   entity and day, and check the resolved clocks are what you meant.
6. **Dry-run** `make apply`. The diff must touch only what you intended.
   Unchanged circuits and automations must stay unchanged.
   Every schedule change reply shows: the new day timeline as a table (clock
   by circuit), a before/after table per changed circuit and day, and the sum
   of heat-hour change, split into in-LT and off-LT hours with signed deltas
   (e.g. +1 h, −1 h). LT = any LT specifier window that day. Heat hour = hour
   with setpoint above 15.5 °C (pool: on-hours). Degree-hours = sum of
   (setpoint − 15.5) × hours.
7. **Record.** Append a `reviews` entry: `period`, `results`, `verdict`,
   `changes`. Add `data_used` findings with the period and entities.
   Bump `context.updated`.
8. **Apply** only when asked, then tell the user the backup path.

## Whole-system scheduling

Never edit one heating circuit without the rest of the heat-source timeline
in view.

- Identify roles from `context.entity_roles` / `context.house` (floors,
  radiators, accumulation/buffer, DHW, HVAC, pool). Do not assume entity
  names.
- Before changing a day, `show` that day for every circuit that shares the
  heat pump, plus DHW / high-temp automations that can stack load.
- If an accumulation (buffer) circuit exists, preserve the handoff: room
  preheat first, buffer charge last in the cheap window. State the
  handoff clocks explicitly when you change a peer circuit.
- If there is no accumulation circuit, still check every other circuit on
  the same heat source; skip only buffer-specific checks.
- Check proposed high-demand intervals against `no_overlap_above`, DHW
  duration, tariff windows, and backup-heater risk. A green `validate` is
  necessary but not enough — walk the hour-by-hour combined timeline.
- After editing, `show` every changed entity and every interacting
  circuit/automation for each affected day. Say which schedules stay
  unchanged and why the combined timeline is still safe.
- Always show the new plan as tables: day timeline by circuit, before/after
  clocks per changed circuit and day, and heat-hour change split into
  in-LT and off-LT with signed deltas (see Dry-run step 6).
- Record that cross-circuit conclusion in `reviews` / `data_used`.

## Tuning heuristics (heat pump houses)

- Low-temp circuits (floors) are cheap to run and store heat in the
  building mass. Lead with them in the cheap window.
- High-temp requests (radiators, buffer tank, DHW) cost COP. Keep them
  apart in time (`no_overlap_above`) and keep steps small
  (`max_step_up`), or a backup electric heater may kick in.
- A small buffer tank holds little heat. If circuits pump from it while
  the compressor idles, it drains before the window ends. Preheat the
  circuits first, then charge the buffer last.
- End the preheat at the comfort target (or a little above), so rooms
  hold heat after the window. Annotate targets in
  `context.expectations`.
- A midday top-up in high tariff pays off only when the COP gain beats
  the price ratio (`tariff.lt_vs_ht`). Check it with `collect` data, per
  outdoor temperature.
- Judge results by energy per heating degree hour (kWh/HDH), COP and
  backup-heater kWh, not by raw kWh. Weather changes raw kWh.
- Treat compressor modulation and circuit-pump modulation as standard
  before/after evidence (`collect.history_metrics` or history reads).
  Avoid schedules that leave cheap or warmer hours idle and then compress
  demand into sustained 95–100% modulation later.
- Check the measured outdoor trajectory inside candidate tariff windows;
  prefer warmer hours for COP when repeated evidence supports it. Never
  assume a fixed local inflection hour, and do not optimize from one day
  alone — record variability and confounders in `data_used`.
- Preserve whole-system handoffs and overlap rules before shifting load
  earlier in a window.

## Config layout (`schedule.yaml`)

Start a new house from `schedule.example.yaml`.

- `context` — human notes for the next run: `summary`, `tariff`,
  `house`, `entity_roles`, `design_notes`, `data_used`, `expectations`.
  Prose; not enforced by the script.
- `specifiers` — named time windows (`start`, `end`, `days`). A string
  value is an alias. Low-tariff windows start with `LT_`.
- `rules` — machine-checked constraints in two layers: `disable` (turn off
  a built-in by id) and `user` (house constraints). See Linters.
- `schedules` — key is a climate entity id or `group:<Name>`, value
  `{mode, temp_step, weekday: [...], weekend: [...]}` (or `all_days`).
- `collect` — metrics and ratios for `collect`. Each metric names a
  `stat` id and `agg`:
  - `state_sum` — sum of hourly `state` (counters that report per hour)
  - `change_sum` — sum of hourly `change`
  - `delta` — last minus first `state` (cumulative meters)
  - `mean` — average of hourly `mean` (measurements)
  - `state_mean` — average of hourly `state`
  Ratios use `num` / `den`; `den` may be a list to sum. A missing or zero
  `den` gives `null`. Check which agg fits a sensor by looking at its raw
  statistic rows before trusting a number.
  Optional `collect.compare.<entity>` overrides history entity / attrs for
  `make compare` (defaults: climate entity, `current_temperature` /
  `temperature`).
  Optional `collect.history_metrics` pulls recorder history (not long-term
  statistics) for sensors such as modulation. Each entry needs `entity`
  and `agg` (`mean`, `min`, `max`, `hours_at_or_above` with `threshold`).
  Optional `windows` is a list of specifier names; intervals use the HA
  timezone from `/api/config`. Omit `windows` to use the full
  `--since`..now span. Results merge into the same `collect` review
  output as statistic metrics.
- `scheduler` — optional `{status_sensor, paused_state}` for `test`.
- `groups`, `profiles`, `climate_profiles`, `automations`,
  `legacy_disable`, `backup`.
- `profiles` — apply-time overlay into Default (`apply --profile`). Not a
  live Velair seasonal profile.
- `climate_profiles` — alternate Velair weeks selected by Home Assistant.
  Each entry has `mode`, unique `priority`, and `when`. Hold form uses
  `zones` (all-day temp). Offset form uses `from: default`, `offset_c`
  (negative), and `entities`. Exactly one profile has
  `when.heating_season: off` and must hold the highest priority.
- `automations.<name>` — required `type`:
  - `outdoor_gate`: `entity`, `outdoor_sensor`, `min_outdoor_c`
    (optional `id`, `fallback_script`, `alias`, `labels`)
  - `time_switch`: `id`, `triggers` (`at` specifier token + `weekday`
    list; optional `entity` + `temp_sensor` + `below_c`, `alias`,
    `labels`)
  - `season_select`: `id: velair_season_select`, `heating_season`,
    `outdoor`. Selects `select.velair_mode` by priority; never activates
    profiles from `apply` itself.
- `reviews` — append-only log, written by `collect` or by the agent.

## Seasonal climate profiles

- Read `climate_profiles`, conditions, priorities, and the last
  profile-related review before changing them.
- Treat `schedules` as Default and `climate_profiles` as alternate Velair
  profiles. Never use the existing `profiles` key for live seasonal
  profiles.
- Evaluate conditions by descending explicit priority. Off season must
  remain highest; Default is the final fallback.
- Keep profile changes small: one band, offset, or hold per phase. Do not
  silently alter Default schedules while tuning a profile.
- Run `make validate ARGS='--strict'`, then
  `make show ARGS='--climate-profile cold --entity climate.hc2 --day mon'`
  (and every affected key). Confirm clocks, snapped temps, expanded
  groups, and the priority winner.
- Dry-run `make apply` and verify only intended Default, profile, mode,
  and managed automation diffs. Apply must not activate, delete, or
  restore a profile. `apply --profile Summer` skips climate_profiles.
- Never run `apply-yes` unless asked in that turn. Record active profile,
  thresholds, priority, period, and result in `reviews` / `data_used`.

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
- A window that crosses midnight (`22:00`-`06:00`) puts its late nodes on
  the same calendar day's evening. Weekend days reset at `00:00`, so give
  them an explicit `00:00` node.

## Linters (run by `validate` and before every `apply`)

- Built-in: `grid_15min`, `unique_times`, `temp_bounds`,
  `temp_step_multiple`, `ramp_geometry`, `rule_threshold_sanity`,
  `midnight_transition`, `cycle_closed`, `noop_nodes` (warn),
  `config_schema` (collect + automation types).
- User rule types: `max_step_up` (`limit_c`, `min_gap_minutes`,
  `window_c`/`window_minutes`), `max_temp_outside` (`windows`, `above_c`),
  `no_overlap_above` (`above_c` scalar or per-entity map, plus
  `automations` with required `duration_minutes`), `forbid_node_keys`.
  `min_temp_at` / `max_temp_at` are accepted but not checked yet.
- Add or tighten constraints in the `rules` block, never in the script.
- If a linter blocks a change, fix the schedule, not the rule. Relax a
  rule only when the user agrees, and write the reason in `reason`.
- `validate --strict` promotes warnings to errors.

## Climate Scheduler notes (v1.15)

- Schedules live on named groups; a single entity becomes an auto group
  named after its friendly name.
- Profiles are **global**. Each circuit needs its own active profile, or
  every group sharing `Default` gets the same nodes. `apply` creates and
  activates a `velair_<slug>` profile per circuit automatically.
- `get_schedule` / `get_groups` return node lists as JSON strings per day.

## Edit style

- Move specifier offsets or the specifier window instead of pasting new
  clock times.
- Keep each day sorted by intent (window start to window end) so diffs
  read well.
- Append `reviews` (verdict and what changed); never delete earlier
  `reviews` or `data_used`.
- When you change resolved output that a golden test covers
  (`tests/expected_*.txt`), update the golden file in the same change.

## Commands

All commands run in docker via `make` (`docker compose`).

```
make validate ARGS='--strict'
make show ARGS='--entity climate.hc1 --day mon'
make show ARGS='--climate-profile cold --entity climate.hc2 --day mon'
make compare ARGS='--entity climate.hc2 --day sat --date 2026-11-21'
make check                          # offline pytest + fake HA
make test                           # HA connection, read-only
make collect ARGS='--since <date> --dry-run'
make collect ARGS='--since <date>'  # appends a review
make apply                          # dry-run diff, 0 writes
make apply-yes                      # only when asked; writes a backup
make restore ARGS='backups/<file>.yaml'
```

Connection comes from `.env` (`HA_URL`, `HA_TOKEN`) or `--url`/`--token`.
Global flags such as `--config` go before the subcommand.
