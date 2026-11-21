# Initial setup: new `schedule.yaml`

Use this guide only when a house has no `schedule.yaml` yet. For tuning an
existing config, follow `AGENTS.md`.

Goal: collect enough facts to write a first schedule that is safe (no
backup-heater spikes, no stacked high-temp loads) and comfortable, then let
`collect` reviews tune it.

## Rules

- Import first, ask second. Read everything Home Assistant can tell you, then
 ask the user only what HA cannot answer, and confirm what you imported.
- Ask in small batches (3–6 questions) with `AskQuestion` where answers are
 choices. Group by topic below. Do not dump the whole list at once.
- Get as many answers as possible. When the user does not know, record the
 default you chose and why in `context.design_notes`, and mark it as an
 assumption to verify with `collect`.
- All reads are read-only. No `apply-yes` during setup unless the user asks.
- Start from `schedule.example.yaml`. Keep house facts in yaml, never in code.

## Step 1: Import from Home Assistant (read-only)

Run `make test` first to confirm `.env` (`HA_URL`, `HA_TOKEN`) works.

Import or list:

- **Climate entities**: every `climate.*` from `/api/states` with
 `friendly_name`, `min_temp`, `max_temp`, `target_temp_step`, `hvac_modes`,
 `preset_modes`, current setpoint and room temperature. These become
 `schedules` keys and `temp_step`.
- **Existing schedules**: `make backup` saves the live Velair / Climate
 Scheduler schedules, groups and profiles to `backups/`. Use them as the
 starting point for nodes, and ask the user if they are still wanted.
- **Groups and areas**: Velair groups, plus HA areas of each climate
 (room names help the usage questions).
- **Automations** touching heating: DHW charge, outdoor gates, pool, season
 or tariff switches, anything that sets `climate.*` or heat-pump switches.
 Note their ids and triggers; decide later whether yaml manages them
 (`automations`) or they stay untouched.
- **Sensors** for `collect`: outdoor temperature, heat-pump consumed and
 produced energy, compressor energy, backup / electric heater energy,
 heating degree hours, COP, tank (buffer, DHW) temperatures, compressor and
 circuit-pump modulation, tariff meter readings. Check which have recorder
 statistics (`metrics`) and which only have history (`history_metrics`).
- **Tariff signal**: a tariff sensor or switch (HT/LT state) if present.
 Its history shows the real LT/HT clocks.
- **Recent history** (7–14 days): setpoints, room temps, outdoor temp,
 modulation. Shows how the house behaves today and which rooms are cold.
- **HA config**: `time_zone` from `/api/config`.

Show the user a summary table of what you imported and ask them to confirm
or correct it.

## Step 2: Ask the user

### Heat source

- Heat pump type (air/water, ground), brand and model, rated power.
- Is there a backup electric heater (in the HP or in a tank)? When does it
 kick in? Which sensor measures it?
- Other heat sources: electric radiators, fireplace, gas boiler, solar.
- Known limits: max supply temperature, minimum run time, defrost issues,
 noise limits at night.

### Circuits (one batch per circuit)

For each climate entity:

- What does it heat: rooms, floor, which storey?
- Type: underfloor (low temp, slow), radiators (high temp, fast), buffer
 tank, DHW, HVAC/air, pool, electric backstop.
- Is it on the heat pump directly, through a mixer, or from a buffer?
- Target comfort temperature, and the acceptable minimum when unused
 (setback). Different targets for workdays and weekends?
- How fast does it respond (minutes vs hours)? How long does the room hold
 heat after it stops?
- Any known cold or overheating problem today?

### Interactions between circuits

- Which circuits share the heat pump or a buffer?
- Which ones must not run at high temperature at the same time (they cause
 backup heater or low supply temp)? These become `no_overlap_above`.
- Does one circuit partially heat another room (e.g. a floor warming an
 attic room with an electric backstop)?
- Buffer or accumulation tank: size in litres, what drains it, should it be
 charged last in the cheap window?
- DHW: when does it charge, how long (minutes), what triggers it? It
 becomes an `automations` entry with `duration_minutes` for overlap rules.

### Room usage

- For each room or circuit: who uses it and when, on workdays and on
 weekends (wake-up, leave, return, bedtime)?
- Rooms unused on weekends (office) or on workdays (guest room, hobby)?
- Home office days? Which days?
- Bedrooms cooler at night? Bathroom warmer in the morning?
- Holidays or seasonal patterns (away weeks, summer pool use).

### Priorities

- Which circuit is most important on workdays? On weekends?
- When the heat pump cannot do everything at once, what goes first?
- Comfort vs cost: may some rooms drop below target to save money? Which?

### Tariff

- Electricity product name, HT and LT prices (per kWh, incl. VAT).
- LT/HT clocks per day type (workdays, weekends, holidays). Do they cross
 midnight? Are they fixed or signalled by the grid?
- Any other price signal (spot price, export, solar surplus)?
- These become `specifiers` (`LT_` prefix) and `context.tariff`.

### House

- Size, insulation, building mass (heavy brick/concrete vs light).
- Location climate: typical winter outdoor temps, design temperature.
- Season: when does heating start and stop? Any heating-season sensor?

### Existing setup and constraints

- Existing schedules (imported in Step 1): keep, adapt, or replace?
- Automations that must stay untouched by this tool.
- Anything the user wants never changed (e.g. a room always at 22).
- Who else adjusts thermostats by hand, and should that be respected?

### Measurement

- Which sensors are trusted for energy, COP, outdoor temp?
- What does success look like: lower cost, fewer backup-heater kWh, steady
 comfort, fewer complaints?

## Step 3: Write the config

- `context`: summary, tariff, house, `entity_roles` (one line per climate),
 `design_notes` (interactions, priorities, assumptions), `expectations`
 (comfort targets per circuit and time, backup heater = 0, LT share).
- `specifiers`: LT/HT windows and room-usage windows.
- `rules.user`: `max_step_up` for high-temp circuits and buffers,
 `no_overlap_above` for loads that must not stack, `max_temp_outside` for
 buffers charged only in LT.
- `schedules`: low-temp floors lead in the cheap window, high-temp circuits
 alone, buffer last. Setbacks for unused rooms. Use specifier offsets, not
 clock times.
- `automations`: only those the user wants managed.
- `collect`: `metrics`, `ratios` (COP, kWh/HDH, LT share), and
 `history_metrics` for modulation, tank and outdoor windows.

## Step 4: Verify and hand over

- `make validate ARGS='--strict'`, `make show` for every circuit and day
 type, and walk the combined hour timeline (see `AGENTS.md`).
- Show the user the new schedule as tables per `AGENTS.md`: timeline by
 circuit, before/after vs imported schedule, heat hours in-LT and off-LT
 with signed deltas.
- `make apply` dry-run. Apply only when the user asks.
- Append the first `reviews` entry: what was imported, what the user
 answered, which defaults are assumptions. Run `make collect` after 2–3
 days and tune with `AGENTS.md`.
