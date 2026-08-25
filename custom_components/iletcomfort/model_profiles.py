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
# CONFIRMED against the app (tank 49 °C, ambient 21 °C, OFF, setpoint 50 °C):
#   STATUS frame (0x01,0xf4): body[27] = DHW setpoint, direct °C (0x32 → 50).
#   TANK frame  (0x03,0x84):  body[30:32] = tank temp ×10 BE (0x01ec → 49.2).
#
# DELIBERATELY NOT DECODED (read-only v1 — awaiting a second capture):
#   - Power/mode. The status frame's candidate flag bytes (body[14]=0x40,
#     body[15]=0x02, body[16]=0x08, body[20]=0x01) were all captured with the
#     unit OFF, so none is pinned. mode stays 0/"Off" until a RUNNING capture
#     shows which byte flips — reporting a guessed mode would be worse than
#     reporting none.
#   - Ambient. The ODU frame (0x03,0xe8) carries several 16-bit sensors
#     (35.3 / 26.8 / 22.3 / 21.2 / 37.8 °C); 21.2 matches the app's ambient, but
#     which sensor the app labels "ambient" is unconfirmed, so it is not
#     surfaced.
#   - Writes (setpoint / on-off). No SET command has been captured for this
#     model, and the standard 62-byte C3 SET frame is built from a status query
#     this device does not answer — so set_device raises a clear error instead
#     of sending a frame assembled from garbage (see api.set_device).

# Query selectors (idx11/idx12 of the frame) captured from the app.
AQUAPURA_SPLIT_GREEN_STATUS_SELECTOR = (0x01, 0xF4)
AQUAPURA_SPLIT_GREEN_TANK_SELECTOR = (0x03, 0x84)

# Which selector serves each of the integration's two polled subtypes.
_AQUAPURA_SPLIT_GREEN_SUBTYPE_SELECTORS: dict[int, tuple[int, int]] = {
    0x01: AQUAPURA_SPLIT_GREEN_STATUS_SELECTOR,
    0x02: AQUAPURA_SPLIT_GREEN_TANK_SELECTOR,
}

AQUAPURA_SPLIT_GREEN_DHW_SETPOINT_INDEX = 27
AQUAPURA_SPLIT_GREEN_TANK_TEMP_INDEX = 30
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
# shows them unavailable, and the real tank temp is routed to th_temp.
_AQUAPURA_SPLIT_GREEN_SUPPRESSED_TEMPS = {
    "t3_temp": None,
    "t4_temp": None,
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

    Surfaces the one confirmed field — the DHW setpoint at body[27], a direct °C
    value — through ``t5s_def`` (which the climate entity's target_temperature
    reads) and ``set_temperature``. Power/mode is not decoded: no capture of
    this frame with the unit running exists yet, so mode stays 0/"Off". See the
    module notes above.
    """
    status = ITSStatus()
    status.raw_body = bytes(body)

    # No fault is signalled in the captured frames, and no compressor-running
    # byte is confirmed for this model.
    status.error_code = 0
    status.comp_running = False

    if len(body) > AQUAPURA_SPLIT_GREEN_DHW_SETPOINT_INDEX:
        setpoint = body[AQUAPURA_SPLIT_GREEN_DHW_SETPOINT_INDEX]
        status.t5s_def = float(setpoint)
        status.set_temperature = setpoint

    return status


def decode_aquapura_split_green_tank_temp(body: bytearray | bytes) -> float | None:
    """Return the DHW tank temperature °C from a Split Green TANK (0x03,0x84) frame.

    body[30:32] is the live water temperature as 16-bit big-endian tenths
    (0x01ec → 49.2 °C, confirmed against the app). None when unavailable.
    """
    return _aquapura_split_green_temp16(bytes(body), AQUAPURA_SPLIT_GREEN_TANK_TEMP_INDEX)


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
      tenths) is decoded from that same raw frame into ``th_temp``.
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
    "AQUAPURA_SPLIT_GREEN_SN8",
    "AQUAPURA_SPLIT_GREEN_STATUS_SELECTOR",
    "AQUAPURA_SPLIT_GREEN_TANK_SELECTOR",
    "KJRH120L_DHW_OFF",
    "KJRH120L_DHW_ON",
    "KJRH120L_SN8",
    "KJRH120L_TEMP_MAX",
    "KJRH120L_TEMP_MIN",
    "ModelProfile",
    "apply_profile_to_sensors",
    "apply_profile_to_status",
    "build_aquapura_split_green_query",
    "build_kjrh120l_set_temperature",
    "build_query_command",
    "decode_atw_status",
    "decode_aquapura_split_green_status",
    "decode_aquapura_split_green_tank_temp",
    "decode_kjrh120l_status",
    "resolve_profile",
]
