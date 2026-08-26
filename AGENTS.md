# AGENTS.md — context & hard-won knowledge for ha-iletcomfort

Guidance for AI agents (and humans) working on this repo. Read this **before** triaging issues or
changing the decoder. It records domain knowledge and, crucially, the **wrong turns we've already
made** so they aren't repeated.

---

## 1. What this integration is (scope)

A Home Assistant integration for **iLetComfort (ITS / BTRI) heat pumps** via the **Midea Dollin cloud**.

**It emulates the iLetComfort app** — it speaks the iLetComfort/Dollin **cloud protocol**, doing what
the app does. The protocol was originally reverse-engineered from the **iLetComfort iOS app** (see
[`tgenov/iletcomfort-cli`](https://github.com/tgenov/iletcomfort-cli)).

**Scope boundary is the cloud/app protocol layer, not the device.** A heat pump may be reachable by
several vendor apps (iLetComfort, SMARTHOME, …); each app uses its **own cloud protocol** even when the
heat pump shares the same underlying device protocol (Midea `0xC3`).

- **In-scope test:** does the device work in the **iLetComfort app**? If yes → in scope.
- **Do NOT add a second cloud/protocol.** We will not implement Midea **MSmartHome** (`*.appsmb.com`)
  support. A device that works *only* via a Midea/MSmartHome app (and not iLetComfort) → out of scope;
  point the user to a Midea integration (`midea_ac_lan`, `midea-local`).

---

## 2. Wrong turns we made — DO NOT REPEAT

These are real mistakes from the project history. Each cost a public retraction.

### 2a. "It's a Midea device, therefore out of scope" — WRONG
A reporter (KJRH-120L, #21/#5) shared a capture from the **SMARTHOME** app hitting `appsmb.com`. It was
concluded the device was "Midea-only / out of scope" and the issues were closed.
**That was false** — the reporter's **screenshots showed the unit working in the iLetComfort app too.**
- **Lesson:** A SMARTHOME / `appsmb.com` capture, or the device also working on Midea, does **not**
  prove out-of-scope. **Verify whether it works in the iLetComfort app first** (re-read the thread /
  screenshots). Only out of scope if it does **not** work via iLetComfort at all.

### 2b. "The app uses certificate pinning, so you need a rooted device" — WRONG
A reporter's Android proxy attempt showed an error and a `14005` response; it was concluded the app pins
its TLS cert and a rooted-Android + Frida bypass was needed.
**That was false:**
- The original protocol RE was done on **iPhone + mitmproxy with NO pinning** in the way.
- The reporter had **actually decrypted traffic** (a `bizGroup:msmart,datakey` request + JSON response
  came through the proxy in the clear). **True pinning shows only TLS handshake failures, never a
  decrypted request/response.**
- The real blocker was the **`14005` single-active-session** response (auth), not pinning.
- **Lesson:** Don't claim pinning unless you see handshake failures with **no** decrypted bodies. If a
  request/response decrypts, the proxy works. The known-good capture setup is **iPhone + mitmproxy**.

### 2c. Don't gate model decoding on a full/per-device serial
The original instinct to gate on the device serial was correctly rejected (targets one device). See §4.
**Lesson:** gate on the `sn8` **model code** (shared by all units of a model), default unknown → STANDARD.

### Meta-lesson
Both 2a and 2b came from **over-reading one new signal instead of reconciling it with facts already in
the thread.** Before asserting a root cause as settled — especially when closing an issue or telling a
user to do hard work — re-read the whole thread and check the new signal against what's already known.

---

## 3. Cloud protocol & error codes

- Auth/login + `list_appliances` happen first; the C3 status/sensor queries follow.
- **`code=1214` ("System error")**: the Dollin cloud **rejects our C3 hex command** for a device
  (raised in `ILetComfortClient.send_hex_command`). It is **not** auth, not offline, not a parsing bug,
  and **not proof of out-of-scope**. It means the command we send isn't what the cloud accepts for that
  model — the app sends something different. Fixing it requires capturing what the iLetComfort app sends.
- **Truncated C3 frame** (e.g. `aa,0b,c3,…,01,2e`): the cloud relays the command but the device returns
  a near-empty stub instead of a full status frame. Same family of "this model doesn't answer our
  standard query" as `1214` (see #5 vs #21).
- **`code=14005`**: token rejected / **single active session**. The cloud allows **one active login per
  account** — logging in from the app invalidates HA's token and vice versa (the "login war"). The
  integration auto-re-auths on `14005`/`12001`. Workaround for users: give HA its own account and share
  the device to it (documented in `README.md` / `docs/TROUBLESHOOTING.md`). `14xxx` codes are the
  auth range.

---

## 4. Decoding, model variants & `sn8` profiles

Heat-pump models pack the status frame (C3 subtype `0x01`) at **different byte offsets**. The cloud
appliance metadata has **NO device-class field**: both an ATW and an ATA unit report
`applianceType="0xC3"` and `modelNumber="0"`. The **only** differentiator is **`sn8`** — the 8-char
model-code serial prefix (e.g. `171H120F` vs `171000AU`). The full per-device serial `sn` is **redacted**
and never stored.

**Model-specific decoding is gated on an `sn8 → profile` table** (`custom_components/iletcomfort/model_profiles.py`):
- `resolve_profile(sn8)` → `ModelProfile.{STANDARD,ATW,AQUAPURA,KJRH120L,AQUAPURA_SPLIT_GREEN}`.
  **Unknown/missing sn8 → STANDARD
  (unchanged today's behavior).** So a new model can never be *corrupted* — worst case it doesn't get a
  profile yet. Caveat: `sn8` is assumed model-level (shared across units of a model); only ever confirmed
  on one unit per model so far.

Key decode constants (`api.py`): base offset `d=1` (body[0] is the subtype byte);
`_temp_offset(raw) = raw - TEMP_OFFSET` with `TEMP_OFFSET=35`, returning `None` for
`SENSOR_DISCONNECTED=204`; `STATUS_MIN_BODY_LEN=6`.

### ATW profile (`sn8 171H120F`, Italtherm air-to-water, #22) — hardware-validated
Status `raw_body` (0-indexed; STANDARD misreads this 25-byte frame):
- `byte[8]`  = DHW setpoint, **direct °C** (`ATW_DHW_SETPOINT_INDEX`).
- `byte[9]`  = Zone-1 setpoint **×2** (0.5° resolution) → divide by 2.
- `byte[22]` = DHW tank current temp, **direct °C** (`ATW_DHW_TANK_TEMP_INDEX`).
- `byte[1]`  = flags (`ATW_FLAGS_INDEX`); bit `0x01` = space-heat demand (`ATW_SPACE_HEAT_DEMAND_BIT`).
- `byte[24]` = constant `0x80` flags/MSB byte — **NOT an error code** (STANDARD wrongly read it as 128).
- ATW sets `error_code=0` and `comp_running=False` (no confirmed running signal in this short frame).
- `decode_atw_status(body)` implements this. Uncertain HVAC mode/action is left conservative — validate
  on hardware before adding.

### AQUAPURA_SPLIT_GREEN profile (`sn8 17186T3A`, Energie Aquapura Split Green HPWH, KJR-86T3)
This model **answers neither standard query**: the long C3 query *and* the KJRH-120L short form are
**echoed back verbatim** (code 0, `data == command`). Its app sends fully-formed **21-byte selector
frames** and reads each data group from a **separate** frame:

    aa 14 c3 00 00 00 00 00 00 03 00 <sel> <param> ff ff ff ff 00 ff ff <cks>

`build_aquapura_split_green_query(sel, param)` reproduces all eight captured commands byte-exact.
`build_query_command` maps `0x01` → selector `01,f4` (STATUS) and `0x02` → `03,84` (**TANK TEMP — note it is
under selector 0x03, not the standard sensors 0x02**). `02,58` (daily schedule) is fetched separately by
`ILetComfortClient.query_daily_schedule`, gated to this profile (every other/unknown sn8 returns `[]` with
no network call). Other selectors captured but unused: `00,00` identity, `00,64` config/limits, `01,90`
timers/errors, `02,62` a *second*, differently-laid-out schedule frame, `03,e8` ODU sensors.

Confirmed against **two** captures — A (tank 49 °C, ambient 21 °C) and B (tank 48 °C, ambient 20 °C),
both unit OFF:
- TANK body[30:32] = tank temp, **16-bit BE tenths** (A `0x01ec` → 49.2, B `0x01e7` → 48.7) → `th_temp`.
  The value moved with the real tank, so this frame is **live, not cached**.
- ODU body[49:51] = outdoor ambient, same encoding (A `0x00d4` → 21.2, B `0x00d0` → 20.8) → `t4_temp`.
- **The app truncates, it does not round** (49.2→"49", 48.7→"48", 20.8→"20"). That rule is what pins the
  ambient: under it, body[49:51] is the only 16-bit pair in the 225-byte ODU frame consistent with both
  captures (neighbours read 32.6/25.3/21.8 °C in B). We surface the true tenths, not the truncation.
- Frame boilerplate: body[3..11] (raw idx13..21) is identical in every frame. Raw idx21 = `0x15` is a
  **constant, NOT ambient 21 °C** — an early coincidence, ruled out by frame comparison.
- Live temps use `0x7fff` for "sensor absent"; `_aquapura_split_green_temp16` also rejects implausible
  values so a padded/stub frame reports nothing rather than 6553.5 °C.

**STATUS setpoint — WRONG TURN, then corrected.** The very first capture (unit OFF, app-shown 50 °C) had
body[27] == `0x32` (=50, direct °C), and that single coincidental match was reported as "the DHW
setpoint". A capture series that actually **changed** the setpoint (50→51→52→50 via the app, mitmproxy
request+response each time) proved that wrong: body[27] **never moved** — it stayed `0x32` in every
capture regardless of the real setpoint. The byte that tracks the live setpoint exactly is **body[15:17]**,
16-bit BE **tenths** (`0x01fe`→51.0, `0x0208`→52.0, `0x01f4`→50.0 — all three match the app request to the
decimal). body[27] is left undecoded (constant across every capture so far; true meaning unconfirmed —
maybe a limit, maybe stale). **Lesson (see §2/meta-lesson above): one coincidental byte match from a
single capture is not confirmation** — it takes a capture that actually changes the value to tell a real
field from a constant that happens to match.

**Two commands per sensors poll.** The tank and the ambient live in different frames, so
`_query_aquapura_split_green_sensors` fetches `03,84` then `03,e8` and merges them. The ODU fetch is
**best-effort** (AuthError still propagates): losing it costs the ambient, while losing the tank temp
would blank the climate entity and force the cached-data fallback.

**Daily schedule ("Tempor. diário", `02,58`)** — fully decoded and validated byte-for-byte against a
live app screenshot showing all 4 "Temporiz." slots (1 enabled, 3 disabled, all ECO). Each slot is an
8-byte block at `body[45 + n*8]` (`decode_aquapura_split_green_daily_schedule`,
`AquapuraSplitGreenScheduleSlot`): `[active, mode_marker(0x40="Eco"), setpoint_hi, setpoint_lo, start_h,
start_m, end_h, end_m]`. Disabled slots still carry their real setpoint/times — matches the app, which
shows a disabled slot's config too. Fetched by a **separate, best-effort** poll
(`ILetComfortCoordinator._poll` → `client.query_daily_schedule`), stored as `coordinator.data["schedule"]`
(a `list[AquapuraSplitGreenScheduleSlot]`, `[]` for every other profile — no network call). Surfaced as
20 entities: `sensor.py` has `Daily Schedule {1..4} {Setpoint,Start Time,End Time,Mode}` (16), and
`binary_sensor.py` has `Daily Schedule {1..4} Active` (4) — booleans went to `binary_sensor` per the
existing split (`compressor_running`, `error`, …), not `sensor`. Registered unconditionally for every
device (matches the KJRH-120L-suppressed-temps precedent): value_fns index into `data["schedule"]` and
return `None`/`False` when the slot/list is absent, so non-Split-Green devices just show them
unknown/off rather than needing profile-conditional entity registration. All 20 carry
`entity_category=CONFIG` — they represent the device's own timer configuration, so HA groups them
into the device page's "Configuration" section, distinct from Sensors (live values) and Diagnostic
(internal telemetry, §4 entity-category table above).

**`hvac_modes` restricted to Off/Heat.** The climate entity's default `_attr_hvac_modes` (`Off, Heat,
Cool, Fan-only`, shared by STANDARD/ATW/AQUAPURA/KJRH-120L) is overridden by an `hvac_modes` property
for this profile: only power Off/Heat is confirmed (STATUS body[13]), so the Split Green is a DHW-only
heat pump and Cool/Fan-only must not be offered as choices the device can't actually do.

**Power — confirmed.** A real ON/OFF mitmproxy capture pair (SET request + STATUS response, both states)
pins **STATUS body[13]** (raw idx23) as the power bit (`0x00`=Off, `0x01`=On); diffing the two response
frames shows it's the **only** byte that changes. `mode` is reported 0/"Off" or 1/"Heat" (same convention
as KJRH-120L: query mode 1 → `HVACMode.HEAT` in climate.py, a non-off state, making the card read ON and
the Off button usable).

**Operating mode ("Eco"/"Disparo") — confirmed.** A real mode-toggle mitmproxy capture pair pins
**STATUS body[14]** (raw idx24) as the marker (`0x40`="Eco", `0x80`="Disparo") — the **same** marker byte
and vocabulary as a daily-schedule slot's mode byte (§ above), and again the *only* byte that changes
between the two responses. Surfaced on `ITSStatus.operating_mode` (a field only this profile populates)
and, in `climate.py`, as the climate entity's **preset_mode** (`AQUAPURA_SPLIT_GREEN_PRESET_MODES =
["Eco", "Disparo"]`) — deliberately part of the climate card (alongside the existing Off/Heat
`hvac_mode`), not a separate select entity, so it reads as one set of "modes" on one card. Declared
unconditionally on the single shared `ILetComfortClimate` class, like `hvac_modes`; other profiles never
populate `operating_mode`, so `preset_mode` just reads `None` (no preset) for them.

**Writes — all confirmed.** Every captured single-field write (power, operating mode, setpoint, silence)
shares ONE frame shape, differing only in field id / value length / value:

    aa <len> c3 00×6 02 00 01 f4 ff ff ff ff 01 ff ff 00 <fld> <vlen> <value...> <cks>

    fld 0x0b, vlen 1: power        (0x01 On / 0x00 Off)
    fld 0x0c, vlen 1: op. mode     (0x40 Eco / 0x80 Disparo)
    fld 0x0d, vlen 2: DHW setpoint (16-bit BE tenths °C, e.g. 0x01fe = 51.0)
    fld 0x0e, vlen 1: silence      (0x01 On / 0x00 Off)

Confirmed byte-exact against 9 real captures (2 power, 2 mode, 3 setpoint: 50→51→52→50, 2 silence) via
`_build_aquapura_split_green_write`. Same checksum formula as the query frames. header[9] = `0x02` here (a
SET/control request) vs `0x03` for a plain query — this also explains an earlier oddity: a schedule-frame
capture taken right after an app write also had raw[9]=`0x02`, most likely echoing "this exchange involved
a write", not a data field. `api.py`'s `_set_device_aquapura_split_green` wires all four in, with priority
`silence` > `operating_mode` > `temperature` > power on/off (the app only ever writes one field per action,
so this priority is defensive rather than load-bearing — mirrors `_set_device_kjrh120l`'s equivalent rule).

**Silence (quiet mode) — confirmed.** A real ON/OFF mitmproxy capture pair pins **STATUS body[17]** as the
toggle (`0x00`=Off, `0x01`=On) — diffing the two response frames shows it's the only byte that changes.
Surfaced on `ITSStatus.silence` (a field only this profile populates) and, in `switch.py`, as the
**Silence** switch — declared unconditionally like the Boost switch; other profiles never populate
`silence`, so `is_on` just reads `False` for them.

**Disinfection ("Desinfecção") — confirmed, DIFFERENT frame shape from the single-field writes above.**
Lives in the **02,58 frame** (the same selector as the daily schedule — see § above), at
`body[38:44]`, right before the first schedule slot (`body[45]`):

    body[38]    = enabled (0x00/0x01)
    body[39:41] = hour, minute (direct, e.g. 0x0e,0x00 -> "14:00")
    body[41:43] = temperature, 16-bit BE tenths °C (0x028a -> 65.0)
    body[43]    = cycle days (0x07 -> every 7 days)

Confirmed by a 5-capture series (open the app's Desinfecção submenu, then toggle ON→OFF, change temp
65→68 °C, change hour 14→13, change cycle 7→6): each step changed **exactly** the one corresponding byte.
`body[44]` (constant `0x04` throughout) is left unmapped. The **write** frame is a genuinely different
shape from the single-field writes: the app always sends **all five fields together**, and each field
entry is padded with one extra `0x00` byte — **except the last field**, which has none:

    aa <len> c3 00×6 02 00 02 58 ff ff ff ff 01 ff ff 00
        24 01 <enabled> 00  25 01 <hour> 00  26 01 <minute> 00  27 02 <temp hi> <temp lo> 00  28 01 <cycle> <cks>

`build_aquapura_split_green_disinfection_command` reproduces this byte-exact. Because the write always
carries every field, a caller changing only `enabled` (the **Disinfection** switch) must merge in the
current hour/minute/temperature/cycle_days — it does NOT default them, since sending zeros would actually
reset the device's real schedule. `api.py`'s `query_disinfection`/`set_disinfection` and
`coordinator.async_set_disinfection` implement this; the switch reads current values from
`coordinator.data["disinfection"]` and raises rather than guessing if that's not populated yet. Gated on
the same `CONF_FETCH_SCHEDULE` option as the daily schedule (same source frame, same "extra poll" cost
tradeoff), so it costs one extra command per poll — the tank/ODU sensors already established two commands
per poll as an accepted cost for this profile.

**Still not decoded / not writable:**
1. body[27]: real data (constant `0x32` across every capture so far, including ones where the real
   setpoint moved), no confirmed meaning.
2. The ODU frame's other sensors and the config frame's (`00,64`) limits are real data with no app label
   to validate against — left unmapped rather than guessed.
3. Setpoint/power/mode **limits**: no app-confirmed min/max capture exists, so the climate entity reuses
   the KJRH-120L's validated DHW range (`AQUAPURA_SPLIT_GREEN_TEMP_MIN/MAX` = 20-70 °C) as a conservative
   bound — wide enough for the captured 50-52 °C values without guessing a tighter one.

### AQUAPURA profile (`sn8 171000AU`, AQS Energie split HPWH, #12)
- The real water/tank temp is in `status.box_bottom_temp` (status byte[17], offset-decoded → e.g. 40 °C).
- The standard `sensors.twin_temp`/`twout_temp` (sensors bytes 25–26) are `0x23` null-fill → decode to 0
  (that was the "water temp = 0" bug).

### Entity routing (important)
- `sensor.py`: **"Water Inlet Temperature"** ← `twin_temp`; **"DHW Tank Temperature"** ← `th_temp`.
- `climate.py` `current_temperature` is **profile-aware**: `th_temp` for
  ATW/AQUAPURA/AQUAPURA_SPLIT_GREEN, `twin_temp` for STANDARD.
- For ATW/AQUAPURA/AQUAPURA_SPLIT_GREEN the tank temp is routed into **`th_temp`** (so the
  correctly-named "DHW Tank Temperature" entity shows it). Do **not** route a tank reading into
  `twin_temp` (that mislabels "Water Inlet"). This was corrected after a reporter flagged it.

---

## 5. Code map

| File | Responsibility |
|------|----------------|
| `api.py` | `ILetComfortClient` (login, `list_appliances`, `send_hex_command`, `query_status`/`query_sensors`); frame build/parse (`build_c3_query`, `build_c3_set`, `parse_hex_response`, `extract_c3_body`); decoders `decode_its_status`/`decode_its_sensors` + dataclasses `ITSStatus`/`ITSSensors`; `_temp_offset`; `AuthError`/`ApiError`. |
| `model_profiles.py` | `ModelProfile` enum, `_SN8_PROFILES` table, `resolve_profile`, `decode_atw_status`, `apply_profile_to_status`, `apply_profile_to_sensors`. **Add new model support here.** |
| `coordinator.py` | `ILetComfortCoordinator`: polling, re-auth, cache-fallback, offline Repair card. Caches `appliance_meta` (best-effort, never fatal) and exposes `sn8`; threads profile into decode. Reads `entry.options` for poll interval + fetch toggles (§7). |
| `diagnostics.py` | Redacted snapshot: raw frames, decoded status/sensors, `sensors_temperature_scan` (per-byte `_temp_offset` map — use it to find a model's misplaced temp byte), and the `appliance` metadata block. `APPLIANCE_TO_REDACT = {owner, sn, name}` (keeps `applianceType`/`modelNumber`/`sn8`). |
| `climate.py` / `sensor.py` / `binary_sensor.py` / `switch.py` / `select.py` | HA entities. See §4 for which field backs which entity. |
| `config_flow.py` / `__init__.py` / `const.py` / `entity.py` | Setup, entry, constants, base entity, options flow (§7). |
| `tests/` | pytest suite; real captured frames are pinned as fixtures (e.g. issue-#11 frames for STANDARD regression). |

---

## 6a. Options flow (poll interval, per-poll fetch toggles)

`config_flow.py`'s `ILetComfortOptionsFlow` (accessed via the integration's **Configure** button) exposes
three settings, all under `entry.options` (never `entry.data` — options don't require re-auth):
- `scan_interval` (`homeassistant.const.CONF_SCAN_INTERVAL`, default `DEFAULT_SCAN_INTERVAL`=60s, floor
  `MIN_SCAN_INTERVAL`=30s) — the coordinator's `update_interval`.
- `fetch_diagnostics` (default `True`) — forwarded to `client.query_sensors(..., fetch_diagnostics=...)`.
- `fetch_schedule` (default `True`) — gates whether `_poll()` calls `client.query_daily_schedule` at all.

**Both fetch toggles only affect the Aquapura Split Green** (sn8 `17186T3A`): every other profile's
status/sensors poll is already the minimum two commands regardless of these flags — flipping them off
for a STANDARD/ATW/AQUAPURA/KJRH-120L device is a harmless no-op. For the Split Green specifically:
- `fetch_diagnostics=False` skips the second (ODU/ambient) command inside
  `_query_aquapura_split_green_sensors` — saves one cloud call per poll, costs the Outdoor Ambient
  Temperature sensor (`t4_temp`). Despite the name, it does **not** affect the `entity_category=DIAGNOSTIC`
  sensors on other profiles (condenser/evaporator/odu_voltage/…) — those are decoded from the
  status/sensors calls already made every poll, so there's no extra call to skip for them. The entity
  itself (Outdoor Ambient Temperature) always stays registered — it's a normal, always-relevant sensor
  shared by every profile, just blanked (`None`) while this is off — so it is **not** removed like the
  schedule entities below.
- `fetch_schedule=False` skips `client.query_daily_schedule` entirely (no network call) — saves one cloud
  call per poll. Unlike `fetch_diagnostics`, this **removes the 20 `Daily Schedule *` entities from the
  device entirely** (`sensor.py`/`binary_sensor.py`'s `async_setup_entry` filter them out via
  `coordinator.fetch_schedule` when building the entity list, rather than registering them to sit
  unavailable/unknown) — they're a coherent block 1:1 with one skippable call, unlike the diagnostics
  toggle's single shared value, so full removal is the right shape here. Re-enabling adds them back on
  the next reload (saving the options form always triggers one).

The options form's suggested (pre-filled) values are the entry's *current* saved options, via
`self.add_suggested_values_to_schema(schema, {...})` — **not** a `vol.Optional(key, default=...)` static
default, which would ignore what's already saved. `OptionsFlow.__init__` deliberately does **not** store
`self.config_entry = config_entry` (deprecated, removed in HA 2025.12+); `self.config_entry` is read from
the base class's property instead, inside `async_step_init` (see class docstring). Saving options triggers
a full entry reload (`entry.add_update_listener` in `__init__.py`) so the coordinator picks up the new
values — there's no live "hot" option update.

---

## 6. Diagnosing a "wrong/empty sensor" report (playbook)

1. Ask for a **diagnostics file** (Settings → Devices & Services → iLetComfort → ⋮ → Download
   diagnostics) **and** the **real value from the iLetComfort/BTRI app** at the same moment.
2. From diagnostics, read the `appliance` block (`sn8`!) and `sensors_temperature_scan`.
3. Cross-reference the app's real value against the scan / raw bytes to find the correct byte.
4. If it's model-specific, add/extend an `sn8` profile in `model_profiles.py` (default-safe), TDD against
   the real frames, keep STANDARD unchanged.
5. If setup fails before diagnostics exist (`1214` / truncated): get **debug logs**; the fix needs the
   command the **iLetComfort app** sends (mitmproxy capture — see §2b, no rooting assumptions).

---

## 7. Dev & release workflow

- **`main` is protected:** required status checks `pytest (Python 3.12)` + `Validate Conventional Commits
  title`, and `enforce_admins=true`. **No direct pushes** — they're rejected. Land changes via a PR
  (CI auto-runs, ~30s) and merge.
- **release-please** drives versioning from Conventional Commits on `main` (`feat:` → minor, `fix:`/
  `docs:` → patch/none). Its release PRs are **bot-authored, so CI does not fire** on them and
  `enforce_admins` blocks override → **close+reopen the release PR as a real user** to trigger checks,
  then merge. Repo **auto-merge is disabled**.
- **Conventional Commits** required (PR title is checked). Squash-merge keeps the conventional message on
  `main`.
- **TDD**: write failing tests first; run `pip install -r requirements_test.txt` then `python -m pytest -q`.
  Never break the STANDARD-decode regression tests.
- Tooling preference: `rg` / `fd` / `bat` over `grep` / `find` / `cat`.
- Distributed via **HACS**.

---

## 8. References

- Protocol RE / CLI: <https://github.com/tgenov/iletcomfort-cli> (captured via iLetComfort **iOS app** +
  mitmproxy).
- Out-of-scope (Midea MSmartHome) devices: <https://github.com/georgezhao2010/midea_ac_lan>,
  <https://github.com/midea-lan/midea-local>.
- `README.md` (scope, install, login-war note) and `docs/TROUBLESHOOTING.md`, `CONTRIBUTING.md`.
