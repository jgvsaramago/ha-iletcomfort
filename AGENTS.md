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
`entity_category=DIAGNOSTIC` — **not** `CONFIG`, even though they represent the device's own timer
configuration and "Configuration" reads like the intuitive HA section for them. `CONFIG` is invalid for a
read-only entity: `SensorEntity`/`BinarySensorEntity.async_internal_added_to_hass` explicitly raise
`HomeAssistantError("Entity ... cannot be added as the entity category is set to config")` if
`entity_category is EntityCategory.CONFIG`, since that category is reserved for entities the user can
*change* (switch/number/select) — a real bug shipped once (all 20 Daily Schedule entities failing to add,
every poll, on every reload), invisible for a while only because these entities used to be filtered out
entirely by a "fetch schedule" options-flow toggle that has since been removed (see the note on the options
flow below). `DIAGNOSTIC` is the only non-primary category available to a plain sensor/binary_sensor, so
that's what groups them away from the main entity list now — not literally labeled "Configuration", but the
closest valid equivalent.

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
carries every field, a caller changing only `enabled` (the **Disinfection Routine** switch — named to
leave room for a future one-off **Manual Disinfection** switch on the same device) must merge in the
current hour/minute/temperature/cycle_days — it does NOT default them, since sending zeros would actually
reset the device's real schedule. `api.py`'s `query_disinfection`/`set_disinfection` and
`coordinator.async_set_disinfection` implement this; the switch reads current values from
`coordinator.data["disinfection"]` and raises rather than guessing if that's not populated yet. The
hour/minute/temperature/cycle-days themselves are surfaced as separate `sensor.py` entities
(`disinfection_temperature`, `disinfection_time` — "HH:MM" like the daily-schedule start/end times,
`disinfection_cycle_days`), entity_category=DIAGNOSTIC like everything else here — not squeezed into the
switch's attributes. Fetched unconditionally every poll (same as the daily schedule, whose 02,58 frame it
shares) — costs one extra command per poll, on top of the tank/ODU sensors' own two commands, an accepted
cost for this profile.

**Heating element — confirmed, on the TIMERS (01,90) frame, not STATUS.** A real ON/OFF mitmproxy capture
pair pins `body[54]` of the **01,90 frame** (previously "captured but unused" — see module notes above) as
the toggle (`0x00`=Off, `0x01`=On). `body[62]` also moves between the two captures (`0x84`→`0x96`, +18) but
is a monotonically increasing counter, not part of the toggle — confirmed by checking it's the *only other*
byte that changes, and it changes by a small amount consistent with elapsed seconds between the two
captures, not a semantically meaningful state flip. The **write** reuses the single-field shape (see the
field table above) but on selector `01,90` instead of `01,f4`, with field id `0x0e` — the SAME field id
number as silence's, but field ids are scoped per-selector, so it's unrelated:

    aa <len> c3 00×6 02 00 01 90 ff ff ff ff 01 ff ff 00 0e 01 <value> <cks>

`build_aquapura_split_green_heating_element_command`/`decode_aquapura_split_green_heating_element` and
`api.py`'s `query_heating_element`/`set_heating_element` implement this; surfaced as the **Heating
Element** switch (`switch.py`), reading `coordinator.data["heating_element"]`, fetched unconditionally
every poll like disinfection.

**Force Disinfection — confirmed, same TIMERS (01,90) frame as the heating element, different bit.** The
app's manual/one-off counterpart to the scheduled Disinfection Routine. A real ON/OFF capture pair pins
`body[55]` (`0x00`=Off, `0x01`=On) — `body[62]` moves again as the same noisy counter, nothing else. Write
is field id `0x0f` on the same `01,90` selector (same shape as the heating element's, different field id
and body index — the two are otherwise independent, just packed in the same frame). Surfaced as the
**Force Disinfection** switch, reading `coordinator.data["force_disinfection"]`.

**Consumption (day/week/month/year/total energy) — confirmed, NEW selector `03,20`.** `body[12:32]` is 5
consecutive 32-bit BE hundredths-of-kWh counters in this order: day, week, month, year, total. Confirmed
by matching a live capture against the app's Consumption page: day `0x70`=112→1.12 kWh, the rest
`0x1ea`=490→4.90 kWh each — day being distinct from the other four (all equal, since the device was new)
is what pins the field order, not a guess from position alone. `body[32:36]` (`0x1e9`=489, one hundredth
below the day-adjacent totals) and the all-`0xff` block at `body[51:99]` (a 12-entry × 4-byte history
chart, all "no data" sentinel — matches the app's empty "History data" section) are real but unlabeled and
stay unmapped; so does `body[99:111]` (looks like 3 percentage-like values, possibly the SmartGrid
grid/máx/green split shown on the same page — `0x2710`=10000→100.00% for the first, matching "100% grid" —
but not confirmed against a capture that changes them). `decode_aquapura_split_green_consumption`/
`AquapuraSplitGreenConsumption` and `api.py`'s `query_consumption` implement this — **read-only, no write
captured or needed**. Surfaced as 4 new `sensor.py` entities (`day_energy`/`week_energy`/`month_energy`/
`year_energy`, `state_class=TOTAL` since they reset each period, unlike a lifetime counter) plus folded
into the existing **Total Energy** sensor (`_total_energy` value_fn: prefers `coordinator.data
["consumption"].total` when present, falls back to the generic STATUS-frame `total_kwh` every other
profile already used) rather than adding a redundant `consumption_total_energy` — Aquapura Split Green
`ITSStatus.total_kwh` was never decoded for this profile, so before this the primary-list "Total Energy"
sensor silently read `0` for these devices.

**ODU Serial Number — confirmed, from the ODU (`03,e8`) frame, but at a VARIABLE offset.** An exact
character-for-character match against the app's "More" page "ODU SN" field (`540NA870101B4140300373`) in
**two** real ODU captures taken at different times. The two captures disagree on where it sits — `body[157:179]`
in one, `body[154:176]` in the other — a 3-byte shift caused by some earlier count-like field (`0x02` vs
`0x03` a bit earlier in the frame) that has nothing to do with the serial itself. **`decode_aquapura_split_green_odu_serial`
therefore scans for the longest run of ASCII digits/uppercase letters ≥ `AQUAPURA_SPLIT_GREEN_ODU_SERIAL_MIN_LENGTH`
(15) instead of trusting a fixed byte offset** — a hardcoded index (the first instinct, and what got shipped
initially before the second capture exposed the shift) would have silently misread one of the two real
captures. A `V10`-like tag sits right after the serial in both captures but has no confirmed on-screen label,
so it's left unmapped. Surfaced on `ITSSensors.odu_serial` and the **ODU Serial Number** `sensor.py` entity
(DIAGNOSTIC, plain text, no device_class/unit).

**Deliberately NOT added, despite plausible single-capture matches** (screen values the "More" page shows
that a lone capture's bytes line up with, but with no before/after diff to rule out coincidence — see the
CONSUMPTION frame's SmartGrid-percentage note above for the same caveat): "IHM SN" (no ASCII run matching it
was found in any captured frame — it isn't in the `00,64`/`01,90`/`03,e8` frames we have; would need the
`00,00` "identity" selector's response, never captured), and the `00,64` "config/limits" frame's Aquec. lig.
(TrEH)/Água quente lig (Trdh) values (`10`°C/`5`°C) — several byte-pairs in that frame are plausible temperature
encodings (e.g. `0x02bc`=70.0°C, matching the already-used `AQUAPURA_SPLIT_GREEN_TEMP_MAX` guess) but no single
isolated byte unambiguously reads as `10` or `5` against a specific named field, so no entity was added for
either.

**Still not decoded / not writable:**
1. body[27]: real data (constant `0x32` across every capture so far, including ones where the real
   setpoint moved), no confirmed meaning.
2. The ODU frame's other sensors and the config frame's (`00,64`) limits are real data with no app label
   to validate against — left unmapped rather than guessed. The TIMERS (01,90) frame's own `body[13:48]`
   (two 20-byte all-'0'/zero-padded blocks) is likewise real but unlabeled — possibly blank
   timer/error-log strings — and stays unmapped. Same for the CONSUMPTION frame's `body[32:48]` and
   `body[99:118]` — see above.
3. Setpoint/power/mode **limits**: no app-confirmed min/max capture exists, so the climate entity reuses
   the KJRH-120L's validated DHW range (`AQUAPURA_SPLIT_GREEN_TEMP_MIN/MAX` = 20-70 °C) as a conservative
   bound — wide enough for the captured 50-52 °C values without guessing a tighter one.
4. **Daily schedule slot activate/deactivate — CONFIRMED for slots 1-2, EXTRAPOLATED for 3-4.** Single-field
   write on the schedule selector (`02,58`) — the same shape as power/silence/heating element, NOT
   Disinfection's multi-field shape:

       aa <len> c3 00×6 02 00 02 58 ff ff ff ff 01 ff ff 00 <fld> 01 <value> <cks>

       fld 0x2a: slot 1 active   fld 0x31: slot 2 active
       fld 0x38: slot 3 active (EXTRAPOLATED)   fld 0x3f: slot 4 active (EXTRAPOLATED)

   Slot 1: a real ON/OFF capture pair diffs to exactly `body[45]` (the same byte the *read* decode already
   uses) — clean. Slot 2: only ON was captured, and diffing also showed `body[57]`/`body[59]` (the slot's
   start/end hour) change — confirmed by the reporter to be a *separate* time edit made in the same app
   session, not a side effect of the write, so only field `0x31` → `body[53]` is trusted from it. Slots 3-4
   have **no captures at all**; `0x38`/`0x3f` come from extrapolating the `+7` field-id spacing between slots
   1 and 2, which lines up structurally with the 8-byte read-side slot layout (1 active byte + 7 other field
   ids covering mode/setpoint/start_h/start_m/end_h/end_m — so slot 1 occupies `0x2a`-`0x30` and slot 2
   starting at `0x31` is exactly "the next slot"). This is a deliberate, structurally-reasoned extrapolation
   made at the maintainer's explicit request (not the project's default "don't guess" posture) — **if a real
   capture for slot 3 or 4 ever contradicts `_AQUAPURA_SPLIT_GREEN_SCHEDULE_ACTIVE_FIELD_IDS`, trust the
   capture and fix the table**, don't rationalize the mismatch away.

   `build_aquapura_split_green_schedule_active_command`/`api.py`'s `set_schedule_active` implement this;
   `coordinator.async_set_schedule_active` also resets `_last_config_fetch` to force an immediate schedule
   refetch on the next poll (otherwise a slot the user just toggled could keep reading stale for up to
   `CONFIG_FETCH_INTERVAL`). Surfaced as **switch.py**'s `ILetComfortDailyScheduleActiveSwitch` (one per
   slot 1-4), which replaced the old read-only `binary_sensor.py` "Daily Schedule N Active" entities — now
   that it's a confirmed write, `entity_category=CONFIG` is the correct category (unlike the other
   daily-schedule fields, which stay DIAGNOSTIC sensors since they're still read-only — see §6). Editing a
   slot's setpoint/start/end/mode (as opposed to just activate/deactivate) is still NOT captured — don't
   guess those field ids without a capture.

   **Migration note:** removing the old binary_sensor entities without cleanup would leave them
   "unavailable" forever in any existing install (HA doesn't retroactively delete entities a platform stops
   creating) — exactly the class of bug the old options-flow fetch toggles caused (see the note on the
   removed options flow, §5 code map). `__init__.py`'s `_remove_stale_daily_schedule_binary_sensors` handles
   this proactively (removes the 4 known-stale unique_ids at setup, best-effort, idempotent) instead of
   waiting for a user report like last time.

**Unsupported entities hidden, not shown reading a fake value.** `decode_aquapura_split_green_status`/
`_sensors` only ever set a handful of fields (see above); every other `ITSStatus`/`ITSSensors` field this
profile's entities would otherwise read stays at its Python class default (`0`/`False`, not `None`) —
worse than the four temps `_AQUAPURA_SPLIT_GREEN_SUPPRESSED_TEMPS` nulls to "unavailable", these would show
a live-looking-but-permanently-fake reading (a 0 Hz compressor frequency forever, an "Off" that never
turns on). None of them correspond to anything the app shows for this model — checked against every
captured screen. `AQUAPURA_SPLIT_GREEN_UNSUPPORTED_SENSOR_KEYS` (water_inlet, water_outlet, condenser,
evaporator, refrigerant, plate_hx, compressor_freq, comp_run_hours, pressure_high, pressure_low,
odu_voltage, odu_current) and `_UNSUPPORTED_BINARY_SENSOR_KEYS` (compressor_running, ibh_running) — both in
`model_profiles.py` — are filtered out of `sensor.py`/`binary_sensor.py`'s `async_setup_entry` for this
profile. `error_code`/`error` are the one exception kept despite the identical "no confirmed signal, forced
to a default" justification in the decode's own docstring — not reported as a problem, and losing fault
visibility is a bigger risk than losing a runtime counter, so it stays unless a report says otherwise.

**Boost is unsupported for Aquapura Split Green *and* KJRH-120L — a real broken-write bug, same root cause
as Silent Mode.** `sensors.ctrl_flag` (what `is_on` reads) is never set by either profile's sensors decode,
*and* neither `_set_device_kjrh120l` nor `_set_device_aquapura_split_green` accepts a `boost` kwarg at all —
confirmed by reading the method signatures, not a live capture — so pressing the switch silently does
nothing on either model, exactly like Silent Mode's mute/ctrl_flag before it was hidden (§4 above).
`BOOST_UNSUPPORTED_PROFILES` in `model_profiles.py` gates it out of `switch.py`'s `async_setup_entry`,
mirroring `select.py`'s `_MUTE_UNSUPPORTED_PROFILES` (kept local/unshared there; Boost's version is shared
since `__init__.py`'s migration cleanup also needs it — see below).

**Migration:** removing entities a platform used to create without cleanup leaves them "unavailable"
forever in any existing install — the same class of bug the removed options-flow toggles caused, and that
`_remove_stale_daily_schedule_binary_sensors` already fixed once for a different entity set. `__init__.py`'s
`_remove_unsupported_entities` handles this one the same way: best-effort, idempotent, runs after
`coordinator.sn8` is available (needs it to know which profile's list applies).

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
| `coordinator.py` | `ILetComfortCoordinator`: polling, re-auth, cache-fallback, offline Repair card. Caches `appliance_meta` (best-effort, never fatal) and exposes `sn8`; threads profile into decode. Fixed `DEFAULT_SCAN_INTERVAL` (60s) poll cadence for status/sensors/disinfection/heating element/force disinfection/consumption — every fetch always runs, no user-facing config to disable any of them. The one exception: the daily schedule is refetched only once per `CONFIG_FETCH_INTERVAL` (5 min), tracked via `self._last_config_fetch`, since a user's timer config changes far less often than everything else polled — see `_poll()`. |
| `diagnostics.py` | Redacted snapshot: raw frames, decoded status/sensors, `sensors_temperature_scan` (per-byte `_temp_offset` map — use it to find a model's misplaced temp byte), and the `appliance` metadata block. `APPLIANCE_TO_REDACT = {owner, sn, name}` (keeps `applianceType`/`modelNumber`/`sn8`). |
| `climate.py` / `sensor.py` / `binary_sensor.py` / `switch.py` / `select.py` | HA entities. See §4 for which field backs which entity. |
| `config_flow.py` / `__init__.py` / `const.py` / `entity.py` | Setup, entry, constants, base entity. No options flow — the integration has zero user-configurable settings by design (see the note below). |
| `tests/` | pytest suite; real captured frames are pinned as fixtures (e.g. issue-#11 frames for STANDARD regression). |

**No options flow, deliberately.** An earlier version added a Configure options flow (poll interval,
per-poll fetch toggles for diagnostics/daily-schedule). It was removed: the fetch toggles left orphaned
"unavailable" Daily Schedule entities behind in the entity registry when disabled (HA doesn't retroactively
delete previously-registered entities just because a later reload stops creating them — the platform
simply stops touching them, and they sit greyed out until manually deleted from Settings → Devices &
Services → Entities). Every per-poll fetch (diagnostics, daily schedule, disinfection) now always runs
unconditionally, and every entity is always registered — the same "declared unconditionally, reads inert
for non-applicable profiles" shape used everywhere else in this codebase (Boost/Silence switches, climate
presets). If a orphaned "unavailable" entity from the old toggle still lingers for a user upgrading from
that version, it must be deleted manually via the UI — the integration re-registering it on this version's
first reload does not resurrect an entity id HA has already marked orphaned, so point users at manual
deletion first when this is reported.

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
