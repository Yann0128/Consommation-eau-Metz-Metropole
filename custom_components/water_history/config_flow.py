"""Config flow: name the sensor, then connect it to the water portal."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_NAME
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.util import dt as dt_util

from .const import CONF_CONTRACT, CONF_LOGIN, CONF_PASSWORD, DOMAIN
from .portal import (
    PortalAuthError,
    PortalClient,
    PortalConnectionError,
    PortalDataError,
)

DEFAULT_NAME = "Consommation d'eau"


def entry_title(contract_id: str) -> str:
    return f"Contrat {contract_id}"


def _portal_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_LOGIN, default=defaults.get(CONF_LOGIN, "")): TextSelector(
                TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
            ),
            vol.Required(CONF_PASSWORD): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
            ),
            vol.Required(CONF_CONTRACT, default=defaults.get(CONF_CONTRACT, "")): TextSelector(
                TextSelectorConfig(type=TextSelectorType.TEXT)
            ),
        }
    )


class WaterHistoryConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 2

    def __init__(self) -> None:
        self._name = DEFAULT_NAME

    async def _async_check_portal(self, user_input: dict[str, Any]) -> str | None:
        """Return an error key, or None when the portal accepts the credentials."""
        contract = user_input[CONF_CONTRACT].strip()
        if not contract.isdigit():
            return "invalid_contract"
        client = PortalClient(
            async_create_clientsession(self.hass, cookie_jar=aiohttp.DummyCookieJar()),
            user_input[CONF_LOGIN].strip(),
            user_input[CONF_PASSWORD],
            contract,
        )
        today = dt_util.now().date()
        try:
            await client.async_fetch(today - timedelta(days=2), today)
        except PortalAuthError:
            return "invalid_auth"
        except PortalConnectionError:
            return "cannot_connect"
        except PortalDataError:
            return "invalid_contract"
        except Exception:  # noqa: BLE001
            return "unknown"
        return None

    @staticmethod
    def _clean(user_input: dict[str, Any]) -> dict[str, Any]:
        return {
            CONF_LOGIN: user_input[CONF_LOGIN].strip(),
            CONF_PASSWORD: user_input[CONF_PASSWORD],
            CONF_CONTRACT: user_input[CONF_CONTRACT].strip(),
        }

    # --- creation -------------------------------------------------------
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input[CONF_NAME].strip()
            if not name:
                errors["base"] = "empty_name"
            else:
                await self.async_set_unique_id(name.casefold())
                self._abort_if_unique_id_configured()
                self._name = name
                return await self.async_step_portal()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_NAME, default=self._name): str}),
            errors=errors,
        )

    async def async_step_portal(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            error = await self._async_check_portal(user_input)
            if error is None:
                data = {CONF_NAME: self._name, **self._clean(user_input)}
                return self.async_create_entry(title=entry_title(data[CONF_CONTRACT]), data=data)
            errors["base"] = error
        return self.async_show_form(
            step_id="portal", data_schema=_portal_schema(user_input), errors=errors
        )

    # --- change credentials / contract id / entries created before v0.4 --
    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            error = await self._async_check_portal(user_input)
            if error is None:
                data = self._clean(user_input)
                return self.async_update_reload_and_abort(
                    entry, title=entry_title(data[CONF_CONTRACT]), data_updates=data
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_portal_schema(user_input or dict(entry.data)),
            errors=errors,
        )

    # --- password refused several times in a row ------------------------
    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            error = await self._async_check_portal(user_input)
            if error is None:
                data = self._clean(user_input)
                return self.async_update_reload_and_abort(
                    entry, title=entry_title(data[CONF_CONTRACT]), data_updates=data
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_portal_schema(user_input or dict(entry.data)),
            errors=errors,
        )
