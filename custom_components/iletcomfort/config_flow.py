"""Config flow for the iLetComfort integration."""

from __future__ import annotations

import logging
from typing import Any

import requests
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, CONF_SCAN_INTERVAL
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import ApiError, AuthError, ILetComfortClient
from .const import (
    CONF_APPLIANCE_CODE,
    CONF_FETCH_DIAGNOSTICS,
    CONF_FETCH_SCHEDULE,
    CONF_REGION,
    DEFAULT_FETCH_DIAGNOSTICS,
    DEFAULT_FETCH_SCHEDULE,
    DEFAULT_REGION,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
    REGION_EU,
    REGION_URLS,
    REGION_US,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_REGION, default=DEFAULT_REGION): SelectSelector(
            SelectSelectorConfig(
                options=[REGION_US, REGION_EU],
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="region",
            )
        ),
    }
)


def _appliance_label(appliance: dict[str, Any]) -> str:
    """Build a user-facing label for an appliance entry."""
    for key in ("applianceName", "nickname", "name", "applianceCode"):
        value = appliance.get(key)
        if value:
            return str(value)
    return "Unknown device"


class ILetComfortConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for iLetComfort."""

    VERSION = 2

    def __init__(self) -> None:
        self._email: str | None = None
        self._password: str | None = None
        self._region: str | None = None
        self._appliances: list[dict[str, Any]] = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> ILetComfortOptionsFlow:
        """Create the options flow (poll interval, per-poll fetch toggles)."""
        return ILetComfortOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle the credentials + region step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL]
            password = user_input[CONF_PASSWORD]
            region = user_input[CONF_REGION]
            api_base = REGION_URLS.get(region, REGION_URLS[DEFAULT_REGION])

            client = ILetComfortClient(api_base=api_base)
            appliances: list[dict[str, Any]] | None = None

            try:
                await self.hass.async_add_executor_job(
                    client.login, email, password,
                )
            except AuthError as err:
                _LOGGER.warning("Auth error during login: %s", err)
                errors["base"] = "invalid_auth"
            except requests.exceptions.RequestException as err:
                _LOGGER.warning("Network error during login: %s", err)
                errors["base"] = "cannot_connect"
            except ApiError as err:
                _LOGGER.warning("API error during login: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during login")
                errors["base"] = "unknown"

            if not errors:
                try:
                    appliances = await self.hass.async_add_executor_job(
                        client.list_appliances,
                    )
                except requests.exceptions.RequestException as err:
                    _LOGGER.warning("Network error during device discovery: %s", err)
                    errors["base"] = "cannot_connect"
                except ApiError as err:
                    _LOGGER.warning("API error during device discovery: %s", err)
                    errors["base"] = "cannot_connect"
                except Exception:
                    _LOGGER.exception("Unexpected error during device discovery")
                    errors["base"] = "unknown"

            if not errors:
                if not appliances:
                    errors["base"] = "no_devices"
                else:
                    self._email = email
                    self._password = password
                    self._region = region
                    self._appliances = appliances
                    return await self.async_step_device()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle the device-picker step."""
        errors: dict[str, str] = {}

        options = {
            str(a.get("applianceCode", "")): _appliance_label(a)
            for a in self._appliances
            if a.get("applianceCode")
        }

        if user_input is not None:
            appliance_code = user_input[CONF_APPLIANCE_CODE]

            await self.async_set_unique_id(
                f"{(self._email or '').lower()}:{appliance_code}"
            )
            self._abort_if_unique_id_configured()

            label = options.get(appliance_code, appliance_code)
            return self.async_create_entry(
                title=f"iLetComfort ({label})",
                data={
                    CONF_EMAIL: self._email,
                    CONF_PASSWORD: self._password,
                    CONF_REGION: self._region,
                    CONF_APPLIANCE_CODE: appliance_code,
                },
            )

        schema = vol.Schema(
            {vol.Required(CONF_APPLIANCE_CODE): vol.In(options)}
        )
        return self.async_show_form(
            step_id="device",
            data_schema=schema,
            errors=errors,
        )


class ILetComfortOptionsFlow(OptionsFlow):
    """Options: poll interval and per-poll fetch toggles.

    ``__init__`` must accept ``config_entry`` (the factory in
    ``ILetComfortConfigFlow.async_get_options_flow`` passes it positionally),
    but does NOT store it as ``self.config_entry`` (deprecated, removed in HA
    2025.12+) — the base class already exposes it as a property once the flow
    is initialized, i.e. from inside ``async_step_init`` onward.
    """

    def __init__(self, config_entry: ConfigEntry) -> None:
        del config_entry  # unused: see class docstring

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        schema = self.add_suggested_values_to_schema(
            vol.Schema(
                {
                    vol.Optional(CONF_SCAN_INTERVAL): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_SCAN_INTERVAL,
                            step=1,
                            unit_of_measurement="s",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Optional(CONF_FETCH_DIAGNOSTICS): BooleanSelector(),
                    vol.Optional(CONF_FETCH_SCHEDULE): BooleanSelector(),
                }
            ),
            {
                CONF_SCAN_INTERVAL: options.get(
                    CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL,
                ),
                CONF_FETCH_DIAGNOSTICS: options.get(
                    CONF_FETCH_DIAGNOSTICS, DEFAULT_FETCH_DIAGNOSTICS,
                ),
                CONF_FETCH_SCHEDULE: options.get(
                    CONF_FETCH_SCHEDULE, DEFAULT_FETCH_SCHEDULE,
                ),
            },
        )
        return self.async_show_form(step_id="init", data_schema=schema)
