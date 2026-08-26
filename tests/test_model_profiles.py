"""Tests for sn8-gated model decode profiles (issues #22 and #12).

Different C3 heat-pump models pack their status/sensor frames differently, but
the cloud metadata exposes no device-class field — both an air-to-water (ATW)
unit and an air-to-air unit report ``applianceType="0xC3"`` /
``modelNumber="0"``. The only differentiator is the 8-char ``sn8`` model-code
serial prefix, so model-specific decoding is gated on an sn8 lookup table.

These tests pin:
- STANDARD (unknown / None sn8): byte-for-byte identical to the legacy decode.
- ATW (sn8 ``171H120F``, issue #22): the 25-byte status layout.
- AQUAPURA (sn8 ``171000AU``, issue #12): tank temp sourced from
  ``status.box_bottom_temp`` and surfaced on ``th_temp`` (the "DHW Tank
  Temperature" sensor) instead of ``sensors.twin_temp``.
- AQUAPURA_SPLIT_GREEN (sn8 ``17186T3A``): the app's 21-byte selector query frames
  (this model echoes the standard query back verbatim), the setpoint at
  status body[27] and the tank temp at tank-frame body[30:32].
"""

from __future__ import annotations

import pytest

from custom_components.iletcomfort.api import (
    MODE_HEAT,
    MODE_OFF,
    ApiError,
    ILetComfortClient,
    build_c3_query,
    decode_its_sensors,
    decode_its_status,
)
from custom_components.iletcomfort.model_profiles import (
    AQUAPURA_SN8,
    AQUAPURA_SPLIT_GREEN_SCHEDULE_SLOT_COUNT,
    AQUAPURA_SPLIT_GREEN_SN8,
    ATW_SN8,
    AquapuraSplitGreenConsumption,
    AquapuraSplitGreenDisinfectionSettings,
    AquapuraSplitGreenScheduleSlot,
    KJRH120L_DHW_OFF,
    KJRH120L_DHW_ON,
    KJRH120L_SN8,
    KJRH120L_TEMP_MAX,
    KJRH120L_TEMP_MIN,
    ModelProfile,
    apply_profile_to_sensors,
    apply_profile_to_status,
    build_aquapura_split_green_disinfection_command,
    build_aquapura_split_green_force_disinfection_command,
    build_aquapura_split_green_heating_element_command,
    build_aquapura_split_green_operating_mode_command,
    build_aquapura_split_green_power_command,
    build_aquapura_split_green_query,
    build_aquapura_split_green_schedule_active_command,
    build_aquapura_split_green_setpoint_command,
    build_aquapura_split_green_silence_command,
    build_kjrh120l_set_temperature,
    build_query_command,
    decode_aquapura_split_green_ambient_temp,
    decode_aquapura_split_green_consumption,
    decode_aquapura_split_green_daily_schedule,
    decode_aquapura_split_green_disinfection,
    decode_aquapura_split_green_force_disinfection,
    decode_aquapura_split_green_heating_element,
    decode_aquapura_split_green_odu_serial,
    decode_aquapura_split_green_sensors,
    decode_aquapura_split_green_status,
    decode_aquapura_split_green_tank_temp,
    decode_atw_status,
    decode_kjrh120l_status,
    resolve_profile,
)


def _make_client() -> ILetComfortClient:
    return ILetComfortClient(api_base="https://us.dollin.net")


def _c3_frame(body: bytes) -> str:
    header = bytes([0xAA, 0x00, 0xC3, 0, 0, 0, 0, 0, 0, 0x04])
    return (header + body + b"\x00").hex()


def _bytes(csv: str) -> bytes:
    return bytes(int(x, 16) for x in csv.split(","))


# --- The five real ATW (Italtherm, sn8 171H120F) status frames, issue #22 ---
ATW_FRAMES = {
    "state1": _bytes(
        "01,08,07,68,03,03,1e,28,32,2a,37,19,19,05,41,23,19,05,3c,22,3c,14,2e,00,80"
    ),
    "state2": _bytes(
        "01,0d,07,68,03,03,1e,28,32,3a,37,19,19,05,41,23,19,05,3c,22,3c,14,2e,00,80"
    ),
    "state3": _bytes(
        "01,0c,07,68,03,03,1e,28,2d,28,37,19,19,05,41,23,19,05,3c,22,3c,14,2e,00,80"
    ),
    "state4": _bytes(
        "01,0c,07,68,03,03,1e,28,32,28,37,19,19,05,41,23,19,05,3c,22,3c,14,2d,00,80"
    ),
    "state5": _bytes(
        "01,08,07,68,03,03,1e,28,32,28,37,19,19,05,41,23,19,05,3c,22,3c,14,2d,00,80"
    ),
}

# Expected per-frame values (DHW setpoint, Zone-1 setpoint °C, DHW tank °C).
ATW_EXPECTED = {
    "state1": (50, 21.0, 46.0),
    "state2": (50, 29.0, 46.0),
    "state3": (45, 20.0, 46.0),
    "state4": (50, 20.0, 45.0),
    "state5": (50, 20.0, 45.0),
}


# ---------------------------------------------------------------------------
# Profile resolution
# ---------------------------------------------------------------------------

def test_resolve_profile_known_sn8():
    assert resolve_profile(ATW_SN8) is ModelProfile.ATW
    assert resolve_profile(AQUAPURA_SN8) is ModelProfile.AQUAPURA


def test_resolve_profile_unknown_or_none_falls_back_to_standard():
    assert resolve_profile(None) is ModelProfile.STANDARD
    assert resolve_profile("") is ModelProfile.STANDARD
    assert resolve_profile("UNKNOWN8") is ModelProfile.STANDARD
    assert resolve_profile("00000000") is ModelProfile.STANDARD


def test_resolve_profile_kjrh120l_sn8():
    """The KJRH-120L sn8 (17100003, issues #21/#5) resolves to its profile."""
    assert KJRH120L_SN8 == "17100003"
    assert resolve_profile(KJRH120L_SN8) is ModelProfile.KJRH120L


# ---------------------------------------------------------------------------
# Query command encoding (KJRH-120L short form, issues #21 / #5)
# ---------------------------------------------------------------------------

def test_build_query_command_kjrh120l_short_form():
    """KJRH-120L's cloud rejects the long C3 query (code 1214); its app uses a
    short ``ffff<ss><ss><ss>ff`` form (subtype byte repeated 3×)."""
    assert build_query_command(ModelProfile.KJRH120L, 0x01) == "ffff010101ff"
    assert build_query_command(ModelProfile.KJRH120L, 0x02) == "ffff020202ff"


@pytest.mark.parametrize("subtype", [0x01, 0x02])
def test_build_query_command_standard_matches_build_c3_query(subtype):
    """STANDARD must emit the legacy long C3 query frame, byte-for-byte."""
    assert build_query_command(ModelProfile.STANDARD, subtype) == build_c3_query(subtype)


@pytest.mark.parametrize("profile", [ModelProfile.ATW, ModelProfile.AQUAPURA])
@pytest.mark.parametrize("subtype", [0x01, 0x02])
def test_build_query_command_other_profiles_use_long_frame(profile, subtype):
    """Other model profiles keep the standard long C3 query frame, unchanged."""
    assert build_query_command(profile, subtype) == build_c3_query(subtype)


# --- KJRH-120L short WRITE commands (issue #35) ---------------------------
# The KJRH-120L's cloud rejects the standard 62-byte C3 SET frame (same reason
# its long query was rejected). The official app uses short literal write
# commands of shape ``00 <field> 01 <value> ff`` (no checksum byte; the cloud
# does the framing). Captured from the app's proxy traffic with code:0 replies:
#   set DHW setpoint to T °C  → 0007 01 <T:1 byte hex> ff   (field 0x07)
#   DHW power ON              → 00020101ff                  (field 0x02)
#   DHW power OFF             → 00020100ff


def test_build_kjrh120l_set_temperature_matches_captured_commands():
    """Confirmed captures: T=60 → 0007013cff, T=49 → 00070131ff."""
    assert build_kjrh120l_set_temperature(60) == "0007013cff"
    assert build_kjrh120l_set_temperature(49) == "00070131ff"


def test_build_kjrh120l_set_temperature_two_hex_digits():
    """Single-digit values are zero-padded to two hex digits (e.g. 9 → 09)."""
    assert build_kjrh120l_set_temperature(9) == "00070109ff"
    assert build_kjrh120l_set_temperature(15) == "0007010fff"


def test_kjrh120l_dhw_power_constants():
    """DHW power on/off are the literal captured command strings."""
    assert KJRH120L_DHW_ON == "00020101ff"
    assert KJRH120L_DHW_OFF == "00020100ff"


# ---------------------------------------------------------------------------
# ATW status layout (issue #22)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", list(ATW_FRAMES))
def test_atw_decode_field_mappings(name):
    """Each captured ATW frame decodes to the validated DHW/zone-1/tank values."""
    dhw_set, zone1, tank = ATW_EXPECTED[name]
    status = decode_atw_status(ATW_FRAMES[name])

    # DHW setpoint = byte[8], direct value (NOT offset-decoded).
    assert status.set_temperature == dhw_set
    # Zone-1 / climate target setpoint = byte[9] / 2 (0.5° resolution),
    # surfaced via t5s_def so the climate entity reads it unchanged.
    assert status.t5s_def == zone1
    # DHW tank current temp = byte[22], direct value, surfaced via box_bottom_temp.
    assert status.box_bottom_temp == tank
    # byte[24] = 0x80 is a flags/MSB byte, not an error code; the app shows no
    # fault, so the ATW profile must report no error.
    assert status.error_code == 0
    # No confirmed running signal in these frames → conservatively not running.
    assert not status.comp_running


def test_atw_space_heat_demand_flag():
    """byte[1] bit0 marks space-heat demand; only state2 has it set."""
    assert decode_atw_status(ATW_FRAMES["state2"]).status_flags_raw & 0x01
    for name in ("state1", "state3", "state4", "state5"):
        assert not (decode_atw_status(ATW_FRAMES[name]).status_flags_raw & 0x01)


def test_atw_profile_applied_to_status_object():
    """apply_profile_to_status(ATW) re-decodes the frame via the ATW layout."""
    std = decode_its_status(ATW_FRAMES["state1"])
    # The STANDARD decode misreads this frame.
    assert std.error_code == 128
    assert std.set_temperature == 3

    atw = apply_profile_to_status(ModelProfile.ATW, std)
    assert atw.set_temperature == 50
    assert atw.t5s_def == 21.0
    assert atw.box_bottom_temp == 46.0
    assert atw.error_code == 0
    assert not atw.comp_running


def test_atw_tank_temp_routed_to_th_temp_not_twin_temp():
    """For ATW the DHW tank reading (byte[22]) surfaces on ``th_temp``.

    ``th_temp`` backs the "DHW Tank Temperature" sensor. ``twin_temp`` (the
    "Water Inlet Temperature" sensor) must be left honest — these units have no
    real inlet reading — so the override must NOT populate it.
    """
    sensors = decode_its_sensors(bytes([0x02]) + bytes(48))
    status = decode_atw_status(ATW_FRAMES["state1"])
    out = apply_profile_to_sensors(ModelProfile.ATW, sensors, status)
    assert out.th_temp == 46.0
    # Water Inlet stays untouched (None/0); never the tank value.
    assert out.twin_temp != 46.0
    assert out.twin_temp == sensors.twin_temp


@pytest.mark.parametrize("name", list(ATW_FRAMES))
def test_atw_dhw_tank_sensor_value_fn_returns_tank_temp(name):
    """The ``dhw_tank`` sensor ``value_fn`` (th_temp) returns the tank temp."""
    from custom_components.iletcomfort.sensor import SENSOR_DESCRIPTIONS

    _, _, tank = ATW_EXPECTED[name]
    sensors = decode_its_sensors(bytes([0x02]) + bytes(48))
    status = decode_atw_status(ATW_FRAMES[name])
    out = apply_profile_to_sensors(ModelProfile.ATW, sensors, status)

    dhw = next(d for d in SENSOR_DESCRIPTIONS if d.key == "dhw_tank")
    assert dhw.value_fn({"sensors": out, "status": status}) == tank
    # The Water Inlet sensor must not carry the tank value.
    water_inlet = next(d for d in SENSOR_DESCRIPTIONS if d.key == "water_inlet")
    assert water_inlet.value_fn({"sensors": out, "status": status}) != tank


# ---------------------------------------------------------------------------
# AQUAPURA water temperature source (issue #12)
# ---------------------------------------------------------------------------

# Representative AQUAPURA status frame: byte[17] = 0x4b → 75 → 75-35 = 40.0 °C
# is box_bottom_temp (water tank temp shown by the app). twin_temp source bytes
# are the 0x23 null-fill that decodes to 0 °C — the bug being fixed.
def _aquapura_status_body() -> bytes:
    body = bytearray([0x23] * 50)
    body[0] = 0x01
    body[17] = 0x4b  # status byte[17] = box_bottom_temp raw → 40.0 °C
    return bytes(body)


def _aquapura_sensors_body() -> bytes:
    # twin_temp = sensors byte[24] (d+23 region); 0x23 → 0x23-0x23... arranged
    # so twin_temp decodes to 0 °C. t4_temp byte[22]=0x36 → 19 °C (sanity).
    body = bytearray([0x23] * 50)
    body[0] = 0x02
    body[22] = 0x36  # t4_temp raw 0x36 = 54 → 19 °C
    body[25] = 0x23  # twin_temp raw 0x23 = 35 → 0.0 °C
    return bytes(body)


def test_aquapura_tank_temp_from_box_bottom_temp_on_th_temp():
    """AQUAPURA tank temp must source box_bottom_temp and surface on th_temp.

    The "DHW Tank Temperature" sensor reads ``th_temp``; ``twin_temp`` (Water
    Inlet) must stay honest — this model has no real inlet reading.
    """
    status = decode_its_status(_aquapura_status_body())
    sensors = decode_its_sensors(_aquapura_sensors_body())

    # Baseline: twin_temp decodes to 0 while box_bottom_temp holds 40 °C.
    assert sensors.twin_temp == 0.0
    assert status.box_bottom_temp == 40.0

    out = apply_profile_to_sensors(ModelProfile.AQUAPURA, sensors, status)
    assert out.th_temp == 40.0
    # Water Inlet left honest (not populated with the tank value).
    assert out.twin_temp == sensors.twin_temp
    assert out.twin_temp != 40.0


def test_aquapura_ambient_unchanged():
    """The AQUAPURA override must not disturb the (already-correct) ambient temp."""
    status = decode_its_status(_aquapura_status_body())
    sensors = decode_its_sensors(_aquapura_sensors_body())
    out = apply_profile_to_sensors(ModelProfile.AQUAPURA, sensors, status)
    assert out.t4_temp == 19.0


# ---------------------------------------------------------------------------
# KJRH-120L decode (sn8 17100003, issues #21 / #5)
# ---------------------------------------------------------------------------
#
# Real STATUS (0x01) frame — device Off, app shows DHW setpoint 60 °C. The
# STANDARD decoder misreads this 95-byte frame and emits garbage:
#   set_temperature=66, t5s_def=-35, error_code=1 (fake fault),
#   comp_running=True (it's Off), total_kwh=1340, comp_frq=6425, sensors -35.
# decode_kjrh120l_status surfaces only the CONFIRMED fields:
#   - power/mode from body[10] (0x00 → Off; non-zero → On/Heat). body[2] stays
#     0x00 even when the unit is on, so it is NOT the power byte (issue #35).
#   - DHW setpoint from body[15] (direct °C; 0x3c → 60), surfaced via t5s_def so
#     the climate target_temperature reads it.
#   - error_code = 0, comp_running = False; everything else left at defaults.
#
# This is the OFF capture: body[10] = 0x00, setpoint body[15] = 0x3c (60 °C).
KJRH120L_STATUS_BODY = _bytes(
    "01,fe,00,00,00,42,00,56,00,00,00,03,41,1e,30,3c,00,00,00,00,00,00,01,"
    "00,01,00,00,00,01,00,00,00,00,00,01,02,02,4b,23,19,05,37,19,19,05,3c,"
    "22,46,14,13,00,01,01,02,03,01,01,e7,2f,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,"
    "ff,30,ff,ff,ff,ff,00,00,00,01,00,00,00,00,00,17,07,14,0f,20,00,00,00,"
    "00,00,ff"
)

# Real ON capture — body[10] = 0x01 (the only state byte that flips vs the OFF
# frame besides the setpoint and timestamp tail), setpoint body[15] = 0x41 (65).
KJRH120L_STATUS_BODY_ON = _bytes(
    "01,fe,00,00,00,42,00,56,00,00,01,03,41,1e,30,41,00,00,00,00,00,00,01,"
    "00,01,00,00,00,01,00,00,00,00,00,01,02,02,4b,23,19,05,37,19,19,05,3c,"
    "22,46,14,13,00,01,01,02,03,01,01,e7,2f,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,"
    "ff,30,ff,ff,ff,ff,00,00,00,01,00,00,00,00,00,17,07,14,0f,20,00,00,00,"
    "00,00,ff"
)

# Real SENSORS (0x02) frame — static/cached; carries NO live temps. Water and
# outdoor temps never appear in any byte across states, so they are not
# decodable for this model and must be suppressed (left None / unavailable).
KJRH120L_SENSORS_BODY = _bytes(
    "02,fe,00,46,00,9f,00,5a,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,"
    "00,00,00,00,00,00,00,00,32,00,00,00,00,00,00,00,00,01,41,13,00,00,00,"
    "32,14,00,ff"
)


def test_kjrh120l_status_surfaces_confirmed_setpoint_only():
    """The KJRH-120L status decode surfaces only confirmed fields (setpoint 60)."""
    status = decode_kjrh120l_status(KJRH120L_STATUS_BODY)

    # body[10] = power (0x00 → Off → mode 0).
    assert status.mode == 0
    assert status.mode_name == "Off"
    # body[15] = DHW setpoint, direct °C (0x3c = 60). Surfaced via t5s_def so the
    # climate target_temperature (t5s_def if not None) shows 60.0.
    assert status.t5s_def == 60.0
    assert status.set_temperature == 60
    # No fault (STANDARD's error_code=1 is a misread); device Off (not running).
    assert status.error_code == 0
    assert status.comp_running is False
    # raw_body preserved.
    assert status.raw_body == bytes(KJRH120L_STATUS_BODY)
    # The bogus fields the STANDARD decode produced must NOT be populated.
    assert status.total_kwh in (None, 0)
    assert status.comp_frq in (None, 0)
    assert status.box_bottom_temp is None
    assert status.tr_temperature is None
    assert status.ptc_temperature is None
    assert status.exv_drg in (None, 0)
    assert status.comp_total_run_hours in (None, 0)
    assert status.pressure_h in (None, 0)
    assert status.pressure_l in (None, 0)


def test_kjrh120l_status_setpoint_tracks_body15():
    """body[15] is THE setpoint: 0x41 → 65 °C in an earlier captured state."""
    body = bytearray(KJRH120L_STATUS_BODY)
    body[15] = 0x41
    status = decode_kjrh120l_status(bytes(body))
    assert status.t5s_def == 65.0
    assert status.set_temperature == 65


def test_kjrh120l_status_power_off_from_body10():
    """OFF capture: body[10] == 0x00 → mode 0 / "Off" (climate hvac OFF)."""
    status = decode_kjrh120l_status(KJRH120L_STATUS_BODY)
    assert status.mode == 0
    assert status.mode_name == "Off"


def test_kjrh120l_status_power_on_from_body10():
    """ON capture: body[10] == 0x01 → non-off heating mode so HA shows it on.

    mode 1 maps to HVACMode.HEAT in climate.py (_QUERY_MODE_TO_HVAC), which is a
    non-off state — this is what makes the climate card read ON and the Off
    button usable. body[15] = 0x41 → 65 °C setpoint in this capture.
    """
    status = decode_kjrh120l_status(KJRH120L_STATUS_BODY_ON)
    assert status.mode == 1
    assert status.mode != 0  # explicitly NOT off
    assert status.mode_name == "Heat"
    assert status.t5s_def == 65.0
    assert status.set_temperature == 65
    # Power-on is not a compressor-running signal; leave it False (none confirmed).
    assert status.comp_running is False
    assert status.error_code == 0


def test_kjrh120l_status_power_byte_is_body10_not_body2():
    """body[2] stays 0x00 in both captures; only body[10] flips with power."""
    assert KJRH120L_STATUS_BODY[2] == 0x00
    assert KJRH120L_STATUS_BODY_ON[2] == 0x00
    assert KJRH120L_STATUS_BODY[10] == 0x00
    assert KJRH120L_STATUS_BODY_ON[10] == 0x01
    assert decode_kjrh120l_status(KJRH120L_STATUS_BODY).mode == 0
    assert decode_kjrh120l_status(KJRH120L_STATUS_BODY_ON).mode == 1


def test_kjrh120l_status_short_frame_is_safe():
    """A frame too short to carry body[15]/body[10] decodes safe, no crash."""
    status = decode_kjrh120l_status(bytes([0x01, 0x00]))
    assert status.error_code == 0
    assert status.t5s_def is None
    # Defaults to Off when the power byte is absent.
    assert status.mode == 0


def test_kjrh120l_temp_range_is_20_to_70():
    """Setpoint clamp widened to the app-allowed 20–70 °C (issue #35)."""
    assert KJRH120L_TEMP_MIN == 20
    assert KJRH120L_TEMP_MAX == 70


def test_kjrh120l_profile_applied_to_status_object():
    """apply_profile_to_status(KJRH120L) re-decodes the frame via the KJRH layout."""
    std = decode_its_status(KJRH120L_STATUS_BODY)
    # Confirm the STANDARD decode misreads this frame (the garbage being fixed).
    assert std.set_temperature == 66
    assert std.t5s_def == -35.0
    assert std.error_code == 1
    assert std.comp_running is True
    assert std.total_kwh == 1340
    assert std.comp_frq == 6425

    kjrh = apply_profile_to_status(ModelProfile.KJRH120L, std)
    assert kjrh.mode_name == "Off"
    assert kjrh.t5s_def == 60.0
    assert kjrh.set_temperature == 60
    assert kjrh.error_code == 0
    assert kjrh.comp_running is False
    assert kjrh.total_kwh in (None, 0)
    assert kjrh.comp_frq in (None, 0)
    assert kjrh.box_bottom_temp is None


def test_kjrh120l_climate_target_temperature_shows_setpoint():
    """climate target_temperature (t5s_def if not None) shows the DHW setpoint."""
    status = decode_kjrh120l_status(KJRH120L_STATUS_BODY)
    target = status.t5s_def if status.t5s_def is not None else float(status.set_temperature)
    assert target == 60.0


def test_kjrh120l_sensors_temps_suppressed():
    """All temperature fields are suppressed (None) — water/outdoor not in API.

    The sensors frame is static/cached and carries no live temps; the STANDARD
    decode would emit a misleading constant -35 for every temp. The KJRH profile
    nulls them so HA shows them unavailable instead of wrong.
    """
    sensors = decode_its_sensors(KJRH120L_SENSORS_BODY)
    status = decode_kjrh120l_status(KJRH120L_STATUS_BODY)
    out = apply_profile_to_sensors(ModelProfile.KJRH120L, sensors, status)

    assert out.t3_temp is None
    assert out.t4_temp is None
    assert out.t2_temp is None
    assert out.t2b_temp is None
    assert out.twin_temp is None
    assert out.twout_temp is None
    assert out.t1_temp is None
    # raw_body preserved.
    assert out.raw_body == sensors.raw_body


# ---------------------------------------------------------------------------
# Aquapura Split Green decode + query commands (sn8 17186T3A)
# ---------------------------------------------------------------------------
#
# Energie Aquapura Split Green HPWH, KJR-86T3 wired controller. Captured from the
# official iLetComfort iOS app (v1.7.0) against eu.dollin.net, in two states:
#   capture A: tank 49 °C, ambient 21 °C   (tank bytes 0x01ec, ambient 0x00d4)
#   capture B: tank 48 °C, ambient 20 °C   (the full frames pinned below)
# Both with the unit OFF and the setpoint at 50 °C. The app truncates rather than
# rounds (48.7 → "48", 20.8 → "20"); we surface the true tenths.
#
# This model echoes the standard long C3 query back verbatim (and the KJRH-120L
# short form too), so the profile has to send the app's 21-byte selector frames
# and read each data group from its own frame — hence the query-command tests
# below as well as the decode tests.

# The query commands captured from the app: (selector, param) → command.
AQUAPURA_SPLIT_GREEN_QUERY_COMMANDS = {
    (0x00, 0x00): "aa14c300000000000003000000ffffffff00ffff2c",  # identity
    (0x00, 0x64): "aa14c300000000000003000064ffffffff00ffffc8",  # config/limits
    (0x01, 0xF4): "aa14c3000000000000030001f4ffffffff00ffff37",  # STATUS
    (0x01, 0x90): "aa14c300000000000003000190ffffffff00ffff9b",  # timers/errors
    (0x02, 0x58): "aa14c300000000000003000258ffffffff00ffffd2",  # schedule A
    (0x02, 0x62): "aa14c300000000000003000262ffffffff00ffffc8",  # schedule B
    (0x03, 0x20): "aa14c300000000000003000320ffffffff00ffff09",  # consumption
    (0x03, 0x84): "aa14c300000000000003000384ffffffff00ffffa5",  # TANK TEMP
    (0x03, 0xE8): "aa14c3000000000000030003e8ffffffff00ffff41",  # ODU sensors
}

# Real STATUS (selector 0x01,0xf4) response frame, complete as captured. The app
# showed setpoint 50 °C → raw idx37 = body[27] = 0x32. This particular capture is
# from an OFF unit (body[13] = raw idx23 = 0x00); see the ON/OFF pair below for
# the power-bit confirmation.
AQUAPURA_SPLIT_GREEN_STATUS_RAW = _bytes(
    "aa,2e,c3,00,00,00,00,00,00,03,00,01,f4,ff,ff,83,ff,00,ff,ff,ff,15,ff,"
    "00,40,02,08,00,ff,ff,01,ff,ff,00,ff,00,00,32,ff,ff,ff,ff,ff,ff,ff,00,15"
)
AQUAPURA_SPLIT_GREEN_STATUS_BODY = AQUAPURA_SPLIT_GREEN_STATUS_RAW[10:-1]

# Real ON/OFF SET request + response pair, captured toggling power in the app
# (mitmproxy). The responses are the STATUS (01,f4) frame; diffing them shows
# body[13] (raw idx23) is the ONLY byte that changes — the power bit.
AQUAPURA_SPLIT_GREEN_POWER_ON_COMMAND = "aa18c3000000000000020001f4ffffffff01ffff000b010126"
AQUAPURA_SPLIT_GREEN_POWER_OFF_COMMAND = "aa18c3000000000000020001f4ffffffff01ffff000b010027"
AQUAPURA_SPLIT_GREEN_STATUS_ON_RAW = _bytes(
    "aa,2e,c3,00,00,00,00,00,00,02,00,01,f4,ff,ff,83,ff,00,ff,ff,ff,15,ff,"
    "01,40,02,08,00,ff,ff,01,ff,ff,00,ff,00,00,32,ff,ff,ff,ff,ff,ff,ff,00,15"
)
AQUAPURA_SPLIT_GREEN_STATUS_OFF_RAW = _bytes(
    "aa,2e,c3,00,00,00,00,00,00,02,00,01,f4,ff,ff,83,ff,00,ff,ff,ff,15,ff,"
    "00,40,02,08,00,ff,ff,01,ff,ff,00,ff,00,00,32,ff,ff,ff,ff,ff,ff,ff,00,16"
)
AQUAPURA_SPLIT_GREEN_STATUS_ON_BODY = AQUAPURA_SPLIT_GREEN_STATUS_ON_RAW[10:-1]
AQUAPURA_SPLIT_GREEN_STATUS_OFF_BODY = AQUAPURA_SPLIT_GREEN_STATUS_OFF_RAW[10:-1]

# Real setpoint-change SET request + response triples, captured changing the
# DHW setpoint 50 -> 51 -> 52 -> 50 °C in the app (mitmproxy). Diffing the
# responses shows body[15:17] (16-bit BE tenths) is the ONLY thing that moves.
AQUAPURA_SPLIT_GREEN_SETPOINT_51_COMMAND = (
    "aa19c3000000000000020001f4ffffffff01ffff000d0201fe24"
)
AQUAPURA_SPLIT_GREEN_SETPOINT_52_COMMAND = (
    "aa19c3000000000000020001f4ffffffff01ffff000d02020819"
)
AQUAPURA_SPLIT_GREEN_SETPOINT_50_COMMAND = (
    "aa19c3000000000000020001f4ffffffff01ffff000d0201f42e"
)
AQUAPURA_SPLIT_GREEN_STATUS_SETPOINT_51_RAW = _bytes(
    "aa,2e,c3,00,00,00,00,00,00,02,00,01,f4,ff,ff,83,ff,00,ff,ff,ff,15,ff,"
    "01,40,01,fe,00,ff,ff,01,ff,ff,00,ff,00,00,32,ff,ff,ff,ff,ff,ff,ff,00,20"
)
AQUAPURA_SPLIT_GREEN_STATUS_SETPOINT_52_RAW = _bytes(
    "aa,2e,c3,00,00,00,00,00,00,02,00,01,f4,ff,ff,83,ff,00,ff,ff,ff,15,ff,"
    "01,40,02,08,00,ff,ff,01,ff,ff,00,ff,00,00,32,ff,ff,ff,ff,ff,ff,ff,00,15"
)
AQUAPURA_SPLIT_GREEN_STATUS_SETPOINT_50_RAW = _bytes(
    "aa,2e,c3,00,00,00,00,00,00,02,00,01,f4,ff,ff,83,ff,00,ff,ff,ff,15,ff,"
    "01,40,01,f4,00,ff,ff,01,ff,ff,00,ff,00,00,32,ff,ff,ff,ff,ff,ff,ff,00,2a"
)
AQUAPURA_SPLIT_GREEN_STATUS_SETPOINT_51_BODY = (
    AQUAPURA_SPLIT_GREEN_STATUS_SETPOINT_51_RAW[10:-1]
)
AQUAPURA_SPLIT_GREEN_STATUS_SETPOINT_52_BODY = (
    AQUAPURA_SPLIT_GREEN_STATUS_SETPOINT_52_RAW[10:-1]
)
AQUAPURA_SPLIT_GREEN_STATUS_SETPOINT_50_BODY = (
    AQUAPURA_SPLIT_GREEN_STATUS_SETPOINT_50_RAW[10:-1]
)

# Real operating-mode-change SET request + response pair, captured switching
# Eco <-> Disparo in the app (mitmproxy). Diffing the responses shows body[14]
# is the ONLY thing that moves — same marker encoding as a schedule slot.
AQUAPURA_SPLIT_GREEN_MODE_DISPARO_COMMAND = (
    "aa18c3000000000000020001f4ffffffff01ffff000c0180a6"
)
AQUAPURA_SPLIT_GREEN_MODE_ECO_COMMAND = (
    "aa18c3000000000000020001f4ffffffff01ffff000c0140e6"
)
AQUAPURA_SPLIT_GREEN_STATUS_MODE_DISPARO_RAW = _bytes(
    "aa,2e,c3,00,00,00,00,00,00,02,00,01,f4,ff,ff,83,ff,00,ff,ff,ff,15,ff,"
    "01,80,02,08,00,ff,ff,01,ff,ff,00,ff,00,00,32,ff,ff,ff,ff,ff,ff,ff,00,d5"
)
AQUAPURA_SPLIT_GREEN_STATUS_MODE_ECO_RAW = _bytes(
    "aa,2e,c3,00,00,00,00,00,00,02,00,01,f4,ff,ff,83,ff,00,ff,ff,ff,15,ff,"
    "01,40,02,08,00,ff,ff,01,ff,ff,00,ff,00,00,32,ff,ff,ff,ff,ff,ff,ff,00,15"
)
AQUAPURA_SPLIT_GREEN_STATUS_MODE_DISPARO_BODY = (
    AQUAPURA_SPLIT_GREEN_STATUS_MODE_DISPARO_RAW[10:-1]
)
AQUAPURA_SPLIT_GREEN_STATUS_MODE_ECO_BODY = (
    AQUAPURA_SPLIT_GREEN_STATUS_MODE_ECO_RAW[10:-1]
)


def test_aquapura_split_green_status_setpoint_from_real_capture_series():
    """body[15:17] tracks 3 real setpoint changes (50->51->52->50) exactly.

    Each response's only diff from its predecessor is body[15:17] — proof this
    field (not body[27], which stays 0x32 throughout) is the live setpoint.
    """
    s51 = decode_aquapura_split_green_status(
        AQUAPURA_SPLIT_GREEN_STATUS_SETPOINT_51_BODY,
    )
    s52 = decode_aquapura_split_green_status(
        AQUAPURA_SPLIT_GREEN_STATUS_SETPOINT_52_BODY,
    )
    s50 = decode_aquapura_split_green_status(
        AQUAPURA_SPLIT_GREEN_STATUS_SETPOINT_50_BODY,
    )

    assert s51.t5s_def == 51.0
    assert s51.set_temperature == 51
    assert s52.t5s_def == 52.0
    assert s52.set_temperature == 52
    assert s50.t5s_def == 50.0
    assert s50.set_temperature == 50

    # body[27] is constant 0x32 (=50) across ALL three, regardless of the real
    # setpoint — confirms it is NOT the field that changed.
    assert AQUAPURA_SPLIT_GREEN_STATUS_SETPOINT_51_BODY[27] == 0x32
    assert AQUAPURA_SPLIT_GREEN_STATUS_SETPOINT_52_BODY[27] == 0x32
    assert AQUAPURA_SPLIT_GREEN_STATUS_SETPOINT_50_BODY[27] == 0x32

    diffs_51_52 = [
        i for i in range(len(AQUAPURA_SPLIT_GREEN_STATUS_SETPOINT_51_BODY))
        if AQUAPURA_SPLIT_GREEN_STATUS_SETPOINT_51_BODY[i]
        != AQUAPURA_SPLIT_GREEN_STATUS_SETPOINT_52_BODY[i]
    ]
    assert diffs_51_52 == [15, 16]


def test_aquapura_split_green_status_operating_mode_from_real_capture_pair():
    """body[14] is confirmed as the operating-mode marker by a real toggle pair.

    Diffing the two responses (captured switching Eco <-> Disparo in the app)
    shows it is the ONLY byte that changes.
    """
    disparo = decode_aquapura_split_green_status(
        AQUAPURA_SPLIT_GREEN_STATUS_MODE_DISPARO_BODY,
    )
    eco = decode_aquapura_split_green_status(
        AQUAPURA_SPLIT_GREEN_STATUS_MODE_ECO_BODY,
    )

    assert AQUAPURA_SPLIT_GREEN_STATUS_MODE_DISPARO_BODY[14] == 0x80
    assert AQUAPURA_SPLIT_GREEN_STATUS_MODE_ECO_BODY[14] == 0x40
    assert disparo.operating_mode == "Disparo"
    assert eco.operating_mode == "Eco"

    diffs = [
        i for i in range(len(AQUAPURA_SPLIT_GREEN_STATUS_MODE_DISPARO_BODY))
        if AQUAPURA_SPLIT_GREEN_STATUS_MODE_DISPARO_BODY[i]
        != AQUAPURA_SPLIT_GREEN_STATUS_MODE_ECO_BODY[i]
    ]
    assert diffs == [14]
    assert disparo.t5s_def == eco.t5s_def
    assert disparo.mode == eco.mode  # power unaffected by operating mode


def test_aquapura_split_green_status_operating_mode_defaults_to_eco():
    """The very first (pre-toggle) capture already reads Eco (marker 0x40)."""
    status = decode_aquapura_split_green_status(AQUAPURA_SPLIT_GREEN_STATUS_BODY)
    assert AQUAPURA_SPLIT_GREEN_STATUS_BODY[14] == 0x40
    assert status.operating_mode == "Eco"


def test_aquapura_split_green_status_operating_mode_unknown_marker():
    """An unrecognised marker reports Unknown(0xNN) rather than guessing."""
    body = bytearray(AQUAPURA_SPLIT_GREEN_STATUS_BODY)
    body[14] = 0x20
    status = decode_aquapura_split_green_status(bytes(body))
    assert status.operating_mode == "Unknown(0x20)"


def test_build_aquapura_split_green_setpoint_command_matches_captured_commands():
    """All 3 captured setpoint writes are reproduced byte-exact."""
    assert (
        build_aquapura_split_green_setpoint_command(51)
        == AQUAPURA_SPLIT_GREEN_SETPOINT_51_COMMAND
    )
    assert (
        build_aquapura_split_green_setpoint_command(52)
        == AQUAPURA_SPLIT_GREEN_SETPOINT_52_COMMAND
    )
    assert (
        build_aquapura_split_green_setpoint_command(50)
        == AQUAPURA_SPLIT_GREEN_SETPOINT_50_COMMAND
    )


def test_build_aquapura_split_green_operating_mode_command_matches_captured_commands():
    """Both captured mode writes are reproduced byte-exact."""
    assert (
        build_aquapura_split_green_operating_mode_command("Disparo")
        == AQUAPURA_SPLIT_GREEN_MODE_DISPARO_COMMAND
    )
    assert (
        build_aquapura_split_green_operating_mode_command("Eco")
        == AQUAPURA_SPLIT_GREEN_MODE_ECO_COMMAND
    )


def test_build_aquapura_split_green_operating_mode_command_rejects_unknown():
    """An unproven mode label must not silently send a guessed marker byte."""
    with pytest.raises(ValueError, match="Unknown Aquapura Split Green operating mode"):
        build_aquapura_split_green_operating_mode_command("Boost")


# Real "Silence" quiet-mode SET request + response pair, captured toggling
# Silence on/off in the app (mitmproxy). Diffing the responses shows body[17]
# is the ONLY thing that moves.
AQUAPURA_SPLIT_GREEN_SILENCE_ON_COMMAND = (
    "aa18c3000000000000020001f4ffffffff01ffff000e010123"
)
AQUAPURA_SPLIT_GREEN_SILENCE_OFF_COMMAND = (
    "aa18c3000000000000020001f4ffffffff01ffff000e010024"
)
AQUAPURA_SPLIT_GREEN_STATUS_SILENCE_ON_RAW = _bytes(
    "aa,2e,c3,00,00,00,00,00,00,02,00,01,f4,ff,ff,83,ff,00,ff,ff,ff,15,ff,"
    "01,40,01,f4,01,ff,ff,01,ff,ff,00,ff,00,00,32,ff,ff,ff,ff,ff,ff,ff,00,29"
)
AQUAPURA_SPLIT_GREEN_STATUS_SILENCE_OFF_RAW = _bytes(
    "aa,2e,c3,00,00,00,00,00,00,02,00,01,f4,ff,ff,83,ff,00,ff,ff,ff,15,ff,"
    "01,40,01,f4,00,ff,ff,01,ff,ff,00,ff,00,00,32,ff,ff,ff,ff,ff,ff,ff,00,2a"
)
AQUAPURA_SPLIT_GREEN_STATUS_SILENCE_ON_BODY = (
    AQUAPURA_SPLIT_GREEN_STATUS_SILENCE_ON_RAW[10:-1]
)
AQUAPURA_SPLIT_GREEN_STATUS_SILENCE_OFF_BODY = (
    AQUAPURA_SPLIT_GREEN_STATUS_SILENCE_OFF_RAW[10:-1]
)


def test_aquapura_split_green_status_silence_from_real_capture_pair():
    """body[17] is confirmed as the Silence toggle by a real ON/OFF capture pair.

    Diffing the two responses (captured switching Silence on/off in the app)
    shows it is the ONLY byte that changes.
    """
    silence_on = decode_aquapura_split_green_status(
        AQUAPURA_SPLIT_GREEN_STATUS_SILENCE_ON_BODY,
    )
    silence_off = decode_aquapura_split_green_status(
        AQUAPURA_SPLIT_GREEN_STATUS_SILENCE_OFF_BODY,
    )

    assert AQUAPURA_SPLIT_GREEN_STATUS_SILENCE_ON_BODY[17] == 0x01
    assert AQUAPURA_SPLIT_GREEN_STATUS_SILENCE_OFF_BODY[17] == 0x00
    assert silence_on.silence is True
    assert silence_off.silence is False

    diffs = [
        i for i in range(len(AQUAPURA_SPLIT_GREEN_STATUS_SILENCE_ON_BODY))
        if AQUAPURA_SPLIT_GREEN_STATUS_SILENCE_ON_BODY[i]
        != AQUAPURA_SPLIT_GREEN_STATUS_SILENCE_OFF_BODY[i]
    ]
    assert diffs == [17]
    assert silence_on.t5s_def == silence_off.t5s_def
    assert silence_on.operating_mode == silence_off.operating_mode
    assert silence_on.mode == silence_off.mode


def test_build_aquapura_split_green_silence_command_matches_captured_commands():
    """Both captured Silence writes are reproduced byte-exact."""
    assert (
        build_aquapura_split_green_silence_command(True)
        == AQUAPURA_SPLIT_GREEN_SILENCE_ON_COMMAND
    )
    assert (
        build_aquapura_split_green_silence_command(False)
        == AQUAPURA_SPLIT_GREEN_SILENCE_OFF_COMMAND
    )

# Real TANK (selector 0x03,0x84) response frame from capture B, complete as
# captured: raw idx40:41 = 0x01e7 = 48.7 °C, app showed 48.
AQUAPURA_SPLIT_GREEN_TANK_RAW = _bytes(
    "aa,3e,c3,00,00,00,00,00,00,03,00,03,84,ff,ff,83,ff,00,ff,ff,ff,15,00,"
    "00,00,00,00,ff,01,00,01,ff,00,00,00,00,00,ff,ff,ff,01,e7,2f,00,00,00,"
    "00,00,00,00,00,00,00,00,ff,ff,ff,ff,ff,ff,ff,ff,d7"
)
AQUAPURA_SPLIT_GREEN_TANK_BODY = AQUAPURA_SPLIT_GREEN_TANK_RAW[10:-1]

# Real ODU (selector 0x03,0xe8) response frame from capture B, complete as
# captured: raw idx59:60 = 0x00d0 = 20.8 °C ambient, app showed 20.
AQUAPURA_SPLIT_GREEN_ODU_RAW = _bytes(
    "aa,eb,c3,00,00,00,00,00,00,03,00,03,e8,ff,ff,83,ff,00,ff,ff,ff,0f,ff,"
    "ff,00,00,00,00,ff,01,e0,ff,ff,ff,ff,00,00,00,00,ff,ff,00,dd,ff,ff,ff,"
    "ff,00,00,ff,ff,ff,ff,01,46,00,fd,00,da,00,d0,ff,ff,ff,ff,ff,ff,7f,ff,"
    "7f,ff,7f,ff,ff,ff,ff,ff,ff,ff,00,00,ff,ff,ff,ff,ff,ff,00,32,00,64,7f,"
    "ff,00,00,00,00,01,7a,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,"
    "00,00,00,02,00,00,00,02,00,00,00,01,ff,ff,ff,ff,00,ff,ff,ff,00,00,00,"
    "00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,"
    "00,00,00,00,00,00,00,35,34,30,4e,41,38,37,30,31,30,31,42,34,31,34,30,"
    "33,30,30,33,37,33,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,"
    "00,00,00,00,00,00,00,00,00,56,31,30,00,00,00,00,00,00,00,00,00,00,00,"
    "00,00,ff,ff,00,00,0f"
)
AQUAPURA_SPLIT_GREEN_ODU_BODY = AQUAPURA_SPLIT_GREEN_ODU_RAW[10:-1]


def _aquapura_split_green_tank_frame(tank_tenths: int) -> bytes:
    """Return the real TANK frame with the temperature bytes overwritten.

    Used to exercise readings we have no capture for (the earlier 49.2 °C, the
    0x7fff sensor-absent sentinel); everything around them stays as captured.
    """
    frame = bytearray(AQUAPURA_SPLIT_GREEN_TANK_RAW)
    frame[40:42] = tank_tenths.to_bytes(2, "big")
    frame[62] = (~sum(frame[1:62]) + 1) & 0xFF
    return bytes(frame)


def test_resolve_profile_aquapura_split_green_sn8():
    """The Aquapura Split Green sn8 (17186T3A) resolves to its own profile."""
    assert AQUAPURA_SPLIT_GREEN_SN8 == "17186T3A"
    assert resolve_profile(AQUAPURA_SPLIT_GREEN_SN8) is ModelProfile.AQUAPURA_SPLIT_GREEN


@pytest.mark.parametrize("selector,param", list(AQUAPURA_SPLIT_GREEN_QUERY_COMMANDS))
def test_build_aquapura_split_green_query_matches_captured_commands(selector, param):
    """Every captured app command is reproduced byte-for-byte (checksum included)."""
    expected = AQUAPURA_SPLIT_GREEN_QUERY_COMMANDS[(selector, param)]
    assert build_aquapura_split_green_query(selector, param) == expected


def test_build_query_command_aquapura_split_green_uses_app_selector_frames():
    """Split Green status/sensors fetches send the app's selector frames.

    Subtype 0x01 (status) → selector 01,f4; subtype 0x02 (sensors) → the tank
    frame 03,84, because on this model the water temperature lives under
    selector 0x03 rather than the standard sensors subtype.
    """
    status_cmd = build_query_command(ModelProfile.AQUAPURA_SPLIT_GREEN, 0x01)
    sensors_cmd = build_query_command(ModelProfile.AQUAPURA_SPLIT_GREEN, 0x02)

    assert status_cmd == AQUAPURA_SPLIT_GREEN_QUERY_COMMANDS[(0x01, 0xF4)]
    assert sensors_cmd == AQUAPURA_SPLIT_GREEN_QUERY_COMMANDS[(0x03, 0x84)]
    # Explicitly NOT the standard frames (which this model echoes back verbatim)
    # nor the KJRH-120L short form.
    assert status_cmd != build_c3_query(0x01)
    assert sensors_cmd != build_c3_query(0x02)
    assert status_cmd != "ffff010101ff"


def test_aquapura_split_green_status_decodes_confirmed_setpoint():
    """body[15:17] is the live DHW setpoint, 16-bit BE tenths of °C (0x0208 = 52.0).

    NOT body[27] (0x32 = 50): that byte is constant across every capture in
    this fixture set, including ones where the real setpoint moved — see the
    module notes' "wrong turn" writeup. body[15:17], by contrast, is proven by
    the 50->51->52->50 capture series to track the live value exactly.
    """
    status = decode_aquapura_split_green_status(AQUAPURA_SPLIT_GREEN_STATUS_BODY)

    assert AQUAPURA_SPLIT_GREEN_STATUS_BODY[27] == 0x32  # constant, NOT the setpoint
    # Surfaced via t5s_def so the climate target_temperature reads it.
    assert status.t5s_def == 52.0
    assert status.set_temperature == 52
    assert status.error_code == 0
    assert status.raw_body == bytes(AQUAPURA_SPLIT_GREEN_STATUS_BODY)


def test_aquapura_split_green_status_setpoint_tracks_body15_17():
    """The setpoint follows body[15:17] rather than being pinned to one capture."""
    body = bytearray(AQUAPURA_SPLIT_GREEN_STATUS_BODY)
    body[15:17] = (550).to_bytes(2, "big")
    status = decode_aquapura_split_green_status(bytes(body))
    assert status.t5s_def == 55.0
    assert status.set_temperature == 55


def test_aquapura_split_green_status_power_off_from_body13():
    """The baseline (OFF) capture decodes to mode 0/"Off"."""
    status = decode_aquapura_split_green_status(AQUAPURA_SPLIT_GREEN_STATUS_BODY)
    assert status.mode == 0
    assert status.mode_name == "Off"
    assert status.comp_running is False


def test_aquapura_split_green_status_power_on_off_from_real_capture_pair():
    """body[13] (raw idx23) is confirmed as the power bit by a real ON/OFF pair.

    Diffing the two response frames (captured toggling power in the app) shows
    it is the ONLY byte that changes — the four originally-suspected candidate
    bytes (body[14], body[15], body[16], body[20]) are identical in both and
    are NOT the power bit. (body[14] turned out to be the operating-mode
    marker and body[15:17] the live setpoint — both confirmed by separate
    capture pairs below — while power is isolated to body[13] alone here.)
    """
    on_status = decode_aquapura_split_green_status(
        AQUAPURA_SPLIT_GREEN_STATUS_ON_BODY,
    )
    off_status = decode_aquapura_split_green_status(
        AQUAPURA_SPLIT_GREEN_STATUS_OFF_BODY,
    )

    assert AQUAPURA_SPLIT_GREEN_STATUS_ON_BODY[13] == 0x01
    assert AQUAPURA_SPLIT_GREEN_STATUS_OFF_BODY[13] == 0x00
    assert on_status.mode == 1
    assert on_status.mode_name == "Heat"
    assert off_status.mode == 0
    assert off_status.mode_name == "Off"

    # The two response bodies differ ONLY at body[13] — the setpoint and every
    # other byte the decode reads is untouched by the power state.
    diffs = [
        i for i in range(len(AQUAPURA_SPLIT_GREEN_STATUS_ON_BODY))
        if AQUAPURA_SPLIT_GREEN_STATUS_ON_BODY[i]
        != AQUAPURA_SPLIT_GREEN_STATUS_OFF_BODY[i]
    ]
    assert diffs == [13]
    assert on_status.t5s_def == off_status.t5s_def


def test_aquapura_split_green_status_power_byte_is_body13():
    """Sanity check pinning the exact index (regression against re-indexing)."""
    assert AQUAPURA_SPLIT_GREEN_STATUS_ON_BODY[13] == 0x01
    assert AQUAPURA_SPLIT_GREEN_STATUS_OFF_BODY[13] == 0x00


def test_aquapura_split_green_status_suppresses_standard_garbage():
    """None of the STANDARD decode's misreads survive the Split Green decode."""
    status = decode_aquapura_split_green_status(AQUAPURA_SPLIT_GREEN_STATUS_BODY)
    assert status.box_bottom_temp is None
    assert status.tr_temperature is None
    assert status.ptc_temperature is None
    assert status.total_kwh in (None, 0)
    assert status.comp_frq in (None, 0)
    assert status.comp_total_run_hours in (None, 0)


def test_aquapura_split_green_status_short_frame_is_safe():
    """A frame too short to carry body[27] decodes safe, no crash."""
    status = decode_aquapura_split_green_status(bytes([0x00, 0x01, 0xF4]))
    assert status.t5s_def is None
    assert status.set_temperature == 0
    assert status.error_code == 0


def test_aquapura_split_green_profile_applied_to_status_object():
    """apply_profile_to_status re-decodes the frame via the Split Green layout."""
    std = decode_its_status(AQUAPURA_SPLIT_GREEN_STATUS_BODY)
    # Confirm the STANDARD decode misreads this frame (the garbage being fixed).
    assert std.mode == 244
    assert std.mode_name == "Unknown(244)"
    assert std.set_temperature == 131
    assert std.t5s_def == 220.0
    assert std.error_code == 255

    split_green = apply_profile_to_status(ModelProfile.AQUAPURA_SPLIT_GREEN, std)
    assert split_green.mode == 0
    assert split_green.mode_name == "Off"
    assert split_green.t5s_def == 52.0
    assert split_green.set_temperature == 52
    assert split_green.error_code == 0


def test_aquapura_split_green_tank_temp_is_16bit_tenths():
    """body[30:32] = 0x01e7 → 48.7 °C (capture B; the app truncated it to 48)."""
    assert decode_aquapura_split_green_tank_temp(AQUAPURA_SPLIT_GREEN_TANK_BODY) == 48.7
    # Tracks the bytes, not one capture: 0x0226 → 55.0 °C.
    warmer = _aquapura_split_green_tank_frame(0x0226)[10:-1]
    assert decode_aquapura_split_green_tank_temp(warmer) == 55.0


def test_aquapura_split_green_tank_frame_is_live_not_cached():
    """The tank bytes moved between captures, tracking the real tank.

    Capture A read 0x01ec (49.2 °C, app 49) and capture B 0x01e7 (48.7 °C, app
    48) at the same offset — so the 03,84 frame is live and worth polling, not a
    static/cached frame like the KJRH-120L's sensors response.
    """
    capture_a = _aquapura_split_green_tank_frame(0x01EC)[10:-1]
    assert decode_aquapura_split_green_tank_temp(capture_a) == 49.2
    assert decode_aquapura_split_green_tank_temp(AQUAPURA_SPLIT_GREEN_TANK_BODY) == 48.7


def test_aquapura_split_green_ambient_temp_from_odu_frame():
    """ODU body[49:51] = 0x00d0 → 20.8 °C (capture B; the app showed 20).

    Pinned by two captures plus the app's truncation rule: capture A read 0x00d4
    (21.2 °C, app 21). Under truncation this is the only 16-bit pair in the whole
    frame consistent with both readings — its neighbours are 32.6/25.3/21.8 °C.
    """
    assert decode_aquapura_split_green_ambient_temp(
        AQUAPURA_SPLIT_GREEN_ODU_BODY
    ) == 20.8


def test_aquapura_split_green_ambient_temp_missing_frame_is_none():
    """No ODU frame (best-effort fetch failed) → no ambient, not a crash."""
    assert decode_aquapura_split_green_ambient_temp(None) is None
    assert decode_aquapura_split_green_ambient_temp(b"") is None
    assert decode_aquapura_split_green_ambient_temp(b"\xff" * 60) is None


# Second real ODU (selector 0x03,0xe8) capture, unrelated poll (ambient 24.8
# °C, from a later debug-log capture). Its body is 3 bytes LONGER than
# AQUAPURA_SPLIT_GREEN_ODU_RAW's (an earlier count-like field reads 0x03
# here vs 0x02 there) which shifts everything after it, including the ODU
# serial number, by 3 bytes -- this is exactly why the serial is decoded by
# scanning for the ASCII run, not a fixed byte offset (see
# decode_aquapura_split_green_odu_serial's module notes).
AQUAPURA_SPLIT_GREEN_ODU_RAW_2 = _bytes(
    "aa,eb,c3,00,00,00,00,00,00,03,00,03,e8,ff,ff,83,ff,00,ff,ff,ff,0f,ff,"
    "ff,00,00,00,00,ff,01,e0,ff,ff,ff,ff,00,00,00,00,ff,ff,00,e7,ff,ff,ff,"
    "ff,00,00,ff,ff,ff,ff,01,6b,01,35,01,01,00,f8,ff,ff,ff,ff,ff,ff,7f,ff,"
    "7f,ff,7f,ff,ff,ff,ff,ff,ff,ff,00,00,ff,ff,ff,ff,ff,ff,00,32,00,64,7f,"
    "ff,00,00,00,00,01,e9,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,"
    "00,00,00,03,00,00,00,03,00,00,00,01,ff,ff,ff,ff,00,ff,ff,ff,00,00,00,"
    "00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,"
    "00,00,00,00,00,00,00,00,00,35,34,30,4e,41,38,37,30,31,30,31,42,34,31,"
    "34,30,33,30,30,33,37,33,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,"
    "00,00,00,00,00,00,00,00,00,00,00,56,31,30,00,00,00,00,00,00,00,00,00,"
    "00,00,00,00,ff,ff,00,00,e6"
)
AQUAPURA_SPLIT_GREEN_ODU_BODY_2 = AQUAPURA_SPLIT_GREEN_ODU_RAW_2[10:-1]


def test_aquapura_split_green_odu_serial_from_real_captures():
    """The ODU serial number decodes to the app's exact "ODU SN" string from
    BOTH real captures, even though it sits at a DIFFERENT byte offset in
    each (157 vs 154) -- proof the scan-based decode (not a fixed index) is
    the right approach, since a hardcoded offset would misread one of them.
    """
    assert len(AQUAPURA_SPLIT_GREEN_ODU_BODY) != len(AQUAPURA_SPLIT_GREEN_ODU_BODY_2)
    assert (
        decode_aquapura_split_green_odu_serial(AQUAPURA_SPLIT_GREEN_ODU_BODY)
        == "540NA870101B4140300373"
    )
    assert (
        decode_aquapura_split_green_odu_serial(AQUAPURA_SPLIT_GREEN_ODU_BODY_2)
        == "540NA870101B4140300373"
    )


def test_aquapura_split_green_odu_serial_missing_frame_is_none():
    """No ODU frame, or no run long enough to be a serial → None, not a crash."""
    assert decode_aquapura_split_green_odu_serial(None) is None
    assert decode_aquapura_split_green_odu_serial(b"") is None
    assert decode_aquapura_split_green_odu_serial(b"\xff" * 60) is None
    assert decode_aquapura_split_green_odu_serial(b"ABC123") is None  # too short


def test_aquapura_split_green_tank_temp_absent_or_implausible_is_none():
    """0x7fff (sensor absent), padding and short frames all decode to None."""
    absent = _aquapura_split_green_tank_frame(0x7FFF)[10:-1]
    assert decode_aquapura_split_green_tank_temp(absent) is None
    # An all-0xff stub frame would decode to 6553.5 °C — report nothing instead.
    assert decode_aquapura_split_green_tank_temp(b"\xff" * 40) is None
    assert decode_aquapura_split_green_tank_temp(b"\x00\x01\xf4") is None


def test_aquapura_split_green_sensors_merge_tank_and_odu():
    """The two frames merge into tank → th_temp and ambient → t4_temp.

    Everything else is nulled: nothing else in these frames is validated against
    the app, so those entities stay unavailable rather than showing a guess.
    """
    out = decode_aquapura_split_green_sensors(
        AQUAPURA_SPLIT_GREEN_TANK_BODY, AQUAPURA_SPLIT_GREEN_ODU_BODY,
    )

    assert out.th_temp == 48.7
    assert out.t4_temp == 20.8
    assert out.odu_serial == "540NA870101B4140300373"
    assert out.t3_temp is None
    assert out.t2_temp is None
    assert out.t2b_temp is None
    assert out.twin_temp is None
    assert out.twout_temp is None
    assert out.t1_temp is None
    assert out.tf_temp is None
    assert out.tp_temp is None
    # The tank frame is kept as raw_body (diagnostics / SENSORS RAW log).
    assert out.raw_body == bytes(AQUAPURA_SPLIT_GREEN_TANK_BODY)


def test_aquapura_split_green_sensors_without_odu_keep_tank_temp():
    """A failed ODU fetch costs the ambient only — the tank temp still arrives."""
    out = decode_aquapura_split_green_sensors(AQUAPURA_SPLIT_GREEN_TANK_BODY)
    assert out.th_temp == 48.7
    assert out.t4_temp is None


def test_apply_profile_to_sensors_preserves_aquapura_split_green_ambient():
    """The coordinator's profile pass must not clobber the ODU-sourced ambient.

    apply_profile_to_sensors re-derives th_temp from raw_body (the tank frame),
    which cannot carry the ambient — so t4_temp has to survive untouched.
    """
    sensors = decode_aquapura_split_green_sensors(
        AQUAPURA_SPLIT_GREEN_TANK_BODY, AQUAPURA_SPLIT_GREEN_ODU_BODY,
    )
    status = decode_aquapura_split_green_status(AQUAPURA_SPLIT_GREEN_STATUS_BODY)
    out = apply_profile_to_sensors(ModelProfile.AQUAPURA_SPLIT_GREEN, sensors, status)

    assert out.t4_temp == 20.8
    assert out.th_temp == 48.7
    assert out.twin_temp is None


def test_aquapura_split_green_standard_sensor_decode_is_suppressed():
    """A STANDARD-decoded tank frame still gets scrubbed by the profile pass.

    Guards the cached/legacy path: whatever decode_its_sensors invents from the
    tank frame must not reach HA.
    """
    sensors = decode_its_sensors(bytearray(AQUAPURA_SPLIT_GREEN_TANK_BODY))
    status = decode_aquapura_split_green_status(AQUAPURA_SPLIT_GREEN_STATUS_BODY)
    out = apply_profile_to_sensors(ModelProfile.AQUAPURA_SPLIT_GREEN, sensors, status)

    assert out.th_temp == 48.7
    assert out.t3_temp is None
    assert out.twin_temp is None
    assert out.twout_temp is None
    assert out.tf_temp is None
    assert out.tp_temp is None


def test_query_status_aquapura_split_green_sends_selector_frame_and_decodes():
    """query_status with the Split Green sn8 sends the 01,f4 frame and decodes it."""
    client = _make_client()
    with patch_send(client, AQUAPURA_SPLIT_GREEN_STATUS_RAW.hex()) as send:
        status = client.query_status("APPL1", sn8=AQUAPURA_SPLIT_GREEN_SN8)

    send.assert_called_once_with(
        "APPL1", AQUAPURA_SPLIT_GREEN_QUERY_COMMANDS[(0x01, 0xF4)],
    )
    assert status.t5s_def == 52.0
    assert status.set_temperature == 52
    assert status.error_code == 0


def test_query_sensors_aquapura_split_green_fetches_tank_and_odu_frames():
    """One sensors poll sends both 03,84 (tank) and 03,e8 (ODU/ambient)."""
    client = _make_client()
    responses = {
        AQUAPURA_SPLIT_GREEN_QUERY_COMMANDS[(0x03, 0x84)]:
            AQUAPURA_SPLIT_GREEN_TANK_RAW.hex(),
        AQUAPURA_SPLIT_GREEN_QUERY_COMMANDS[(0x03, 0xE8)]:
            AQUAPURA_SPLIT_GREEN_ODU_RAW.hex(),
    }
    with patch_send_per_command(client, responses) as send:
        sensors = client.query_sensors("APPL1", sn8=AQUAPURA_SPLIT_GREEN_SN8)

    sent = [call.args[1] for call in send.call_args_list]
    assert sent == [
        AQUAPURA_SPLIT_GREEN_QUERY_COMMANDS[(0x03, 0x84)],
        AQUAPURA_SPLIT_GREEN_QUERY_COMMANDS[(0x03, 0xE8)],
    ]
    assert sensors.th_temp == 48.7
    assert sensors.t4_temp == 20.8


def test_query_sensors_aquapura_split_green_survives_odu_failure():
    """The ODU frame is best-effort: its failure must not cost the tank temp.

    Losing the tank temperature would blank the climate entity's current
    temperature and push the coordinator onto cached data — too high a price for
    a missing ambient reading.
    """
    client = _make_client()

    def _send(_appliance_code, command):
        if command == AQUAPURA_SPLIT_GREEN_QUERY_COMMANDS[(0x03, 0x84)]:
            return AQUAPURA_SPLIT_GREEN_TANK_RAW.hex()
        raise ApiError("Send command failed: code=1214")

    with patch_send_callable(client, _send):
        sensors = client.query_sensors("APPL1", sn8=AQUAPURA_SPLIT_GREEN_SN8)

    assert sensors.th_temp == 48.7
    assert sensors.t4_temp is None


def test_query_sensors_aquapura_split_green_skips_odu_when_diagnostics_disabled():
    """fetch_diagnostics=False must skip the ODU call entirely, not just
    tolerate its failure -- this is the options-flow "reduce API calls" path,
    distinct from the best-effort failure path above.
    """
    client = _make_client()
    with patch_send(client, AQUAPURA_SPLIT_GREEN_TANK_RAW.hex()) as send:
        sensors = client.query_sensors(
            "APPL1", sn8=AQUAPURA_SPLIT_GREEN_SN8, fetch_diagnostics=False,
        )

    send.assert_called_once_with(
        "APPL1", AQUAPURA_SPLIT_GREEN_QUERY_COMMANDS[(0x03, 0x84)],
    )
    assert sensors.th_temp == 48.7
    assert sensors.t4_temp is None


def test_query_sensors_non_aquapura_split_green_ignores_fetch_diagnostics():
    """fetch_diagnostics is a no-op for every other profile: their sensors
    query is already a single command regardless of this flag.
    """
    client = _make_client()
    with patch_send(client, _c3_frame(ISSUE_11_SENSORS_BODY)) as send:
        sensors_true = client.query_sensors(
            "APPL1", sn8=None, fetch_diagnostics=True,
        )
    with patch_send(client, _c3_frame(ISSUE_11_SENSORS_BODY)) as send_false:
        sensors_false = client.query_sensors(
            "APPL1", sn8=None, fetch_diagnostics=False,
        )

    send.assert_called_once()
    send_false.assert_called_once()
    assert sensors_true.twin_temp == sensors_false.twin_temp


# Real DAILY SCHEDULE (selector 0x02,0x58, "Tempor. diário") response frame,
# complete as captured. Validated byte-for-byte against a live app screenshot
# showing all 4 "Temporiz." slots:
#   1: ON,  ECO, 09:00~21:00, 50°C   2: OFF, ECO, 14:00~18:00, 60°C
#   3: OFF, ECO, 20:00~23:00, 60°C   4: OFF, ECO, 00:00~07:00, 60°C
AQUAPURA_SPLIT_GREEN_SCHEDULE_RAW = _bytes(
    "aa,57,c3,00,00,00,00,00,00,03,00,02,58,ff,ff,83,ff,00,ff,ff,ff,15,ff,"
    "ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,00,1a,08,19,1a,08,"
    "1f,ff,01,0e,00,02,8a,07,04,01,40,01,f4,09,00,15,00,00,40,02,58,0e,00,"
    "12,00,00,40,02,58,14,00,17,00,00,40,02,58,00,00,07,00,74"
)
AQUAPURA_SPLIT_GREEN_SCHEDULE_BODY = AQUAPURA_SPLIT_GREEN_SCHEDULE_RAW[10:-1]

# Second real capture, taken after switching Temporiz. 1's mode in the app
# from "ECO" to "Disparo" (everything else left as-is). Diffed against
# AQUAPURA_SPLIT_GREEN_SCHEDULE_RAW above: the ONLY byte that changed in this
# 88-byte frame is slot 1's mode marker, raw idx56 = body[46], 0x40 -> 0x80 —
# proof this one byte (and nothing else) encodes the mode. (raw[9] also read
# 0x02 here instead of the usual 0x03; unexplained, unconsumed by the decoder.)
AQUAPURA_SPLIT_GREEN_SCHEDULE_DISPARO_RAW = _bytes(
    "aa,57,c3,00,00,00,00,00,00,02,00,02,58,ff,ff,83,ff,00,ff,ff,ff,15,ff,"
    "ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,00,1a,08,19,1a,08,"
    "1f,ff,01,0e,00,02,8a,07,04,01,80,01,f4,09,00,15,00,00,40,02,58,0e,00,"
    "12,00,00,40,02,58,14,00,17,00,00,40,02,58,00,00,07,00,35"
)
AQUAPURA_SPLIT_GREEN_SCHEDULE_DISPARO_BODY = (
    AQUAPURA_SPLIT_GREEN_SCHEDULE_DISPARO_RAW[10:-1]
)


def test_aquapura_split_green_daily_schedule_matches_app_screenshot():
    """All 4 slots decode to exactly what the "Tempor. diário" screen showed."""
    slots = decode_aquapura_split_green_daily_schedule(
        AQUAPURA_SPLIT_GREEN_SCHEDULE_BODY,
    )

    assert len(slots) == AQUAPURA_SPLIT_GREEN_SCHEDULE_SLOT_COUNT
    assert slots[0] == AquapuraSplitGreenScheduleSlot(
        active=True, mode="Eco", setpoint=50.0,
        start_time="09:00", end_time="21:00",
    )
    assert slots[1] == AquapuraSplitGreenScheduleSlot(
        active=False, mode="Eco", setpoint=60.0,
        start_time="14:00", end_time="18:00",
    )
    assert slots[2] == AquapuraSplitGreenScheduleSlot(
        active=False, mode="Eco", setpoint=60.0,
        start_time="20:00", end_time="23:00",
    )
    assert slots[3] == AquapuraSplitGreenScheduleSlot(
        active=False, mode="Eco", setpoint=60.0,
        start_time="00:00", end_time="07:00",
    )


def test_aquapura_split_green_daily_schedule_disabled_slots_keep_their_config():
    """A disabled slot still surfaces its setpoint/time — the app shows it too.

    Temporiz. 2/3/4 are OFF on the screenshot, but the app still displays
    "ECO | 14:00~18:00 | 60°C" etc. for them, because the config bytes are
    present in the frame regardless of the active flag.
    """
    slots = decode_aquapura_split_green_daily_schedule(
        AQUAPURA_SPLIT_GREEN_SCHEDULE_BODY,
    )
    assert slots[1].active is False
    assert slots[1].setpoint == 60.0
    assert slots[1].start_time == "14:00"
    assert slots[1].end_time == "18:00"


def test_aquapura_split_green_daily_schedule_disparo_mode():
    """A real capture with Temporiz. 1 switched to "Disparo" decodes marker 0x80.

    Only the mode changed in the app — this checks the other 3 slots (still
    ECO) and slot 1's active/setpoint/times are untouched, so the marker byte
    really is isolated to the mode.
    """
    slots = decode_aquapura_split_green_daily_schedule(
        AQUAPURA_SPLIT_GREEN_SCHEDULE_DISPARO_BODY,
    )

    assert slots[0] == AquapuraSplitGreenScheduleSlot(
        active=True, mode="Disparo", setpoint=50.0,
        start_time="09:00", end_time="21:00",
    )
    assert slots[1].mode == "Eco"
    assert slots[2].mode == "Eco"
    assert slots[3].mode == "Eco"


def test_aquapura_split_green_daily_schedule_short_frame_is_safe():
    """A frame too short to carry any slot decodes to 4 all-default slots."""
    slots = decode_aquapura_split_green_daily_schedule(bytes([0x00, 0x02, 0x58]))
    assert len(slots) == AQUAPURA_SPLIT_GREEN_SCHEDULE_SLOT_COUNT
    assert all(s == AquapuraSplitGreenScheduleSlot() for s in slots)
    assert all(s.active is False and s.setpoint is None for s in slots)


def test_aquapura_split_green_daily_schedule_unknown_mode_marker():
    """An unrecognised mode marker reports Unknown(0xNN) rather than "Eco"."""
    body = bytearray(AQUAPURA_SPLIT_GREEN_SCHEDULE_BODY)
    body[46] = 0x20  # slot 1's marker byte
    slots = decode_aquapura_split_green_daily_schedule(bytes(body))
    assert slots[0].mode == "Unknown(0x20)"


def test_build_query_command_aquapura_split_green_schedule_selector():
    """The 02,58 selector command matches the app's captured schedule fetch."""
    expected = AQUAPURA_SPLIT_GREEN_QUERY_COMMANDS[(0x02, 0x58)]
    assert build_aquapura_split_green_query(0x02, 0x58) == expected


def test_query_daily_schedule_aquapura_split_green_sends_schedule_frame():
    """query_daily_schedule with the Split Green sn8 sends 02,58 and decodes it."""
    client = _make_client()
    with patch_send(client, AQUAPURA_SPLIT_GREEN_SCHEDULE_RAW.hex()) as send:
        slots = client.query_daily_schedule("APPL1", sn8=AQUAPURA_SPLIT_GREEN_SN8)

    send.assert_called_once_with(
        "APPL1", AQUAPURA_SPLIT_GREEN_QUERY_COMMANDS[(0x02, 0x58)],
    )
    assert slots[0].active is True
    assert slots[0].setpoint == 50.0


def test_query_daily_schedule_other_profiles_send_no_command():
    """Devices without this frame (unknown sn8, or another profile) cost

    nothing: no command is sent and an empty list is returned, since only the
    Aquapura Split Green has a "Tempor. diário" frame to fetch.
    """
    client = _make_client()
    with patch_send(client, "should never be used") as send:
        assert client.query_daily_schedule("APPL1", sn8=None) == []
        assert client.query_daily_schedule("APPL1", sn8=ATW_SN8) == []
        assert client.query_daily_schedule("APPL1", sn8=KJRH120L_SN8) == []

    send.assert_not_called()


# Real schedule-slot activate/deactivate captures. Slot 1 (field 0x2a): both
# ON and OFF captured, diffing shows body[45] as the only byte that changes.
# Slot 2 (field 0x31): only ON captured; the response ALSO shows the slot's
# start/end hour changed (a separate, deliberate time edit made in the same
# app session, not part of this write) -- so only field id 0x31 and its
# body[53] effect are trusted here, not the time bytes.
AQUAPURA_SPLIT_GREEN_SCHEDULE_1_OFF_COMMAND = (
    "aa18c300000000000002000258ffffffff01ffff002a0100a3"
)
AQUAPURA_SPLIT_GREEN_SCHEDULE_1_ON_COMMAND = (
    "aa18c300000000000002000258ffffffff01ffff002a0101a2"
)
AQUAPURA_SPLIT_GREEN_SCHEDULE_2_ON_COMMAND = (
    "aa18c300000000000002000258ffffffff01ffff003101019b"
)
AQUAPURA_SPLIT_GREEN_SCHEDULE_1_OFF_RAW = _bytes(
    "aa,57,c3,00,00,00,00,00,00,02,00,02,58,ff,ff,83,ff,00,ff,ff,ff,15,ff,"
    "ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,00,1a,08,19,1a,08,"
    "1f,ff,01,0e,00,02,8a,07,04,00,40,01,f4,0b,00,13,00,00,40,02,58,0e,00,"
    "12,00,00,40,02,58,14,00,17,00,00,40,02,58,00,00,07,00,76"
)
AQUAPURA_SPLIT_GREEN_SCHEDULE_1_ON_RAW = _bytes(
    "aa,57,c3,00,00,00,00,00,00,02,00,02,58,ff,ff,83,ff,00,ff,ff,ff,15,ff,"
    "ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,00,1a,08,19,1a,08,"
    "1f,ff,01,0e,00,02,8a,07,04,01,40,01,f4,0b,00,13,00,00,40,02,58,0e,00,"
    "12,00,00,40,02,58,14,00,17,00,00,40,02,58,00,00,07,00,75"
)
AQUAPURA_SPLIT_GREEN_SCHEDULE_1_OFF_BODY = AQUAPURA_SPLIT_GREEN_SCHEDULE_1_OFF_RAW[10:-1]
AQUAPURA_SPLIT_GREEN_SCHEDULE_1_ON_BODY = AQUAPURA_SPLIT_GREEN_SCHEDULE_1_ON_RAW[10:-1]


def test_aquapura_split_green_schedule_slot1_active_from_real_capture_pair():
    """body[45] is confirmed as slot 1's active byte by a real ON/OFF pair --
    matches decode_aquapura_split_green_daily_schedule's existing offset.
    """
    diffs = [
        i for i in range(len(AQUAPURA_SPLIT_GREEN_SCHEDULE_1_OFF_BODY))
        if AQUAPURA_SPLIT_GREEN_SCHEDULE_1_OFF_BODY[i]
        != AQUAPURA_SPLIT_GREEN_SCHEDULE_1_ON_BODY[i]
    ]
    assert diffs == [45]
    assert AQUAPURA_SPLIT_GREEN_SCHEDULE_1_OFF_BODY[45] == 0x00
    assert AQUAPURA_SPLIT_GREEN_SCHEDULE_1_ON_BODY[45] == 0x01


def test_build_aquapura_split_green_schedule_active_command_matches_captured_commands():
    """Confirmed slots (1, 2) reproduce the real captures byte-exact."""
    assert (
        build_aquapura_split_green_schedule_active_command(1, False)
        == AQUAPURA_SPLIT_GREEN_SCHEDULE_1_OFF_COMMAND
    )
    assert (
        build_aquapura_split_green_schedule_active_command(1, True)
        == AQUAPURA_SPLIT_GREEN_SCHEDULE_1_ON_COMMAND
    )
    assert (
        build_aquapura_split_green_schedule_active_command(2, True)
        == AQUAPURA_SPLIT_GREEN_SCHEDULE_2_ON_COMMAND
    )


def test_build_aquapura_split_green_schedule_active_command_extrapolated_slots():
    """Slots 3-4 are an informed extrapolation (+7 field id per slot, not a
    real capture) -- pinned here so a future real capture that contradicts
    this table fails a test instead of silently drifting.
    """
    assert (
        build_aquapura_split_green_schedule_active_command(3, True)
        == "aa18c300000000000002000258ffffffff01ffff0038010194"
    )
    assert (
        build_aquapura_split_green_schedule_active_command(4, True)
        == "aa18c300000000000002000258ffffffff01ffff003f01018d"
    )


def test_build_aquapura_split_green_schedule_active_command_rejects_bad_slot():
    with pytest.raises(ValueError, match="Unknown Aquapura Split Green schedule slot"):
        build_aquapura_split_green_schedule_active_command(5, True)
    with pytest.raises(ValueError, match="Unknown Aquapura Split Green schedule slot"):
        build_aquapura_split_green_schedule_active_command(0, True)


def test_set_schedule_active_sends_captured_command():
    """set_schedule_active sends the real captured write command."""
    client = _make_client()
    with patch_send(client, AQUAPURA_SPLIT_GREEN_SCHEDULE_1_ON_RAW.hex()) as send:
        result = client.set_schedule_active("APPL1", slot=1, enabled=True)
    send.assert_called_once_with(
        "APPL1", AQUAPURA_SPLIT_GREEN_SCHEDULE_1_ON_COMMAND,
    )
    assert result["effective_slot"] == 1
    assert result["effective_enabled"] is True


# Real disinfection ("Desinfecção") capture series, all in the same 02,58
# frame as the daily schedule (selector shared, body[38:44] is a separate
# region from the schedule slots at body[45+]). The app showed ON, 65°C,
# 14:00, every 7 days when this series started. Each step below changed
# EXACTLY the field named and nothing else — confirmed by diffing.
AQUAPURA_SPLIT_GREEN_DISINFECTION_OFF_RAW = _bytes(
    "aa,57,c3,00,00,00,00,00,00,02,00,02,58,ff,ff,83,ff,00,ff,ff,ff,15,ff,"
    "ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,00,1a,08,19,1a,08,"
    "1f,ff,00,0e,00,02,8a,07,04,01,40,01,f4,0b,00,15,00,00,40,02,58,0e,00,"
    "12,00,00,40,02,58,14,00,17,00,00,40,02,58,00,00,07,00,74"
)
AQUAPURA_SPLIT_GREEN_DISINFECTION_ON_RAW = _bytes(
    "aa,57,c3,00,00,00,00,00,00,02,00,02,58,ff,ff,83,ff,00,ff,ff,ff,15,ff,"
    "ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,00,1a,08,19,1a,08,"
    "1f,ff,01,0e,00,02,8a,07,04,01,40,01,f4,0b,00,15,00,00,40,02,58,0e,00,"
    "12,00,00,40,02,58,14,00,17,00,00,40,02,58,00,00,07,00,73"
)
AQUAPURA_SPLIT_GREEN_DISINFECTION_TEMP68_RAW = _bytes(
    "aa,57,c3,00,00,00,00,00,00,02,00,02,58,ff,ff,83,ff,00,ff,ff,ff,15,ff,"
    "ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,00,1a,08,19,1a,08,"
    "1f,ff,01,0e,00,02,a8,07,04,01,40,01,f4,0b,00,15,00,00,40,02,58,0e,00,"
    "12,00,00,40,02,58,14,00,17,00,00,40,02,58,00,00,07,00,55"
)
AQUAPURA_SPLIT_GREEN_DISINFECTION_HOUR13_RAW = _bytes(
    "aa,57,c3,00,00,00,00,00,00,02,00,02,58,ff,ff,83,ff,00,ff,ff,ff,15,ff,"
    "ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,00,1a,08,19,1a,08,"
    "1f,ff,01,0d,00,02,a8,07,04,01,40,01,f4,0b,00,15,00,00,40,02,58,0e,00,"
    "12,00,00,40,02,58,14,00,17,00,00,40,02,58,00,00,07,00,56"
)
AQUAPURA_SPLIT_GREEN_DISINFECTION_CYCLE6_RAW = _bytes(
    "aa,57,c3,00,00,00,00,00,00,02,00,02,58,ff,ff,83,ff,00,ff,ff,ff,15,ff,"
    "ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,00,1a,08,19,1a,08,"
    "1f,ff,01,0d,00,02,a8,06,04,01,40,01,f4,0b,00,15,00,00,40,02,58,0e,00,"
    "12,00,00,40,02,58,14,00,17,00,00,40,02,58,00,00,07,00,57"
)
AQUAPURA_SPLIT_GREEN_DISINFECTION_OFF_BODY = (
    AQUAPURA_SPLIT_GREEN_DISINFECTION_OFF_RAW[10:-1]
)
AQUAPURA_SPLIT_GREEN_DISINFECTION_ON_BODY = (
    AQUAPURA_SPLIT_GREEN_DISINFECTION_ON_RAW[10:-1]
)
AQUAPURA_SPLIT_GREEN_DISINFECTION_TEMP68_BODY = (
    AQUAPURA_SPLIT_GREEN_DISINFECTION_TEMP68_RAW[10:-1]
)
AQUAPURA_SPLIT_GREEN_DISINFECTION_HOUR13_BODY = (
    AQUAPURA_SPLIT_GREEN_DISINFECTION_HOUR13_RAW[10:-1]
)
AQUAPURA_SPLIT_GREEN_DISINFECTION_CYCLE6_BODY = (
    AQUAPURA_SPLIT_GREEN_DISINFECTION_CYCLE6_RAW[10:-1]
)

AQUAPURA_SPLIT_GREEN_DISINFECTION_OFF_COMMAND = (
    "aa29c300000000000002000258ffffffff01ffff"
    "002401000025010e00260100002702028a0028010758"
)
AQUAPURA_SPLIT_GREEN_DISINFECTION_ON_COMMAND = (
    "aa29c300000000000002000258ffffffff01ffff"
    "002401010025010e00260100002702028a0028010757"
)
AQUAPURA_SPLIT_GREEN_DISINFECTION_TEMP68_COMMAND = (
    "aa29c300000000000002000258ffffffff01ffff"
    "002401010025010e0026010000270202a80028010739"
)
AQUAPURA_SPLIT_GREEN_DISINFECTION_HOUR13_COMMAND = (
    "aa29c300000000000002000258ffffffff01ffff"
    "002401010025010d0026010000270202a8002801073a"
)
AQUAPURA_SPLIT_GREEN_DISINFECTION_CYCLE6_COMMAND = (
    "aa29c300000000000002000258ffffffff01ffff"
    "002401010025010d0026010000270202a8002801063b"
)


def test_aquapura_split_green_disinfection_from_real_capture_series():
    """Each step in the capture series changes EXACTLY one decoded field."""
    off = decode_aquapura_split_green_disinfection(
        AQUAPURA_SPLIT_GREEN_DISINFECTION_OFF_BODY,
    )
    on = decode_aquapura_split_green_disinfection(
        AQUAPURA_SPLIT_GREEN_DISINFECTION_ON_BODY,
    )
    temp68 = decode_aquapura_split_green_disinfection(
        AQUAPURA_SPLIT_GREEN_DISINFECTION_TEMP68_BODY,
    )
    hour13 = decode_aquapura_split_green_disinfection(
        AQUAPURA_SPLIT_GREEN_DISINFECTION_HOUR13_BODY,
    )
    cycle6 = decode_aquapura_split_green_disinfection(
        AQUAPURA_SPLIT_GREEN_DISINFECTION_CYCLE6_BODY,
    )

    assert off == AquapuraSplitGreenDisinfectionSettings(
        enabled=False, hour=14, minute=0, temperature=65.0, cycle_days=7,
    )
    assert on == AquapuraSplitGreenDisinfectionSettings(
        enabled=True, hour=14, minute=0, temperature=65.0, cycle_days=7,
    )
    assert temp68 == AquapuraSplitGreenDisinfectionSettings(
        enabled=True, hour=14, minute=0, temperature=68.0, cycle_days=7,
    )
    assert hour13 == AquapuraSplitGreenDisinfectionSettings(
        enabled=True, hour=13, minute=0, temperature=68.0, cycle_days=7,
    )
    assert cycle6 == AquapuraSplitGreenDisinfectionSettings(
        enabled=True, hour=13, minute=0, temperature=68.0, cycle_days=6,
    )

    # off -> on: only body[38] (enable) differs.
    diffs = [
        i for i in range(len(AQUAPURA_SPLIT_GREEN_DISINFECTION_OFF_BODY))
        if AQUAPURA_SPLIT_GREEN_DISINFECTION_OFF_BODY[i]
        != AQUAPURA_SPLIT_GREEN_DISINFECTION_ON_BODY[i]
    ]
    assert diffs == [38]


def test_aquapura_split_green_disinfection_short_frame_returns_none():
    """A frame too short to carry the disinfection block decodes to None
    rather than a stub of defaults that could be mistaken for "disabled".
    """
    assert decode_aquapura_split_green_disinfection(bytes([0x00, 0x02, 0x58])) is None


def test_build_aquapura_split_green_disinfection_command_matches_captured_commands():
    """All 5 captured disinfection writes are reproduced byte-exact."""
    assert (
        build_aquapura_split_green_disinfection_command(
            enabled=False, hour=14, minute=0, temp_c=65, cycle_days=7,
        )
        == AQUAPURA_SPLIT_GREEN_DISINFECTION_OFF_COMMAND
    )
    assert (
        build_aquapura_split_green_disinfection_command(
            enabled=True, hour=14, minute=0, temp_c=65, cycle_days=7,
        )
        == AQUAPURA_SPLIT_GREEN_DISINFECTION_ON_COMMAND
    )
    assert (
        build_aquapura_split_green_disinfection_command(
            enabled=True, hour=14, minute=0, temp_c=68, cycle_days=7,
        )
        == AQUAPURA_SPLIT_GREEN_DISINFECTION_TEMP68_COMMAND
    )
    assert (
        build_aquapura_split_green_disinfection_command(
            enabled=True, hour=13, minute=0, temp_c=68, cycle_days=7,
        )
        == AQUAPURA_SPLIT_GREEN_DISINFECTION_HOUR13_COMMAND
    )
    assert (
        build_aquapura_split_green_disinfection_command(
            enabled=True, hour=13, minute=0, temp_c=68, cycle_days=6,
        )
        == AQUAPURA_SPLIT_GREEN_DISINFECTION_CYCLE6_COMMAND
    )


def test_query_disinfection_aquapura_split_green_sends_schedule_frame():
    """query_disinfection with the Split Green sn8 sends 02,58 and decodes it."""
    client = _make_client()
    with patch_send(client, AQUAPURA_SPLIT_GREEN_DISINFECTION_ON_RAW.hex()) as send:
        settings = client.query_disinfection("APPL1", sn8=AQUAPURA_SPLIT_GREEN_SN8)

    send.assert_called_once_with(
        "APPL1", AQUAPURA_SPLIT_GREEN_QUERY_COMMANDS[(0x02, 0x58)],
    )
    assert settings == AquapuraSplitGreenDisinfectionSettings(
        enabled=True, hour=14, minute=0, temperature=65.0, cycle_days=7,
    )


def test_query_disinfection_other_profiles_send_no_command():
    """Devices without this frame cost nothing: no command sent, None returned."""
    client = _make_client()
    with patch_send(client, "should never be used") as send:
        assert client.query_disinfection("APPL1", sn8=None) is None
        assert client.query_disinfection("APPL1", sn8=ATW_SN8) is None
        assert client.query_disinfection("APPL1", sn8=KJRH120L_SN8) is None

    send.assert_not_called()


def test_set_disinfection_sends_captured_command():
    """set_disinfection sends the real captured multi-field write command."""
    client = _make_client()
    with patch_send(client, AQUAPURA_SPLIT_GREEN_DISINFECTION_ON_RAW.hex()) as send:
        result = client.set_disinfection(
            "APPL1", enabled=True, hour=14, minute=0, temp_c=65, cycle_days=7,
        )
    send.assert_called_once_with(
        "APPL1", AQUAPURA_SPLIT_GREEN_DISINFECTION_ON_COMMAND,
    )
    assert result["effective_enabled"] is True


# Real heating-element ON/OFF SET request + response pair, captured toggling
# it in the app (mitmproxy). Lives in the TIMERS (01,90) frame -- a different
# selector from every other single-field write (all on STATUS, 01,f4).
AQUAPURA_SPLIT_GREEN_HEATING_ELEMENT_ON_COMMAND = (
    "aa18c300000000000002000190ffffffff01ffff000e010187"
)
AQUAPURA_SPLIT_GREEN_HEATING_ELEMENT_OFF_COMMAND = (
    "aa18c300000000000002000190ffffffff01ffff000e010088"
)
AQUAPURA_SPLIT_GREEN_HEATING_ELEMENT_ON_RAW = _bytes(
    "aa,52,c3,00,00,00,00,00,00,02,00,01,90,ff,ff,83,ff,00,ff,ff,ff,15,ff,"
    "30,30,30,30,30,30,30,30,30,30,30,30,30,30,30,00,00,00,00,00,30,30,30,"
    "30,30,30,30,30,30,30,30,30,30,30,30,00,00,00,00,00,00,01,00,02,09,01,"
    "6a,8f,3d,84,00,03,01,00,00,09,0f,00,00,44"
)
AQUAPURA_SPLIT_GREEN_HEATING_ELEMENT_OFF_RAW = _bytes(
    "aa,52,c3,00,00,00,00,00,00,02,00,01,90,ff,ff,83,ff,00,ff,ff,ff,15,ff,"
    "30,30,30,30,30,30,30,30,30,30,30,30,30,30,30,00,00,00,00,00,30,30,30,"
    "30,30,30,30,30,30,30,30,30,30,30,30,00,00,00,00,00,00,00,00,02,09,01,"
    "6a,8f,3d,96,00,03,01,00,00,09,0f,00,00,33"
)
AQUAPURA_SPLIT_GREEN_HEATING_ELEMENT_ON_BODY = (
    AQUAPURA_SPLIT_GREEN_HEATING_ELEMENT_ON_RAW[10:-1]
)
AQUAPURA_SPLIT_GREEN_HEATING_ELEMENT_OFF_BODY = (
    AQUAPURA_SPLIT_GREEN_HEATING_ELEMENT_OFF_RAW[10:-1]
)


def test_aquapura_split_green_heating_element_from_real_capture_pair():
    """body[54] of the TIMERS (01,90) frame is confirmed as the heating
    element toggle by a real ON/OFF capture pair.
    """
    on = decode_aquapura_split_green_heating_element(
        AQUAPURA_SPLIT_GREEN_HEATING_ELEMENT_ON_BODY,
    )
    off = decode_aquapura_split_green_heating_element(
        AQUAPURA_SPLIT_GREEN_HEATING_ELEMENT_OFF_BODY,
    )

    assert AQUAPURA_SPLIT_GREEN_HEATING_ELEMENT_ON_BODY[54] == 0x01
    assert AQUAPURA_SPLIT_GREEN_HEATING_ELEMENT_OFF_BODY[54] == 0x00
    assert on is True
    assert off is False

    # body[62] also differs between the two captures (a monotonically
    # increasing counter, unrelated to the toggle) -- confirm body[54] is
    # still the only byte that changes when that known-noisy byte is excluded.
    diffs = [
        i for i in range(len(AQUAPURA_SPLIT_GREEN_HEATING_ELEMENT_ON_BODY))
        if AQUAPURA_SPLIT_GREEN_HEATING_ELEMENT_ON_BODY[i]
        != AQUAPURA_SPLIT_GREEN_HEATING_ELEMENT_OFF_BODY[i]
        and i != 62
    ]
    assert diffs == [54]


def test_aquapura_split_green_heating_element_short_frame_returns_none():
    """A frame too short to carry the heating element bit decodes to None."""
    assert decode_aquapura_split_green_heating_element(bytes([0x00, 0x01, 0x90])) is None


def test_build_aquapura_split_green_heating_element_command_matches_captured_commands():
    """Both captured heating-element writes are reproduced byte-exact."""
    assert (
        build_aquapura_split_green_heating_element_command(True)
        == AQUAPURA_SPLIT_GREEN_HEATING_ELEMENT_ON_COMMAND
    )
    assert (
        build_aquapura_split_green_heating_element_command(False)
        == AQUAPURA_SPLIT_GREEN_HEATING_ELEMENT_OFF_COMMAND
    )


def test_query_heating_element_aquapura_split_green_sends_timers_frame():
    """query_heating_element with the Split Green sn8 sends 01,90 and decodes it."""
    client = _make_client()
    with patch_send(
        client, AQUAPURA_SPLIT_GREEN_HEATING_ELEMENT_ON_RAW.hex(),
    ) as send:
        result = client.query_heating_element("APPL1", sn8=AQUAPURA_SPLIT_GREEN_SN8)

    send.assert_called_once_with(
        "APPL1", AQUAPURA_SPLIT_GREEN_QUERY_COMMANDS[(0x01, 0x90)],
    )
    assert result is True


def test_query_heating_element_other_profiles_send_no_command():
    """Devices without this frame cost nothing: no command sent, None returned."""
    client = _make_client()
    with patch_send(client, "should never be used") as send:
        assert client.query_heating_element("APPL1", sn8=None) is None
        assert client.query_heating_element("APPL1", sn8=ATW_SN8) is None
        assert client.query_heating_element("APPL1", sn8=KJRH120L_SN8) is None

    send.assert_not_called()


def test_set_heating_element_sends_captured_command():
    """set_heating_element sends the real captured write command."""
    client = _make_client()
    with patch_send(
        client, AQUAPURA_SPLIT_GREEN_HEATING_ELEMENT_ON_RAW.hex(),
    ) as send:
        result = client.set_heating_element("APPL1", enabled=True)
    send.assert_called_once_with(
        "APPL1", AQUAPURA_SPLIT_GREEN_HEATING_ELEMENT_ON_COMMAND,
    )
    assert result["effective_enabled"] is True


# Real "Force Disinfection" ON/OFF SET request + response pair, captured
# toggling it in the app (mitmproxy). Same TIMERS (01,90) frame as the
# heating element, body[55] instead of body[54].
AQUAPURA_SPLIT_GREEN_FORCE_DISINFECTION_ON_COMMAND = (
    "aa18c300000000000002000190ffffffff01ffff000f010186"
)
AQUAPURA_SPLIT_GREEN_FORCE_DISINFECTION_OFF_COMMAND = (
    "aa18c300000000000002000190ffffffff01ffff000f010087"
)
AQUAPURA_SPLIT_GREEN_FORCE_DISINFECTION_ON_RAW = _bytes(
    "aa,52,c3,00,00,00,00,00,00,02,00,01,90,ff,ff,83,ff,00,ff,ff,ff,15,ff,"
    "30,30,30,30,30,30,30,30,30,30,30,30,30,30,30,00,00,00,00,00,30,30,30,"
    "30,30,30,30,30,30,30,30,30,30,30,30,00,00,00,00,00,00,00,01,02,09,01,"
    "6a,8f,48,f5,00,03,01,00,00,09,0f,00,00,c8"
)
AQUAPURA_SPLIT_GREEN_FORCE_DISINFECTION_OFF_RAW = _bytes(
    "aa,52,c3,00,00,00,00,00,00,02,00,01,90,ff,ff,83,ff,00,ff,ff,ff,15,ff,"
    "30,30,30,30,30,30,30,30,30,30,30,30,30,30,30,00,00,00,00,00,30,30,30,"
    "30,30,30,30,30,30,30,30,30,30,30,30,00,00,00,00,00,00,00,00,02,09,01,"
    "6a,8f,48,f7,00,03,01,00,00,09,0f,00,00,c7"
)
AQUAPURA_SPLIT_GREEN_FORCE_DISINFECTION_ON_BODY = (
    AQUAPURA_SPLIT_GREEN_FORCE_DISINFECTION_ON_RAW[10:-1]
)
AQUAPURA_SPLIT_GREEN_FORCE_DISINFECTION_OFF_BODY = (
    AQUAPURA_SPLIT_GREEN_FORCE_DISINFECTION_OFF_RAW[10:-1]
)


def test_aquapura_split_green_force_disinfection_from_real_capture_pair():
    """body[55] of the TIMERS (01,90) frame is confirmed as the Force
    Disinfection toggle by a real ON/OFF capture pair.
    """
    on = decode_aquapura_split_green_force_disinfection(
        AQUAPURA_SPLIT_GREEN_FORCE_DISINFECTION_ON_BODY,
    )
    off = decode_aquapura_split_green_force_disinfection(
        AQUAPURA_SPLIT_GREEN_FORCE_DISINFECTION_OFF_BODY,
    )

    assert AQUAPURA_SPLIT_GREEN_FORCE_DISINFECTION_ON_BODY[55] == 0x01
    assert AQUAPURA_SPLIT_GREEN_FORCE_DISINFECTION_OFF_BODY[55] == 0x00
    assert on is True
    assert off is False

    # body[62] also differs (the same noisy counter as the heating element
    # captures) -- confirm body[55] is still the only OTHER byte that changes.
    diffs = [
        i for i in range(len(AQUAPURA_SPLIT_GREEN_FORCE_DISINFECTION_ON_BODY))
        if AQUAPURA_SPLIT_GREEN_FORCE_DISINFECTION_ON_BODY[i]
        != AQUAPURA_SPLIT_GREEN_FORCE_DISINFECTION_OFF_BODY[i]
        and i != 62
    ]
    assert diffs == [55]


def test_build_aquapura_split_green_force_disinfection_command_matches_captured_commands():
    """Both captured Force Disinfection writes are reproduced byte-exact."""
    assert (
        build_aquapura_split_green_force_disinfection_command(True)
        == AQUAPURA_SPLIT_GREEN_FORCE_DISINFECTION_ON_COMMAND
    )
    assert (
        build_aquapura_split_green_force_disinfection_command(False)
        == AQUAPURA_SPLIT_GREEN_FORCE_DISINFECTION_OFF_COMMAND
    )


def test_query_force_disinfection_aquapura_split_green_sends_timers_frame():
    """query_force_disinfection with the Split Green sn8 sends 01,90 and decodes it."""
    client = _make_client()
    with patch_send(
        client, AQUAPURA_SPLIT_GREEN_FORCE_DISINFECTION_ON_RAW.hex(),
    ) as send:
        result = client.query_force_disinfection("APPL1", sn8=AQUAPURA_SPLIT_GREEN_SN8)

    send.assert_called_once_with(
        "APPL1", AQUAPURA_SPLIT_GREEN_QUERY_COMMANDS[(0x01, 0x90)],
    )
    assert result is True


def test_query_force_disinfection_other_profiles_send_no_command():
    """Devices without this frame cost nothing: no command sent, None returned."""
    client = _make_client()
    with patch_send(client, "should never be used") as send:
        assert client.query_force_disinfection("APPL1", sn8=None) is None
        assert client.query_force_disinfection("APPL1", sn8=ATW_SN8) is None
        assert client.query_force_disinfection("APPL1", sn8=KJRH120L_SN8) is None

    send.assert_not_called()


def test_set_force_disinfection_sends_captured_command():
    """set_force_disinfection sends the real captured write command."""
    client = _make_client()
    with patch_send(
        client, AQUAPURA_SPLIT_GREEN_FORCE_DISINFECTION_ON_RAW.hex(),
    ) as send:
        result = client.set_force_disinfection("APPL1", enabled=True)
    send.assert_called_once_with(
        "APPL1", AQUAPURA_SPLIT_GREEN_FORCE_DISINFECTION_ON_COMMAND,
    )
    assert result["effective_enabled"] is True


# Real CONSUMPTION (selector 0x03,0x20) response frame, captured opening the
# app's Consumption page. The screen showed day=1.12, week=month=year=
# total=4.90 kWh -- matched byte-for-byte below.
AQUAPURA_SPLIT_GREEN_CONSUMPTION_RAW = _bytes(
    "aa,81,c3,00,00,00,00,00,00,03,00,03,20,ff,ff,83,ff,00,ff,ff,ff,00,00,"
    "00,00,70,00,00,01,ea,00,00,01,ea,00,00,01,ea,00,00,01,ea,00,00,01,e9,"
    "00,00,00,00,00,00,01,e9,00,00,00,00,0a,07,ea,ff,ff,ff,ff,ff,ff,ff,ff,"
    "ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,"
    "ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,00,00,27,10,00,00,00,"
    "00,00,00,00,00,04,31,00,00,00,00,00,00,f2"
)
AQUAPURA_SPLIT_GREEN_CONSUMPTION_BODY = AQUAPURA_SPLIT_GREEN_CONSUMPTION_RAW[10:-1]


def test_aquapura_split_green_consumption_matches_app_screenshot():
    """day/week/month/year/total decode to exactly what the Consumption
    page showed: day 1.12 kWh, the rest 4.90 kWh.
    """
    consumption = decode_aquapura_split_green_consumption(
        AQUAPURA_SPLIT_GREEN_CONSUMPTION_BODY,
    )
    assert consumption == AquapuraSplitGreenConsumption(
        day=1.12, week=4.90, month=4.90, year=4.90, total=4.90,
    )


def test_aquapura_split_green_consumption_short_frame_returns_none():
    """A frame too short to carry all 5 counters decodes to None."""
    assert decode_aquapura_split_green_consumption(bytes([0x00, 0x03, 0x20])) is None


def test_query_consumption_aquapura_split_green_sends_consumption_frame():
    """query_consumption with the Split Green sn8 sends 03,20 and decodes it."""
    client = _make_client()
    with patch_send(client, AQUAPURA_SPLIT_GREEN_CONSUMPTION_RAW.hex()) as send:
        result = client.query_consumption("APPL1", sn8=AQUAPURA_SPLIT_GREEN_SN8)

    send.assert_called_once_with(
        "APPL1", AQUAPURA_SPLIT_GREEN_QUERY_COMMANDS[(0x03, 0x20)],
    )
    assert result == AquapuraSplitGreenConsumption(
        day=1.12, week=4.90, month=4.90, year=4.90, total=4.90,
    )


def test_query_consumption_other_profiles_send_no_command():
    """Devices without this frame cost nothing: no command sent, None returned."""
    client = _make_client()
    with patch_send(client, "should never be used") as send:
        assert client.query_consumption("APPL1", sn8=None) is None
        assert client.query_consumption("APPL1", sn8=ATW_SN8) is None
        assert client.query_consumption("APPL1", sn8=KJRH120L_SN8) is None

    send.assert_not_called()


def test_aquapura_split_green_set_device_sends_captured_setpoint_command():
    """set_device sends the real captured setpoint-write command, not a guess.

    climate.async_set_temperature calls async_set_device(temperature=...), so
    this mirrors that call shape.
    """
    client = _make_client()

    with patch_send(client, AQUAPURA_SPLIT_GREEN_STATUS_SETPOINT_51_RAW.hex()) as send:
        result = client.set_device(
            "APPL1", sn8=AQUAPURA_SPLIT_GREEN_SN8, temperature=51,
        )
    send.assert_called_once_with(
        "APPL1", AQUAPURA_SPLIT_GREEN_SETPOINT_51_COMMAND,
    )
    assert result["effective_temp"] == 51


def test_aquapura_split_green_set_device_sends_captured_operating_mode_command():
    """set_device sends the real captured Eco/Disparo write command.

    A future select entity calls async_set_device(operating_mode=...); this
    mirrors that call shape and confirms it takes priority over a same-call
    temperature/power request (mirroring _set_device_kjrh120l's priority
    rule), since the app only ever writes one field per action.
    """
    client = _make_client()

    with patch_send(client, AQUAPURA_SPLIT_GREEN_STATUS_MODE_DISPARO_RAW.hex()) as send:
        result = client.set_device(
            "APPL1", sn8=AQUAPURA_SPLIT_GREEN_SN8, operating_mode="Disparo",
        )
    send.assert_called_once_with(
        "APPL1", AQUAPURA_SPLIT_GREEN_MODE_DISPARO_COMMAND,
    )
    assert result["effective_operating_mode"] == "Disparo"

    with patch_send(client, AQUAPURA_SPLIT_GREEN_STATUS_MODE_ECO_RAW.hex()) as send:
        result = client.set_device(
            "APPL1", sn8=AQUAPURA_SPLIT_GREEN_SN8,
            operating_mode="Eco", temperature=51, power_on=True,
        )
    send.assert_called_once_with(
        "APPL1", AQUAPURA_SPLIT_GREEN_MODE_ECO_COMMAND,
    )
    assert result["effective_operating_mode"] == "Eco"


def test_aquapura_split_green_set_device_sends_captured_silence_command():
    """set_device sends the real captured Silence write command.

    A future switch entity calls async_set_device(silence=...); this mirrors
    that call shape and confirms it takes priority over a same-call
    operating_mode/temperature/power request, since the app only ever writes
    one field per action.
    """
    client = _make_client()

    with patch_send(client, AQUAPURA_SPLIT_GREEN_STATUS_SILENCE_ON_RAW.hex()) as send:
        result = client.set_device(
            "APPL1", sn8=AQUAPURA_SPLIT_GREEN_SN8, silence=True,
        )
    send.assert_called_once_with(
        "APPL1", AQUAPURA_SPLIT_GREEN_SILENCE_ON_COMMAND,
    )
    assert result["effective_silence"] is True

    with patch_send(client, AQUAPURA_SPLIT_GREEN_STATUS_SILENCE_OFF_RAW.hex()) as send:
        result = client.set_device(
            "APPL1", sn8=AQUAPURA_SPLIT_GREEN_SN8,
            silence=False, operating_mode="Eco", temperature=51, power_on=True,
        )
    send.assert_called_once_with(
        "APPL1", AQUAPURA_SPLIT_GREEN_SILENCE_OFF_COMMAND,
    )
    assert result["effective_silence"] is False


def test_aquapura_split_green_set_device_sends_captured_power_command():
    """set_device sends the real captured ON/OFF command, not a guess.

    mode != MODE_OFF (climate.async_set_hvac_mode) and power_on=True
    (climate.async_turn_on) must both resolve to the ON command; mode ==
    MODE_OFF (climate.async_turn_off) resolves to the OFF command — mirroring
    how climate.py actually calls async_set_device for this profile.
    """
    client = _make_client()

    with patch_send(client, AQUAPURA_SPLIT_GREEN_STATUS_ON_RAW.hex()) as send:
        result = client.set_device(
            "APPL1", sn8=AQUAPURA_SPLIT_GREEN_SN8, power_on=True,
        )
    send.assert_called_once_with(
        "APPL1", AQUAPURA_SPLIT_GREEN_POWER_ON_COMMAND,
    )
    assert result["effective_power"] is True

    with patch_send(client, AQUAPURA_SPLIT_GREEN_STATUS_ON_RAW.hex()) as send:
        client.set_device("APPL1", sn8=AQUAPURA_SPLIT_GREEN_SN8, mode=MODE_HEAT)
    send.assert_called_once_with(
        "APPL1", AQUAPURA_SPLIT_GREEN_POWER_ON_COMMAND,
    )

    with patch_send(client, AQUAPURA_SPLIT_GREEN_STATUS_OFF_RAW.hex()) as send:
        result = client.set_device(
            "APPL1", sn8=AQUAPURA_SPLIT_GREEN_SN8, mode=MODE_OFF,
        )
    send.assert_called_once_with(
        "APPL1", AQUAPURA_SPLIT_GREEN_POWER_OFF_COMMAND,
    )
    assert result["effective_power"] is False


def test_build_aquapura_split_green_power_command_matches_captured_commands():
    """Both captured commands are reproduced byte-exact, checksum included."""
    assert (
        build_aquapura_split_green_power_command(True)
        == AQUAPURA_SPLIT_GREEN_POWER_ON_COMMAND
    )
    assert (
        build_aquapura_split_green_power_command(False)
        == AQUAPURA_SPLIT_GREEN_POWER_OFF_COMMAND
    )


# ---------------------------------------------------------------------------
# STANDARD regression — unknown / None sn8 must decode exactly as before
# ---------------------------------------------------------------------------

# Reuse the real MSC-70D2N8-A frames (issue #11) already pinned in test_api.py.
ISSUE_11_STATUS_BODY = _bytes(
    "01,01,01,42,4b,2d,3f,37,0f,23,37,25,28,c0,00,00,80,23,2d,3f,37,00,"
    "23,37,00,3f,00,00,00,00,00,00,01,c2,00,00,01,00,0a,dc,04,e2,00,33,"
    "00,00,00,16,00,00,01"
)
ISSUE_11_SENSORS_BODY = _bytes(
    "02,01,00,00,00,00,00,00,00,00,00,00,01,02,00,29,49,12,00,01,00,33,"
    "3e,41,69,40,42,ef,04,00,eb,03,31,37,0c,31,37,0c,31,67,23,00,00,01,"
    "72,00,00,00,00,00,00"
)


def test_standard_profile_status_is_byte_identical_to_legacy():
    """STANDARD profile leaves decode_its_status output unchanged (regression)."""
    legacy = decode_its_status(ISSUE_11_STATUS_BODY)
    out = apply_profile_to_status(ModelProfile.STANDARD, legacy)
    assert out is legacy  # no copy, no mutation for the default path


@pytest.mark.parametrize("sn8", [None, "", "UNKNOWN8", "00000000"])
def test_standard_profile_sensors_unchanged_for_unknown_sn8(sn8):
    """An unknown/None sn8 resolves to STANDARD and does not touch the sensors."""
    profile = resolve_profile(sn8)
    status = decode_its_status(ISSUE_11_STATUS_BODY)
    sensors = decode_its_sensors(ISSUE_11_SENSORS_BODY)
    out = apply_profile_to_sensors(profile, sensors, status)
    assert out is sensors
    assert out.twin_temp == sensors.twin_temp


def test_query_status_standard_path_unchanged_with_unknown_sn8():
    """query_status with an unknown sn8 returns the identical legacy decode."""
    client = _make_client()
    with patch_send(client, _c3_frame(ISSUE_11_STATUS_BODY)):
        std = client.query_status("APPL1")
        with_sn8 = client.query_status("APPL1", sn8="UNKNOWN8")

    assert with_sn8.set_temperature == std.set_temperature
    assert with_sn8.error_code == std.error_code
    assert with_sn8.comp_running == std.comp_running


def test_query_status_atw_routes_through_atw_layout():
    """query_status with the ATW sn8 applies the ATW status layout."""
    client = _make_client()
    with patch_send(client, _c3_frame(ATW_FRAMES["state1"])):
        status = client.query_status("APPL1", sn8=ATW_SN8)

    assert status.set_temperature == 50
    assert status.t5s_def == 21.0
    assert status.box_bottom_temp == 46.0
    assert status.error_code == 0
    assert not status.comp_running


# Small context-manager helper to patch send_hex_command without importing
# unittest.mock at module top (keeps the import block focused on the SUT).
def patch_send(client, return_value):
    from unittest.mock import patch

    return patch.object(client, "send_hex_command", return_value=return_value)


def patch_send_per_command(client, responses: dict[str, str]):
    """Patch send_hex_command to answer each command hex with its own response."""
    return patch_send_callable(
        client, lambda _appliance_code, command: responses[command],
    )


def patch_send_callable(client, side_effect):
    from unittest.mock import patch

    return patch.object(client, "send_hex_command", side_effect=side_effect)
