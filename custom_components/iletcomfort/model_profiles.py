"""sn8-gated model decode profiles.

Different C3 heat-pump *models* pack their status/sensor frames differently, but
the Dollin cloud exposes no device-class field: both an air-to-water (ATW) unit
and an air-to-air unit report ``applianceType="0xC3"`` and ``modelNumber="0"``.
The only differentiator is ``sn8`` — an 8-char model-code serial prefix carried
in the appliance metadata (cached on the coordinator as
``appliance_meta["sn8"]``).

Model-specific decoding is therefore gated on an ``sn8 -> profile`` lookup table.
Any UNKNOWN or missing sn8 falls back to the STANDARD decode unchanged, so
existing/working models cannot be corrupted by a new profile.

Profiles
--------
STANDARD
    Default. Byte-for-byte identical to ``decode_its_status`` /
    ``decode_its_sensors``. ``apply_profile_to_*`` return the input untouched.

ATW (sn8 ``171H120F``, Italtherm air-to-water, issue #22)
    A 25-byte status frame with a different field layout. Decoded by
    ``decode_atw_status``; the values are surfaced through the *existing*
    ITSStatus/ITSSensors fields so no entity wiring changes are needed (see the
    field map below).

AQUAPURA (sn8 ``171000AU``, AQS Energie AQUAPURA split HPWH, issue #12)
    Standard status/sensor decode, except the tank temperature must read
    ``status.box_bottom_temp`` (status byte[17], offset-decoded) and is surfaced
    on ``th_temp`` (the "DHW Tank Temperature" sensor) instead of
    ``sensors.twin_temp`` (which is null-filled to 0 on this model).

AQUAPURA_SPLIT_GREEN (sn8 ``17186T3A``, Energie Aquapura Split Green HPWH, KJR-86T3 wired
    controller)
    Answers neither the standard long C3 query (it is echoed back verbatim) nor
    the KJRH-120L short form. Its app sends fully-formed 21-byte *selector*
    query frames and reads each data group from a separate frame, so this
    profile overrides the query commands as well as the decode. Read-only for
    now (setpoint + tank temperature); see the section below.

For ATW, AQUAPURA and AQUAPURA_SPLIT_GREEN the tank temperature is routed to
``th_temp`` and the "Water Inlet Temperature" sensor (``twin_temp``) is left
honest (no real inlet reading). Climate ``current_temperature`` is profile-aware
and returns ``th_temp`` for these profiles (``twin_temp`` for STANDARD).
"""

from __future__ import annotations

import dataclasses
from enum import Enum
from typing import Any

from .api import ITSSensors, ITSStatus

# sn8 model codes (8-char serial prefixes) → profile.
ATW_SN8 = "171H120F"
AQUAPURA_SN8 = "171000AU"
KJRH120L_SN8 = "17100003"
AQUAPURA_SPLIT_GREEN_SN8 = "17186T3A"


class ModelProfile(Enum):
    """Decode profile selected from a device's sn8 model code."""

    STANDARD = "standard"
    ATW = "atw"
    AQUAPURA = "aquapura"
    KJRH120L = "kjrh120l"
    AQUAPURA_SPLIT_GREEN = "aquapura_split_green"


_SN8_PROFILES: dict[str, ModelProfile] = {
    ATW_SN8: ModelProfile.ATW,
    AQUAPURA_SN8: ModelProfile.AQUAPURA,
    KJRH120L_SN8: ModelProfile.KJRH120L,
    AQUAPURA_SPLIT_GREEN_SN8: ModelProfile.AQUAPURA_SPLIT_GREEN,
}


def resolve_profile(sn8: str | None) -> ModelProfile:
    """Map an sn8 model code to a decode profile.

    Unknown or missing sn8 → STANDARD (the safe, unchanged default).
    """
    if not sn8:
        return ModelProfile.STANDARD
    return _SN8_PROFILES.get(sn8, ModelProfile.STANDARD)


def build_query_command(profile: ModelProfile, subtype: int) -> str:
    """Return the cloud query command for a status/sensors subtype.

    KJRH-120L's cloud rejects the standard long C3 query frame (1214); its
    app uses a short ``ffff<ss><ss><ss>ff`` form. The Aquapura Split Green
    echoes both of those back verbatim and needs its app's 21-byte selector
    frames instead (see ``build_aquapura_split_green_query``): subtype 0x01
    (status) → selector ``01,f4``, subtype 0x02 (sensors) → the
    tank-temperature frame ``03,84``.
    STANDARD/other profiles use the normal build_c3_query frame, unchanged.
    """
    if profile is ModelProfile.KJRH120L:
        return "ffff%02x%02x%02xff" % (subtype, subtype, subtype)
    if profile is ModelProfile.AQUAPURA_SPLIT_GREEN:
        selector = _AQUAPURA_SPLIT_GREEN_SUBTYPE_SELECTORS.get(subtype)
        if selector is not None:
            return build_aquapura_split_green_query(*selector)
        # No other subtype is queried today; fall back to the standard frame
        # rather than inventing a selector for this model.
    # Lazy import: api.py imports model_profiles, so importing build_c3_query at
    # module level would risk a circular import.
    from .api import build_c3_query

    return build_c3_query(subtype)


# ---------------------------------------------------------------------------
# ATW profile (issue #22) — validated against five real frames from yoavaviram
# ---------------------------------------------------------------------------
#
# status raw_body, 0-indexed ([0] = 0x01 subtype byte):
#   byte[1]  flags: bit0 (0x01) = space-heat demand; the 0x04 bit tracks DHW
#            activity. Treated as FLAGS, not a scalar/mode.
#   byte[8]  DHW setpoint in °C — DIRECT value (not +35-offset encoded).
#   byte[9]  Zone-1 setpoint × 2 (0.5° resolution) → zone1 = byte[9] / 2.
#   byte[22] DHW tank current temp in °C — DIRECT value.
#   byte[24] constant 0x80 across all captured states → a flags/MSB byte,
#            NOT an error code. The app shows no fault, so error_code = 0.
#
# The STANDARD decoder misreads this 25-byte frame (set_temperature=3,
# error_code=128, spurious comp_running, DHW tank=0, mode=Unknown(7), …);
# decode_atw_status fixes those by surfacing the confirmed values through the
# existing ITSStatus fields the entities already read:
#   - set_temperature  ← byte[8]   (no DHW-setpoint entity today; kept for SET
#                                    echo / future use)
#   - t5s_def          ← byte[9]/2 (climate target_temperature reads t5s_def)
#   - box_bottom_temp  ← byte[22]  (routed to th_temp / "DHW Tank Temperature";
#                                    see apply_profile_to_sensors. Climate
#                                    current_temperature is profile-aware and
#                                    reads th_temp for ATW.)
#
# CONSERVATIVE / ASSUMED (await hardware validation by the reporter):
#   - comp_running is forced False: none of the five frames carries a confirmed
#     compressor-running signal, and there is no comp_frq field in a 25-byte
#     frame, so we do not derive "running" from byte[14] (which the STANDARD
#     path misread as a comp flag).
#   - HVAC mode/action semantics are left at the dataclass defaults (mode 0 /
#     "Off"); the byte[1] flags are recorded raw in status_flags_raw with only
#     the space-heat-demand bit (0x01) interpreted. We do not invent an HVAC
#     mode/action mapping until validated.

ATW_DHW_SETPOINT_INDEX = 8
ATW_ZONE1_SETPOINT_X2_INDEX = 9
ATW_DHW_TANK_TEMP_INDEX = 22
ATW_FLAGS_INDEX = 1
ATW_SPACE_HEAT_DEMAND_BIT = 0x01


def decode_atw_status(body: bytearray | bytes) -> ITSStatus:
    """Decode an ATW (sn8 171H120F) status frame into an ITSStatus.

    Routes the confirmed ATW byte values into the existing ITSStatus fields the
    entities consume. See the module-level field map for the full layout and the
    conservative choices made for unvalidated fields.
    """
    status = ITSStatus()
    status.raw_body = bytes(body)
    body_len = len(body)

    # Defensive: a frame too short to carry the confirmed fields decodes to an
    # all-defaults ITSStatus (the caller's truncated-frame guard handles the
    # query path; this keeps direct decode calls safe).
    if body_len <= ATW_DHW_TANK_TEMP_INDEX:
        return status

    flags = body[ATW_FLAGS_INDEX]
    status.status_flags_raw = flags
    # Only the space-heat-demand bit is interpreted; see module notes.
    status.pump_system = bool(flags & ATW_SPACE_HEAT_DEMAND_BIT)

    # DHW setpoint — direct °C value.
    status.set_temperature = body[ATW_DHW_SETPOINT_INDEX]
    # Zone-1 / climate target setpoint — 0.5° resolution. Surfaced via t5s_def
    # so the climate entity's target_temperature reads it without changes.
    status.t5s_def = body[ATW_ZONE1_SETPOINT_X2_INDEX] / 2
    # DHW tank current temp — direct °C value. Surfaced via box_bottom_temp,
    # which apply_profile_to_sensors routes to th_temp ("DHW Tank Temperature").
    status.box_bottom_temp = float(body[ATW_DHW_TANK_TEMP_INDEX])

    # byte[24] is a flags/MSB byte, not a fault → no error. Conservative: no
    # confirmed running signal in these frames.
    status.error_code = 0
    status.comp_running = False

    return status


# ---------------------------------------------------------------------------
# KJRH-120L profile (issues #21 / #5) — clean decode from real diagnostics
# ---------------------------------------------------------------------------
#
# The KJRH-120L (sn8 17100003) connects via the short ffff query form
# (build_query_command above), but its 95-byte status frame does NOT match the
# STANDARD C3 layout. The STANDARD decoder misreads it and emits garbage — from
# the reporter's real frame (device Off, app DHW setpoint 60 °C):
#   set_temperature=66, t5s_def=-35, error_code=1 (fake fault),
#   comp_running=True (it's Off), total_kwh=1340, comp_frq=6425, every temp -35.
#
# Confirmed status fields across multiple captured states:
#   body[10] = power state (0x00 = Off, non-zero = On). Found by diffing a real
#              OFF frame vs a real ON frame (issue #35): body[10] is the only
#              state byte that flips with power. body[2] stays 0x00 in BOTH the
#              off and on captures, so it is NOT the power/mode byte.
#   body[15] = DHW setpoint in °C — DIRECT value (0x3c=60, 0x41=65).
# This is THE setpoint. error_code is 0 (no fault). comp_running is left False:
# power-on is not a compressor-running signal and no compressor byte is
# confirmed. Everything else in the frame is unmapped garbage for this model and
# is left at the dataclass defaults (None/0).
#
# Water tank temp and outdoor temp are NOT decodable for this model — they do
# not appear in the status frame and the sensors frame is static/cached (three
# different water readings and two outdoor readings never appear in any byte).
# We deliberately do not guess a mapping; apply_profile_to_sensors nulls every
# temperature field so HA shows them unavailable instead of a wrong constant.

# body[10] carries the power state (0 = Off, non-zero = On). When On we report
# mode 1 ("Heat"): climate.py's _QUERY_MODE_TO_HVAC maps query-mode 1 →
# HVACMode.HEAT, a non-off state, so the card shows the unit ON and the Off
# button becomes usable (HA no longer thinks it is already off).
KJRH120L_POWER_INDEX = 10
KJRH120L_MODE_OFF = 0
KJRH120L_MODE_ON = 1
KJRH120L_DHW_SETPOINT_INDEX = 15
_KJRH120L_MODES = {0: "Off", 1: "Heat", 2: "Cool", 3: "Auto", 4: "Water Pump"}

# Temperature fields suppressed for the KJRH-120L (not exposed by its API).
_KJRH120L_SUPPRESSED_TEMPS = {
    "t3_temp": None,
    "t4_temp": None,
    "t2_temp": None,
    "t2b_temp": None,
    "twin_temp": None,
    "twout_temp": None,
    "t1_temp": None,
}


def decode_kjrh120l_status(body: bytearray | bytes) -> ITSStatus:
    """Decode a KJRH-120L (sn8 17100003) status frame into a clean ITSStatus.

    Surfaces only the confirmed fields (power from body[10], DHW setpoint from
    body[15]) and suppresses the garbage the STANDARD decoder produces. See the
    module-level notes for the full rationale.
    """
    status = ITSStatus()
    status.raw_body = bytes(body)
    body_len = len(body)

    # No fault is confirmed for every captured state. comp_running stays False:
    # power-on is not a compressor-running signal (no compressor byte confirmed).
    status.error_code = 0
    status.comp_running = False

    # Power state from body[10]: 0 → Off, non-zero → On. When On we report mode 1
    # ("Heat") so the climate entity resolves a non-off hvac_mode (HVACMode.HEAT)
    # — making the card read ON and the Off button usable. Defaults to Off when
    # the frame is too short to carry the power byte.
    if body_len > KJRH120L_POWER_INDEX:
        if body[KJRH120L_POWER_INDEX] == 0:
            status.mode = KJRH120L_MODE_OFF
        else:
            status.mode = KJRH120L_MODE_ON
        status.mode_name = _KJRH120L_MODES.get(
            status.mode, f"Unknown({status.mode})"
        )

    # DHW setpoint — direct °C value. Surfaced via t5s_def so the climate
    # entity's target_temperature (t5s_def if not None) shows it; set_temperature
    # is set too for the SET echo / future DHW-setpoint entity.
    if body_len > KJRH120L_DHW_SETPOINT_INDEX:
        setpoint = body[KJRH120L_DHW_SETPOINT_INDEX]
        status.t5s_def = float(setpoint)
        status.set_temperature = setpoint

    return status


# ---------------------------------------------------------------------------
# KJRH-120L WRITE / control commands (issue #35)
# ---------------------------------------------------------------------------
#
# The KJRH-120L's cloud rejects the standard 62-byte C3 SET frame (build_c3_set)
# for the same reason it rejected the standard long query: this controller only
# accepts the short literal command strings the official app sends. These were
# captured from the app's proxy traffic (code:0 responses, issues #35 / #21).
#
# General write shape: ``00 <field> 01 <value> ff`` — no checksum byte (the
# cloud frames the command). Confirmed fields:
#   field 0x07 = DHW setpoint  → ``0007 01 <T °C as 1-byte hex> ff``
#                                (T=49 → 00070131ff, T=60 → 0007013cff)
#   field 0x02 = DHW power     → ON ``00020101ff`` / OFF ``00020100ff``
#
# DHW setpoint range for this HPWH (captured 49/60/65 °C) sits above the air-side
# HEAT range, so the climate entity uses a profile-specific min/max.
KJRH120L_DHW_ON = "00020101ff"
KJRH120L_DHW_OFF = "00020100ff"
# Setpoint range the official app allows for this HPWH (issue #35). Captured
# setpoints (49/60/65 °C) sit above the air-side HEAT range, so the climate
# entity uses this profile-specific min/max.
KJRH120L_TEMP_MIN = 20
KJRH120L_TEMP_MAX = 70


def build_kjrh120l_set_temperature(temp: int) -> str:
    """Return the KJRH-120L short DHW-setpoint write command for ``temp`` °C.

    Shape ``0007 01 <temp as 2-hex-digit byte> ff`` — e.g. 60 → ``0007013cff``,
    49 → ``00070131ff`` (confirmed captures). No checksum; the cloud frames it.
    """
    return "000701%02xff" % temp


# ---------------------------------------------------------------------------
# Aquapura Split Green profile (sn8 17186T3A) — Energie Aquapura Split Green HPWH
# ---------------------------------------------------------------------------
#
# Captured from the official iLetComfort iOS app (v1.7.0) against eu.dollin.net,
# cross-checked with live HA debug logs. Wired controller KJR-86T3,
# applianceType 0xC3, modelNumber 0 (so, as always, sn8 is the only gate).
#
# WHY THIS NEEDS QUERY OVERRIDES, NOT JUST A DECODER
# The standard long C3 query (``aa,0b,c3,…,01,2e``) is *echoed back verbatim* by
# the cloud (code 0, data == the command), and so is the KJRH-120L short form.
# The app instead sends fully-formed 21-byte frames carrying a (selector, param)
# pair, and reads each data group from its own frame:
#
#   aa 14 c3 00 00 00 00 00 00 03 00 <sel> <param> ff ff ff ff 00 ff ff <cks>
#   cks = (~sum(frame[1:-1]) + 1) & 0xFF   (build_aquapura_split_green_query reproduces all
#                                           eight captured commands byte-exact)
#
#   (0x00,0x00) identity: IHM serial + firmware      (0x00,0x64) config/limits
#   (0x01,0xf4) STATUS: setpoint + mode/power flags  (0x01,0x90) timers/errors
#   (0x02,0x58)/(0x02,0x62) weekly schedule          (0x03,0xe8) ODU sensors
#   (0x03,0x84) TANK TEMP: current water temperature
#
# Note the tank temperature lives under selector 0x03, NOT the standard sensors
# subtype 0x02 — hence the subtype→selector map used by build_query_command.
#
# FRAME LAYOUT (body = raw[10:-1], as extract_c3_body returns)
# body[3..11] (raw idx13..21, ``ff ff 83 ff 00 ff ff ff 15``) are identical in
# every frame: protocol boilerplate, not data. In particular raw idx21 = 0x15 is
# a constant — it is NOT "ambient 21 °C" (an early coincidence, ruled out by
# comparing frames).
# Encodings: the DHW setpoint is a 1-byte direct °C value; live temperatures are
# 16-bit big-endian tenths (value / 10), with 0x7fff meaning "sensor absent".
#
# CONFIRMED against the app across TWO captures — capture A (tank 49 °C,
# ambient 21 °C) and capture B (tank 48 °C, ambient 20 °C), both with the unit
# OFF and the setpoint at 50 °C:
#   TANK frame  (0x03,0x84):  body[30:32] = tank temp ×10 BE
#                             (A 0x01ec → 49.2, B 0x01e7 → 48.7). The value
#                             moved with the real tank, so this frame is LIVE,
#                             not cached.
#   ODU frame   (0x03,0xe8):  body[49:51] = outdoor ambient ×10 BE
#                             (A 0x00d4 → 21.2, B 0x00d0 → 20.8).
#
# The app TRUNCATES rather than rounds: 49.2→"49", 48.7→"48", 20.8→"20". That
# rule is what pins the ambient: under it, body[49:51] is the only 16-bit pair in
# the whole 225-byte ODU frame consistent with both captures (the neighbouring
# sensors read 32.6/25.3/21.8 °C in capture B, none of which the app could show
# as 20). We report the true tenths (48.7), not the app's truncation.
#
# Because the ambient lives in a THIRD frame, one sensors poll needs two
# commands for this model — see ILetComfortClient._query_aquapura_split_green_
# sensors, where the ODU fetch is best-effort so a failure costs the ambient but
# never the tank temperature.
#
# STATUS frame (0x01,f4) — WRONG TURN, then corrected. The very first capture
# (unit OFF, app setpoint 50 °C) had body[27] == 0x32 (=50, direct °C), and
# that single coincidental match was reported as "the DHW setpoint". A later
# capture series that actually CHANGED the setpoint (50→51→52→50 via the app,
# mitmproxy request+response each time) proved that was wrong: body[27] never
# moved — it stayed 0x32 in every single capture regardless of the real
# setpoint. The byte that tracks the live setpoint exactly is body[15:17], a
# 16-bit BE TENTHS value (0x01fe→51.0, 0x0208→52.0, 0x01f4→50.0 — all three
# match the requested value to the decimal). body[27] is left undecoded; its
# true meaning (a limit? a stale/cached field?) is unconfirmed.
# Lesson (see AGENTS.md §2c/meta-lesson): one coincidental byte match from a
# single capture is not confirmation — it takes a capture that actually
# CHANGES the value to tell a real field from a constant that happens to match.
#
# POWER — confirmed via a captured ON/OFF mitmproxy pair (SET request +
# STATUS response, each state): body[13] (raw idx23) = 0x00 Off / 0x01 On,
# the ONLY body byte that differs between the two responses. mode is reported
# 0/"Off" or 1/"Heat" (KJRH-120L's convention: query mode 1 maps to
# HVACMode.HEAT in climate.py, a non-off state, so the card reads ON and the
# Off button becomes usable).
#
# OPERATING MODE ("Eco"/"Disparo") — confirmed via a captured mode-toggle
# mitmproxy pair: body[14] (raw idx24) = 0x40 "Eco" / 0x80 "Disparo", the ONLY
# body byte that differs between the two responses. This is the SAME marker
# byte/encoding as the daily-schedule slots (see below) — the live status and
# a schedule slot share one mode vocabulary. Distinct from ``mode``/power:
# surfaced as ``ITSStatus.operating_mode``, a field only this profile
# populates.
#
# WRITE — all three captured toggles (power, operating mode, setpoint) share
# ONE frame shape, differing only in field id / value length / value:
#
#   aa <len> c3 00×6 02 00 01 f4 ff ff ff ff 01 ff ff 00 <fld> <vlen> <value...> <cks>
#
#   fld 0x0b, vlen 1: power        (0x01 On / 0x00 Off)
#   fld 0x0c, vlen 1: op. mode     (0x40 Eco / 0x80 Disparo — schedule's marker)
#   fld 0x0d, vlen 2: DHW setpoint (16-bit BE tenths °C, e.g. 0x01fe = 51.0)
#
# Confirmed byte-exact against 7 real captures (2 power, 2 mode, 3 setpoint:
# 50→51, 51→52, 52→50) via _build_aquapura_split_green_write. Same checksum
# formula as the query frames. header[9] = 0x02 here (a SET/control request)
# vs 0x03 for a plain query — this also explains an earlier oddity: a
# schedule-frame capture taken right after an app write also had raw[9]=0x02,
# most likely echoing "this exchange involved a write", not a data field.
#
# DELIBERATELY NOT DECODED / NOT WRITABLE:
#   - body[27]: real data (constant 0x32 across every capture so far), no
#     confirmed meaning — see the STATUS "wrong turn" note above.
#   - The ODU frame's other sensors (body[43:45], body[19:21] ≈ the tank, etc.)
#     and the config frame's (0x00,0x64) limit values: real data, but no app
#     label to validate them against, so they stay unmapped.
#   - The 02,62 frame: a different, 9-byte-block schedule layout not shown on
#     any captured screen, so it is left unmapped rather than guessed.
#   - Setpoint/power/mode limits: no app-confirmed min/max capture exists for
#     this model, so the climate entity reuses the KJRH-120L's validated DHW
#     range (20-70 °C) as a conservative bound — wide enough to cover every
#     captured value (50-52 °C) without guessing a tighter one.
#
# DAILY SCHEDULE ("Tempor. diário", selector 0x02,0x58) — fully decoded and
# validated against a live screenshot showing all 4 slots (1 enabled, 3
# disabled, all "ECO"). Each slot is an 8-byte block starting at body[45 + n*8]
# for slot n (0-indexed):
#   [0] active (0x00/0x01)   [1] mode marker (0x40 → "Eco", 0x80 → "Disparo")
#   [2:4] setpoint, 16-bit BE direct °C ×10 (0x01f4 → 50.0)
#   [4:6] start HH,MM (direct, e.g. 0x09,0x00 → "09:00")
#   [6:8] end   HH,MM (direct, e.g. 0x15,0x00 → "21:00", 0x15 = 21)
# All four slots' active flag, setpoint, start/end time matched the app
# byte-for-byte, including the three DISABLED slots — their config is present
# in the frame regardless of the active flag, matching what the app shows.
# The mode marker was confirmed by a second capture, taken after switching
# Temporiz. 1's mode in the app from "ECO" to "Disparo": the ONLY byte that
# changed in the whole 88-byte frame was slot 1's marker, 0x40 -> 0x80 — proof
# this byte (and only this byte) encodes the mode. That capture's raw[9] was
# also 0x02 instead of the usual 0x03. The power ON/OFF captures below explain
# this: 0x02 is the header byte the app also uses for control/SET requests, so
# raw[9]=0x02 in a response most likely echoes "the app was writing something
# around this poll", not a data field — still not consumed by extract_c3_body
# or this decoder.

# Query selectors (idx11/idx12 of the frame) captured from the app.
AQUAPURA_SPLIT_GREEN_STATUS_SELECTOR = (0x01, 0xF4)
AQUAPURA_SPLIT_GREEN_TANK_SELECTOR = (0x03, 0x84)
AQUAPURA_SPLIT_GREEN_ODU_SELECTOR = (0x03, 0xE8)
AQUAPURA_SPLIT_GREEN_SCHEDULE_SELECTOR = (0x02, 0x58)
# "Timers/errors" frame (previously captured but unmapped — see module
# notes). Now known to also carry the heating element enable bit.
AQUAPURA_SPLIT_GREEN_TIMERS_SELECTOR = (0x01, 0x90)
# "Consumption" page frame — day/week/month/year/total energy.
AQUAPURA_SPLIT_GREEN_CONSUMPTION_SELECTOR = (0x03, 0x20)

# Which selector serves each of the integration's two polled subtypes.
_AQUAPURA_SPLIT_GREEN_SUBTYPE_SELECTORS: dict[int, tuple[int, int]] = {
    0x01: AQUAPURA_SPLIT_GREEN_STATUS_SELECTOR,
    0x02: AQUAPURA_SPLIT_GREEN_TANK_SELECTOR,
}

# STATUS body[15:17] = the live DHW setpoint, 16-bit BE tenths of °C
# (confirmed by a real 50->51->52->50 capture series — see module notes).
# body[27] (the original, WRONG guess) is left unmapped.
AQUAPURA_SPLIT_GREEN_SETPOINT_INDEX = 15
AQUAPURA_SPLIT_GREEN_TANK_TEMP_INDEX = 30
AQUAPURA_SPLIT_GREEN_AMBIENT_TEMP_INDEX = 49
# STATUS body[13] = power: 0x00 Off, non-zero On (confirmed by diffing a real
# ON/OFF capture pair — see module notes). Reported mode 1/"Heat" matches the
# KJRH-120L convention: query mode 1 -> HVACMode.HEAT in climate.py.
AQUAPURA_SPLIT_GREEN_POWER_INDEX = 13
AQUAPURA_SPLIT_GREEN_MODE_OFF = 0
AQUAPURA_SPLIT_GREEN_MODE_ON = 1
# STATUS body[14] = the "Eco"/"Disparo" operating preset — same marker/
# encoding as a daily-schedule slot's mode byte (confirmed by a real mode-
# toggle capture pair — see module notes).
AQUAPURA_SPLIT_GREEN_OPERATING_MODE_INDEX = 14
# STATUS body[17] = the "Silence" quiet-mode toggle: 0x00 Off, 0x01 On
# (confirmed by diffing a real Silence ON/OFF capture pair). Independent of
# ``operating_mode`` (Eco/Disparo) — same field table shape as power.
AQUAPURA_SPLIT_GREEN_SILENCE_INDEX = 17

# TIMERS (01,90) frame body[54] = the heating element enable bit: 0x00 Off,
# 0x01 On (confirmed by diffing a real ON/OFF capture pair — the ONLY other
# byte that moved, body[62], is a monotonically increasing counter unrelated
# to the toggle — see module notes). NOT a STATUS-frame index.
AQUAPURA_SPLIT_GREEN_HEATING_ELEMENT_INDEX = 54
# TIMERS (01,90) frame body[55] = the "Force Disinfection" (manual, one-off)
# toggle: 0x00 Off, 0x01 On (confirmed the same way — body[62] again moved as
# the same noisy counter, nothing else).
AQUAPURA_SPLIT_GREEN_FORCE_DISINFECTION_INDEX = 55

# CONSUMPTION (03,20) frame body[12:32] = 5 consecutive 32-bit BE
# hundredths-of-kWh counters: day, week, month, year, total (confirmed by
# matching a live capture against the app's Consumption page: day=0x70=112
# ->1.12 kWh, week/month/year/total all 0x1ea=490 ->4.90 kWh, matching the
# screen exactly, day being distinct from the rest is what pins the field
# order). The 4 bytes right after (body[32:36], 0x1e9=489) and the all-0xff
# block from body[51:99] (yearly/monthly history chart, empty) are real data
# with no confirmed per-field meaning and are left unmapped.
AQUAPURA_SPLIT_GREEN_CONSUMPTION_DAY_INDEX = 12
AQUAPURA_SPLIT_GREEN_CONSUMPTION_WEEK_INDEX = 16
AQUAPURA_SPLIT_GREEN_CONSUMPTION_MONTH_INDEX = 20
AQUAPURA_SPLIT_GREEN_CONSUMPTION_YEAR_INDEX = 24
AQUAPURA_SPLIT_GREEN_CONSUMPTION_TOTAL_INDEX = 28

# Setpoint range the climate entity clamps to for this profile. No app-
# confirmed limit capture exists yet; this reuses the KJRH-120L's validated
# DHW range (issue #35) as a conservative bound wide enough to cover every
# captured value (50-52 °C) without guessing a tighter one.
AQUAPURA_SPLIT_GREEN_TEMP_MIN = 20
AQUAPURA_SPLIT_GREEN_TEMP_MAX = 70

# Write-command field ids (idx21 of the SET frame) and their value length
# (idx22). See build_aquapura_split_green_* and _build_aquapura_split_green_write.
AQUAPURA_SPLIT_GREEN_FIELD_POWER = 0x0B
AQUAPURA_SPLIT_GREEN_FIELD_OPERATING_MODE = 0x0C
AQUAPURA_SPLIT_GREEN_FIELD_SETPOINT = 0x0D
AQUAPURA_SPLIT_GREEN_FIELD_SILENCE = 0x0E
# Heating element / Force Disinfection field ids — both on the TIMERS
# selector (01,90), not STATUS; field ids are scoped per-selector, so
# 0x0E is unrelated to silence's.
AQUAPURA_SPLIT_GREEN_FIELD_HEATING_ELEMENT = 0x0E
AQUAPURA_SPLIT_GREEN_FIELD_FORCE_DISINFECTION = 0x0F

# Operating-mode marker byte -> app label, shared by the live STATUS frame
# (body[14]) and a daily-schedule slot's mode byte (slot offset +1) — both
# confirmed to use the SAME encoding (0x40 Eco / 0x80 Disparo) by diffing real
# before/after capture pairs. An unrecognised marker is reported as
# Unknown(0xNN) rather than guessed.
_AQUAPURA_SPLIT_GREEN_MODE_MARKERS = {0x40: "Eco", 0x80: "Disparo"}
_AQUAPURA_SPLIT_GREEN_MODE_MARKERS_REVERSE = {
    v: k for k, v in _AQUAPURA_SPLIT_GREEN_MODE_MARKERS.items()
}

# 16-bit sentinel for a sensor that is not present/readable.
AQUAPURA_SPLIT_GREEN_SENSOR_ABSENT = 0x7FFF
# Plausibility window for a decoded 16-bit temperature. An all-0xff padded or
# stub frame decodes to 6553.5 °C; such a value means "no reading", not a
# temperature, so it is reported as None rather than pushed to HA.
_AQUAPURA_SPLIT_GREEN_TEMP_MIN = -50.0
_AQUAPURA_SPLIT_GREEN_TEMP_MAX = 150.0

# The Split Green's "sensors" fetch returns the TANK frame, which shares no layout
# with the standard subtype-0x02 sensors frame — every temperature
# decode_its_sensors produces from it is meaningless. They are nulled so HA
# shows them unavailable, and the real readings are routed to th_temp (tank) and
# t4_temp (outdoor ambient, from the ODU frame). t4_temp is NOT in this list: it
# carries a confirmed value that apply_profile_to_sensors must not clobber.
_AQUAPURA_SPLIT_GREEN_SUPPRESSED_TEMPS: dict[str, Any] = {
    "t3_temp": None,
    "t2_temp": None,
    "t2b_temp": None,
    "twin_temp": None,
    "twout_temp": None,
    "t1_temp": None,
    "tf_temp": None,
    "tp_temp": None,
}


def build_aquapura_split_green_query(selector: int, param: int) -> str:
    """Build an Aquapura Split Green (sn8 17186T3A) 21-byte selector query frame.

    Reproduces the commands captured from the official app byte-for-byte, e.g.
    ``build_aquapura_split_green_query(0x01, 0xf4)`` → the STATUS command
    ``aa14c3000000000000030001f4ffffffff00ffff37``.
    """
    frame = [
        0xAA, 0x14, 0xC3,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x03, 0x00,
        selector, param,
        0xFF, 0xFF, 0xFF, 0xFF, 0x00, 0xFF, 0xFF,
    ]
    frame.append((~sum(frame[1:]) + 1) & 0xFF)
    return bytes(frame).hex()


def _aquapura_split_green_temp16(body: bytes, index: int) -> float | None:
    """Decode a Split Green 16-bit big-endian tenths-of-°C temperature at ``index``.

    Returns None when the frame is too short, when the sensor reports the
    0x7fff "absent" sentinel, or when the value is outside a plausible
    temperature range (a padded/stub frame).
    """
    if len(body) <= index + 1:
        return None
    raw = (body[index] << 8) | body[index + 1]
    if raw == AQUAPURA_SPLIT_GREEN_SENSOR_ABSENT:
        return None
    value = raw / 10
    if not _AQUAPURA_SPLIT_GREEN_TEMP_MIN <= value <= _AQUAPURA_SPLIT_GREEN_TEMP_MAX:
        return None
    return value


def decode_aquapura_split_green_status(body: bytearray | bytes) -> ITSStatus:
    """Decode an Aquapura Split Green (sn8 17186T3A) STATUS frame into an ITSStatus.

    Surfaces the live DHW setpoint at body[15:17] (16-bit BE tenths °C, NOT
    body[27] — see module notes for the correction) through ``t5s_def`` (which
    the climate entity's target_temperature reads) and ``set_temperature``; the
    power state at body[13] through ``mode``/``mode_name``; the "Eco"/
    "Disparo" operating preset at body[14] through ``operating_mode``; and the
    "Silence" quiet-mode toggle at body[17] through ``silence``. All are
    confirmed by diffing real before/after capture pairs (see module notes
    above).
    """
    status = ITSStatus()
    status.raw_body = bytes(body)

    # No fault is signalled in the captured frames, and no compressor-running
    # byte is confirmed for this model.
    status.error_code = 0
    status.comp_running = False

    if len(body) > AQUAPURA_SPLIT_GREEN_POWER_INDEX:
        if body[AQUAPURA_SPLIT_GREEN_POWER_INDEX] == 0:
            status.mode = AQUAPURA_SPLIT_GREEN_MODE_OFF
            status.mode_name = "Off"
        else:
            status.mode = AQUAPURA_SPLIT_GREEN_MODE_ON
            status.mode_name = "Heat"

    if len(body) > AQUAPURA_SPLIT_GREEN_OPERATING_MODE_INDEX:
        marker = body[AQUAPURA_SPLIT_GREEN_OPERATING_MODE_INDEX]
        status.operating_mode = _AQUAPURA_SPLIT_GREEN_MODE_MARKERS.get(
            marker, f"Unknown(0x{marker:02x})",
        )

    if len(body) > AQUAPURA_SPLIT_GREEN_SILENCE_INDEX:
        status.silence = body[AQUAPURA_SPLIT_GREEN_SILENCE_INDEX] != 0

    setpoint = _aquapura_split_green_temp16(
        bytes(body), AQUAPURA_SPLIT_GREEN_SETPOINT_INDEX,
    )
    if setpoint is not None:
        status.t5s_def = setpoint
        status.set_temperature = round(setpoint)

    return status


def _build_aquapura_split_green_write(
    field_id: int, value: bytes,
    selector: tuple[int, int] = AQUAPURA_SPLIT_GREEN_STATUS_SELECTOR,
) -> str:
    """Build an Aquapura Split Green write frame for one (field_id, value).

    Shared shape behind every captured single-field write (power, operating
    mode, DHW setpoint, silence — all on the STATUS selector 01,f4 — and the
    heating element, on the 01,90 selector) — see the module notes for the
    full field tables:

        aa <len> c3 00×6 02 00 <selector hi> <selector lo> ff ff ff ff 01 ff ff 00
            <field_id> <len(value)> <value...> <cks>

    Field ids are scoped to the selector they're written on, not global — the
    heating element happens to reuse field id 0x0e on 01,90, unrelated to
    silence's 0x0e on 01,f4.
    """
    prefix = [
        0xAA, 0x00, 0xC3,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x02, 0x00,
        selector[0], selector[1],
        0xFF, 0xFF, 0xFF, 0xFF, 0x01, 0xFF, 0xFF, 0x00,
        field_id, len(value),
    ]
    frame = prefix + list(value)
    frame[1] = len(frame)
    frame.append((~sum(frame[1:]) + 1) & 0xFF)
    return bytes(frame).hex()


def build_aquapura_split_green_power_command(power_on: bool) -> str:
    """Return the Aquapura Split Green (sn8 17186T3A) power ON/OFF write command.

    Captured from the official app (mitmproxy) toggling power via the KJR-86T3
    controller. Reproduces both captures byte-exact:
    ``build_aquapura_split_green_power_command(True)``  ->
      ``aa18c3000000000000020001f4ffffffff01ffff000b010126``
    ``build_aquapura_split_green_power_command(False)`` ->
      ``aa18c3000000000000020001f4ffffffff01ffff000b010027``
    """
    return _build_aquapura_split_green_write(
        AQUAPURA_SPLIT_GREEN_FIELD_POWER, bytes([0x01 if power_on else 0x00]),
    )


def build_aquapura_split_green_operating_mode_command(mode: str) -> str:
    """Return the Aquapura Split Green write command for the "Eco"/"Disparo"
    operating preset. Captured toggling the mode in the app (mitmproxy);
    reproduces both captures byte-exact:
    ``build_aquapura_split_green_operating_mode_command("Eco")``     ->
      ``aa18c3000000000000020001f4ffffffff01ffff000c0140e6``
    ``build_aquapura_split_green_operating_mode_command("Disparo")`` ->
      ``aa18c3000000000000020001f4ffffffff01ffff000c0180a6``

    Raises ``ValueError`` for any other value rather than sending an unproven
    marker byte.
    """
    marker = _AQUAPURA_SPLIT_GREEN_MODE_MARKERS_REVERSE.get(mode)
    if marker is None:
        raise ValueError(
            f"Unknown Aquapura Split Green operating mode: {mode!r} "
            f"(expected one of {sorted(_AQUAPURA_SPLIT_GREEN_MODE_MARKERS_REVERSE)})"
        )
    return _build_aquapura_split_green_write(
        AQUAPURA_SPLIT_GREEN_FIELD_OPERATING_MODE, bytes([marker]),
    )


def build_aquapura_split_green_silence_command(silence_on: bool) -> str:
    """Return the Aquapura Split Green "Silence" quiet-mode ON/OFF write command.

    Captured toggling Silence in the app (mitmproxy); reproduces both
    captures byte-exact:
    ``build_aquapura_split_green_silence_command(True)``  ->
      ``aa18c3000000000000020001f4ffffffff01ffff000e010123``
    ``build_aquapura_split_green_silence_command(False)`` ->
      ``aa18c3000000000000020001f4ffffffff01ffff000e010024``
    """
    return _build_aquapura_split_green_write(
        AQUAPURA_SPLIT_GREEN_FIELD_SILENCE, bytes([0x01 if silence_on else 0x00]),
    )


def build_aquapura_split_green_heating_element_command(enabled: bool) -> str:
    """Return the Aquapura Split Green heating element ON/OFF write command.

    Unlike every other single-field write, this one is on the TIMERS
    selector (01,90), not STATUS (01,f4). Captured toggling the heating
    element in the app (mitmproxy); reproduces both captures byte-exact:
    ``build_aquapura_split_green_heating_element_command(True)``  ->
      ``aa18c300000000000002000190ffffffff01ffff000e010187``
    ``build_aquapura_split_green_heating_element_command(False)`` ->
      ``aa18c300000000000002000190ffffffff01ffff000e010088``
    """
    return _build_aquapura_split_green_write(
        AQUAPURA_SPLIT_GREEN_FIELD_HEATING_ELEMENT,
        bytes([0x01 if enabled else 0x00]),
        selector=AQUAPURA_SPLIT_GREEN_TIMERS_SELECTOR,
    )


def decode_aquapura_split_green_heating_element(
    body: bytearray | bytes,
) -> bool | None:
    """Decode the heating element enable bit from a Split Green 01,90 frame.

    None if the frame is too short to carry it.
    """
    if len(body) <= AQUAPURA_SPLIT_GREEN_HEATING_ELEMENT_INDEX:
        return None
    return body[AQUAPURA_SPLIT_GREEN_HEATING_ELEMENT_INDEX] != 0


def build_aquapura_split_green_force_disinfection_command(enabled: bool) -> str:
    """Return the Aquapura Split Green "Force Disinfection" (manual, one-off)
    ON/OFF write command. Same TIMERS selector (01,90) as the heating
    element, different field id. Captured toggling it in the app
    (mitmproxy); reproduces both captures byte-exact:
    ``build_aquapura_split_green_force_disinfection_command(True)``  ->
      ``aa18c300000000000002000190ffffffff01ffff000f010186``
    ``build_aquapura_split_green_force_disinfection_command(False)`` ->
      ``aa18c300000000000002000190ffffffff01ffff000f010087``
    """
    return _build_aquapura_split_green_write(
        AQUAPURA_SPLIT_GREEN_FIELD_FORCE_DISINFECTION,
        bytes([0x01 if enabled else 0x00]),
        selector=AQUAPURA_SPLIT_GREEN_TIMERS_SELECTOR,
    )


def decode_aquapura_split_green_force_disinfection(
    body: bytearray | bytes,
) -> bool | None:
    """Decode the "Force Disinfection" enable bit from a Split Green 01,90 frame.

    None if the frame is too short to carry it.
    """
    if len(body) <= AQUAPURA_SPLIT_GREEN_FORCE_DISINFECTION_INDEX:
        return None
    return body[AQUAPURA_SPLIT_GREEN_FORCE_DISINFECTION_INDEX] != 0


@dataclasses.dataclass(frozen=True, kw_only=True)
class AquapuraSplitGreenConsumption:
    """Aquapura Split Green "Consumption" page energy totals, all kWh."""

    day: float = 0.0
    week: float = 0.0
    month: float = 0.0
    year: float = 0.0
    total: float = 0.0


def decode_aquapura_split_green_consumption(
    body: bytearray | bytes,
) -> AquapuraSplitGreenConsumption | None:
    """Decode day/week/month/year/total energy from a Split Green 03,20 frame.

    None if the frame is too short to carry them.
    """
    if len(body) <= AQUAPURA_SPLIT_GREEN_CONSUMPTION_TOTAL_INDEX + 3:
        return None

    def _u32(index: int) -> float:
        raw = int.from_bytes(body[index:index + 4], "big")
        return raw / 100

    return AquapuraSplitGreenConsumption(
        day=_u32(AQUAPURA_SPLIT_GREEN_CONSUMPTION_DAY_INDEX),
        week=_u32(AQUAPURA_SPLIT_GREEN_CONSUMPTION_WEEK_INDEX),
        month=_u32(AQUAPURA_SPLIT_GREEN_CONSUMPTION_MONTH_INDEX),
        year=_u32(AQUAPURA_SPLIT_GREEN_CONSUMPTION_YEAR_INDEX),
        total=_u32(AQUAPURA_SPLIT_GREEN_CONSUMPTION_TOTAL_INDEX),
    )


def build_aquapura_split_green_setpoint_command(temp_c: float) -> str:
    """Return the Aquapura Split Green DHW-setpoint write command for ``temp_c``.

    Captured changing the setpoint in the app (mitmproxy), three times in a
    row (50->51->52->50 °C); reproduces all three byte-exact:
    ``build_aquapura_split_green_setpoint_command(51)`` ->
      ``aa19c3000000000000020001f4ffffffff01ffff000d0201fe24``
    ``build_aquapura_split_green_setpoint_command(52)`` ->
      ``aa19c3000000000000020001f4ffffffff01ffff000d02020819``
    ``build_aquapura_split_green_setpoint_command(50)`` ->
      ``aa19c3000000000000020001f4ffffffff01ffff000d0201f42e``

    ``temp_c`` is encoded as 16-bit BE tenths of °C (e.g. 51 -> 0x01fe = 510),
    matching the resolution the app's own captured writes use.
    """
    tenths = round(temp_c * 10)
    return _build_aquapura_split_green_write(
        AQUAPURA_SPLIT_GREEN_FIELD_SETPOINT, tenths.to_bytes(2, "big"),
    )


def decode_aquapura_split_green_tank_temp(body: bytearray | bytes) -> float | None:
    """Return the DHW tank temperature °C from a Split Green TANK (0x03,0x84) frame.

    body[30:32] is the live water temperature as 16-bit big-endian tenths
    (0x01ec → 49.2 °C, 0x01e7 → 48.7 °C across two captures, both matching the
    app). None when unavailable.
    """
    return _aquapura_split_green_temp16(
        bytes(body), AQUAPURA_SPLIT_GREEN_TANK_TEMP_INDEX,
    )


def decode_aquapura_split_green_ambient_temp(
    body: bytearray | bytes | None,
) -> float | None:
    """Return the outdoor ambient °C from a Split Green ODU (0x03,0xe8) frame.

    body[49:51] is the ambient as 16-bit big-endian tenths (0x00d4 → 21.2 °C,
    0x00d0 → 20.8 °C across two captures, matching the app's 21 and 20 — the app
    truncates). None when the frame is missing or unreadable.
    """
    if not body:
        return None
    return _aquapura_split_green_temp16(
        bytes(body), AQUAPURA_SPLIT_GREEN_AMBIENT_TEMP_INDEX,
    )


def decode_aquapura_split_green_sensors(
    tank_body: bytearray | bytes,
    odu_body: bytearray | bytes | None = None,
) -> ITSSensors:
    """Build an ITSSensors from the Split Green's TANK and ODU frames.

    This model splits its readings across two selector frames, so the sensors
    poll fetches both (see ``ILetComfortClient.query_sensors``). Only the two
    confirmed values are surfaced — the tank temp on ``th_temp`` ("DHW Tank
    Temperature", and the climate ``current_temperature`` for this profile) and
    the outdoor ambient on ``t4_temp`` ("Outdoor Ambient Temperature"). Every
    other temperature field is nulled: nothing else in these frames is validated
    against the app, so HA shows those sensors unavailable rather than wrong.

    ``odu_body`` is optional: the ODU fetch is best-effort, and losing it costs
    only the ambient reading.
    """
    fields: dict[str, Any] = dict(_AQUAPURA_SPLIT_GREEN_SUPPRESSED_TEMPS)
    fields["th_temp"] = decode_aquapura_split_green_tank_temp(tank_body)
    fields["t4_temp"] = decode_aquapura_split_green_ambient_temp(odu_body)
    return dataclasses.replace(ITSSensors(raw_body=bytes(tank_body)), **fields)


# Number of "Temporiz. N" slots the app's daily-schedule page shows.
AQUAPURA_SPLIT_GREEN_SCHEDULE_SLOT_COUNT = 4
# body[] index of Temporiz. 1's "active" byte; each subsequent slot is one
# AQUAPURA_SPLIT_GREEN_SCHEDULE_SLOT_SIZE block later.
AQUAPURA_SPLIT_GREEN_SCHEDULE_FIRST_SLOT_INDEX = 45
AQUAPURA_SPLIT_GREEN_SCHEDULE_SLOT_SIZE = 8


@dataclasses.dataclass(frozen=True, kw_only=True)
class AquapuraSplitGreenScheduleSlot:
    """One Aquapura Split Green daily-timer slot ("Temporiz. N" in the app)."""

    active: bool = False
    mode: str | None = None
    setpoint: float | None = None
    start_time: str | None = None
    end_time: str | None = None


def decode_aquapura_split_green_daily_schedule(
    body: bytearray | bytes,
) -> list[AquapuraSplitGreenScheduleSlot]:
    """Decode the 4 "Tempor. diário" slots from a Split Green 02,58 frame.

    Each 8-byte block at ``body[45 + n*8 : 53 + n*8]`` (slot ``n``, 0-indexed)
    is ``[active, mode_marker, setpoint_hi, setpoint_lo, start_h, start_m,
    end_h, end_m]``. Validated byte-for-byte against a live app screenshot
    showing all 4 slots — including the disabled ones, whose setpoint/time
    config is present in the frame regardless of the active flag, matching
    what the app displays for a disabled slot.

    Always returns AQUAPURA_SPLIT_GREEN_SCHEDULE_SLOT_COUNT slots; a slot the
    frame is too short to carry decodes to all-defaults (inactive/None) rather
    than raising.
    """
    slots: list[AquapuraSplitGreenScheduleSlot] = []
    for n in range(AQUAPURA_SPLIT_GREEN_SCHEDULE_SLOT_COUNT):
        start = (
            AQUAPURA_SPLIT_GREEN_SCHEDULE_FIRST_SLOT_INDEX
            + n * AQUAPURA_SPLIT_GREEN_SCHEDULE_SLOT_SIZE
        )
        if len(body) <= start + AQUAPURA_SPLIT_GREEN_SCHEDULE_SLOT_SIZE - 1:
            slots.append(AquapuraSplitGreenScheduleSlot())
            continue

        marker = body[start + 1]
        setpoint = ((body[start + 2] << 8) | body[start + 3]) / 10
        slots.append(
            AquapuraSplitGreenScheduleSlot(
                active=body[start] != 0,
                mode=_AQUAPURA_SPLIT_GREEN_MODE_MARKERS.get(
                    marker, f"Unknown(0x{marker:02x})",
                ),
                setpoint=setpoint,
                start_time=f"{body[start + 4]:02d}:{body[start + 5]:02d}",
                end_time=f"{body[start + 6]:02d}:{body[start + 7]:02d}",
            )
        )
    return slots


# Disinfection ("Desinfecção") settings, packed in the SAME 02,58 frame as the
# daily schedule, just before the first schedule slot (body[45]). Confirmed by
# a real capture series: opening the app's Desinfecção submenu (ON, 65 °C,
# 14:00, every 7 days) matched body[38:44] exactly, and four further captures
# — enable ON->OFF, temp 65->68 °C, hour 14->13, cycle 7->6 — each changed
# EXACTLY the one corresponding byte and nothing else:
#   body[38]    = enabled (0x00/0x01)
#   body[39:41] = hour, minute (direct, e.g. 0x0e,0x00 -> "14:00")
#   body[41:43] = temperature, 16-bit BE tenths of °C (0x028a -> 65.0)
#   body[43]    = cycle days (0x07 -> every 7 days)
# body[44] (constant 0x04 across every capture) is left unmapped — no
# confirmed meaning.
AQUAPURA_SPLIT_GREEN_DISINFECTION_ENABLE_INDEX = 38
AQUAPURA_SPLIT_GREEN_DISINFECTION_HOUR_INDEX = 39
AQUAPURA_SPLIT_GREEN_DISINFECTION_MINUTE_INDEX = 40
AQUAPURA_SPLIT_GREEN_DISINFECTION_TEMP_INDEX = 41
AQUAPURA_SPLIT_GREEN_DISINFECTION_CYCLE_DAYS_INDEX = 43

# Write-command field ids for the disinfection SET frame (selector 0x02,0x58,
# same header shape as the STATUS-selector writes but with FIVE fields sent
# together — the app always resends every field, not just the one the user
# changed). See build_aquapura_split_green_disinfection_command.
AQUAPURA_SPLIT_GREEN_FIELD_DISINFECTION_ENABLE = 0x24
AQUAPURA_SPLIT_GREEN_FIELD_DISINFECTION_HOUR = 0x25
AQUAPURA_SPLIT_GREEN_FIELD_DISINFECTION_MINUTE = 0x26
AQUAPURA_SPLIT_GREEN_FIELD_DISINFECTION_TEMP = 0x27
AQUAPURA_SPLIT_GREEN_FIELD_DISINFECTION_CYCLE_DAYS = 0x28


@dataclasses.dataclass(frozen=True, kw_only=True)
class AquapuraSplitGreenDisinfectionSettings:
    """Aquapura Split Green "Desinfecção" (disinfection) settings."""

    enabled: bool = False
    hour: int = 0
    minute: int = 0
    temperature: float = 0.0
    cycle_days: int = 0


def decode_aquapura_split_green_disinfection(
    body: bytearray | bytes,
) -> AquapuraSplitGreenDisinfectionSettings | None:
    """Decode the disinfection settings from a Split Green 02,58 frame.

    None if the frame is too short to carry them (rather than a stub of
    defaults, which would read as "disinfection off" and could mislead a
    caller merging these into a write).
    """
    if len(body) <= AQUAPURA_SPLIT_GREEN_DISINFECTION_CYCLE_DAYS_INDEX:
        return None
    temp_hi = body[AQUAPURA_SPLIT_GREEN_DISINFECTION_TEMP_INDEX]
    temp_lo = body[AQUAPURA_SPLIT_GREEN_DISINFECTION_TEMP_INDEX + 1]
    return AquapuraSplitGreenDisinfectionSettings(
        enabled=body[AQUAPURA_SPLIT_GREEN_DISINFECTION_ENABLE_INDEX] != 0,
        hour=body[AQUAPURA_SPLIT_GREEN_DISINFECTION_HOUR_INDEX],
        minute=body[AQUAPURA_SPLIT_GREEN_DISINFECTION_MINUTE_INDEX],
        temperature=((temp_hi << 8) | temp_lo) / 10,
        cycle_days=body[AQUAPURA_SPLIT_GREEN_DISINFECTION_CYCLE_DAYS_INDEX],
    )


def build_aquapura_split_green_disinfection_command(
    *, enabled: bool, hour: int, minute: int, temp_c: float, cycle_days: int,
) -> str:
    """Return the Aquapura Split Green disinfection-settings write command.

    Unlike the single-field STATUS-selector writes, the app always sends all
    five fields together in one selector-0x02,0x58 command, so every caller
    must supply the full set (merge with the last known values for the ones
    the user didn't change). Each field entry is ``id, len, value...``,
    padded with one extra ``0x00`` byte — EXCEPT the last field, which has
    none; this was confirmed byte-for-byte, not guessed, against 5 real
    captures (enable ON<->OFF, temp 65->68, hour 14->13, cycle 7->6):
    ``build_aquapura_split_green_disinfection_command(enabled=False, hour=14, minute=0, temp_c=65, cycle_days=7)``
        -> ``aa29c300000000000002000258ffffffff01ffff002401000025010e00260100002702028a0028010758``
    ``build_aquapura_split_green_disinfection_command(enabled=True, hour=14, minute=0, temp_c=65, cycle_days=7)``
        -> ``aa29c300000000000002000258ffffffff01ffff002401010025010e00260100002702028a0028010757``
    ``build_aquapura_split_green_disinfection_command(enabled=True, hour=14, minute=0, temp_c=68, cycle_days=7)``
        -> ``aa29c300000000000002000258ffffffff01ffff002401010025010e0026010000270202a80028010739``
    ``build_aquapura_split_green_disinfection_command(enabled=True, hour=13, minute=0, temp_c=68, cycle_days=7)``
        -> ``aa29c300000000000002000258ffffffff01ffff002401010025010d0026010000270202a8002801073a``
    ``build_aquapura_split_green_disinfection_command(enabled=True, hour=13, minute=0, temp_c=68, cycle_days=6)``
        -> ``aa29c300000000000002000258ffffffff01ffff002401010025010d0026010000270202a8002801063b``
    """
    prefix = [
        0xAA, 0x00, 0xC3,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x02, 0x00,
        0x02, 0x58,
        0xFF, 0xFF, 0xFF, 0xFF, 0x01, 0xFF, 0xFF, 0x00,
    ]
    tenths = round(temp_c * 10)
    fields = [
        (AQUAPURA_SPLIT_GREEN_FIELD_DISINFECTION_ENABLE, [0x01 if enabled else 0x00]),
        (AQUAPURA_SPLIT_GREEN_FIELD_DISINFECTION_HOUR, [hour]),
        (AQUAPURA_SPLIT_GREEN_FIELD_DISINFECTION_MINUTE, [minute]),
        (AQUAPURA_SPLIT_GREEN_FIELD_DISINFECTION_TEMP, list(tenths.to_bytes(2, "big"))),
        (AQUAPURA_SPLIT_GREEN_FIELD_DISINFECTION_CYCLE_DAYS, [cycle_days]),
    ]
    frame = list(prefix)
    for i, (field_id, value) in enumerate(fields):
        frame += [field_id, len(value), *value]
        if i != len(fields) - 1:
            frame.append(0x00)
    frame[1] = len(frame)
    frame.append((~sum(frame[1:]) + 1) & 0xFF)
    return bytes(frame).hex()


def apply_profile_to_status(profile: ModelProfile, status: ITSStatus) -> ITSStatus:
    """Return the profile-canonical ITSStatus for a decoded status object.

    STANDARD and AQUAPURA return ``status`` untouched (AQUAPURA's only override
    is on the sensors side). ATW re-decodes the raw frame via the ATW layout;
    KJRH120L re-decodes it via the KJRH-120L layout (clean setpoint, no garbage);
    AQUAPURA_SPLIT_GREEN re-decodes its app-selector STATUS frame the same way.
    """
    if profile is ModelProfile.ATW:
        return decode_atw_status(status.raw_body)
    if profile is ModelProfile.KJRH120L:
        return decode_kjrh120l_status(status.raw_body)
    if profile is ModelProfile.AQUAPURA_SPLIT_GREEN:
        return decode_aquapura_split_green_status(status.raw_body)
    return status


def apply_profile_to_sensors(
    profile: ModelProfile,
    sensors: ITSSensors,
    status: ITSStatus,
) -> ITSSensors:
    """Return the profile-canonical ITSSensors for the decoded sensors object.

    STANDARD returns ``sensors`` untouched. ATW and AQUAPURA both route a tank
    temperature into ``th_temp`` — the field the "DHW Tank Temperature" sensor
    reads — so that entity shows the meaningful value without entity-wiring
    changes. ``twin_temp`` (the "Water Inlet Temperature" sensor) is deliberately
    left untouched: these units expose no real inlet reading, so it stays honest
    (None/0) rather than being mislabeled with the tank temperature.

    Climate ``current_temperature`` is made profile-aware separately (it returns
    ``th_temp`` for ATW/AQUAPURA) so the climate card still shows the tank temp.

    - ATW: ``status.box_bottom_temp`` carries the DHW tank temp (byte[22]).
    - AQUAPURA: ``status.box_bottom_temp`` (status byte[17], offset-decoded) is
      the water tank temperature the app shows; the STANDARD ``twin_temp`` source
      is null-filled to 0 on this model (issue #12).
    - KJRH120L: water/outdoor temps are NOT exposed by this model's API (the
      sensors frame is static/cached), so every temperature field is nulled — HA
      shows them unavailable rather than the misleading constant -35 the STANDARD
      decode would produce (issues #21 / #5).
    - AQUAPURA_SPLIT_GREEN: the "sensors" fetch returns this model's TANK frame
      (selector 0x03,0x84), so the standard temperatures decoded from it are
      meaningless and are nulled; the real tank temp (body[30:32], 16-bit BE
      tenths) is decoded from that same raw frame into ``th_temp``. ``t4_temp``
      is left alone — it carries the outdoor ambient from the ODU frame, which
      ``query_sensors`` fetched separately and this raw body cannot reproduce.
    """
    if profile is ModelProfile.KJRH120L:
        return dataclasses.replace(sensors, **_KJRH120L_SUPPRESSED_TEMPS)
    if profile is ModelProfile.AQUAPURA_SPLIT_GREEN:
        return dataclasses.replace(
            sensors,
            **_AQUAPURA_SPLIT_GREEN_SUPPRESSED_TEMPS,
            th_temp=decode_aquapura_split_green_tank_temp(sensors.raw_body),
        )
    if profile in (ModelProfile.ATW, ModelProfile.AQUAPURA):
        if status is not None and status.box_bottom_temp is not None:
            return dataclasses.replace(sensors, th_temp=status.box_bottom_temp)
    return sensors


__all__ = [
    "AQUAPURA_SN8",
    "ATW_SN8",
    "AQUAPURA_SPLIT_GREEN_CONSUMPTION_SELECTOR",
    "AQUAPURA_SPLIT_GREEN_ODU_SELECTOR",
    "AQUAPURA_SPLIT_GREEN_SCHEDULE_SELECTOR",
    "AQUAPURA_SPLIT_GREEN_SCHEDULE_SLOT_COUNT",
    "AQUAPURA_SPLIT_GREEN_SN8",
    "AQUAPURA_SPLIT_GREEN_STATUS_SELECTOR",
    "AQUAPURA_SPLIT_GREEN_TANK_SELECTOR",
    "AQUAPURA_SPLIT_GREEN_TEMP_MAX",
    "AQUAPURA_SPLIT_GREEN_TEMP_MIN",
    "AQUAPURA_SPLIT_GREEN_TIMERS_SELECTOR",
    "AquapuraSplitGreenConsumption",
    "AquapuraSplitGreenDisinfectionSettings",
    "AquapuraSplitGreenScheduleSlot",
    "KJRH120L_DHW_OFF",
    "KJRH120L_DHW_ON",
    "KJRH120L_SN8",
    "KJRH120L_TEMP_MAX",
    "KJRH120L_TEMP_MIN",
    "ModelProfile",
    "apply_profile_to_sensors",
    "apply_profile_to_status",
    "build_aquapura_split_green_operating_mode_command",
    "build_aquapura_split_green_power_command",
    "build_aquapura_split_green_disinfection_command",
    "build_aquapura_split_green_force_disinfection_command",
    "build_aquapura_split_green_heating_element_command",
    "build_aquapura_split_green_query",
    "build_aquapura_split_green_setpoint_command",
    "build_aquapura_split_green_silence_command",
    "build_kjrh120l_set_temperature",
    "build_query_command",
    "decode_aquapura_split_green_ambient_temp",
    "decode_aquapura_split_green_consumption",
    "decode_aquapura_split_green_daily_schedule",
    "decode_aquapura_split_green_disinfection",
    "decode_aquapura_split_green_force_disinfection",
    "decode_aquapura_split_green_heating_element",
    "decode_aquapura_split_green_sensors",
    "decode_aquapura_split_green_status",
    "decode_aquapura_split_green_tank_temp",
    "decode_atw_status",
    "decode_kjrh120l_status",
    "resolve_profile",
]
