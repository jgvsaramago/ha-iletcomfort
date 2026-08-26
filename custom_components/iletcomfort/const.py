"""Constants for the iLetComfort integration."""

DOMAIN = "iletcomfort"
PLATFORMS = ["climate", "sensor", "switch", "select", "binary_sensor"]

CONF_APPLIANCE_CODE = "appliance_code"
CONF_REGION = "region"

REGION_US = "us"
REGION_EU = "eu"
REGION_URLS = {
    REGION_US: "https://us.dollin.net",
    REGION_EU: "https://eu.dollin.net",
}
DEFAULT_REGION = REGION_US

DEFAULT_SCAN_INTERVAL = 60
# Sane floor so the options flow can't be used to hammer the vendor cloud.
MIN_SCAN_INTERVAL = 30

# Per-poll fetch toggles (options flow). Both only affect the Aquapura Split
# Green profile (sn8 17186T3A): every other profile's status/sensors poll is
# already the minimum two commands regardless of these settings.
CONF_FETCH_DIAGNOSTICS = "fetch_diagnostics"
CONF_FETCH_SCHEDULE = "fetch_schedule"
DEFAULT_FETCH_DIAGNOSTICS = True
DEFAULT_FETCH_SCHEDULE = True
