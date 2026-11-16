"""Choose and validate the source of floor plan packages."""

import re

from aiohttp import ClientError
from homeassistant.config_entries import SOURCE_RECONFIGURE, ConfigFlow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType
import probatio as vol

from . import DOMAIN, LOCAL, REMOTE
from .gateway import LOCAL_DIRECTORY, check_local_packages, check_remote_packages, publisher_origin


class FloorPlanConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")
        return self.async_show_menu(step_id="user", menu_options=[LOCAL, REMOTE])

    async def async_step_reconfigure(self, user_input=None):
        return self.async_show_menu(step_id="reconfigure", menu_options=[LOCAL, REMOTE])

    async def async_step_local(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                await self.hass.async_add_executor_job(
                    check_local_packages, self.hass.config.path(LOCAL_DIRECTORY)
                )
            except (OSError, ValueError):
                errors["base"] = "invalid_local_packages"
            else:
                return self._finish({"source": LOCAL})
        return self.async_show_form(step_id=LOCAL, data_schema=vol.Schema({}), errors=errors)

    async def async_step_remote(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                origin = publisher_origin(user_input["origin"])
            except (ValueError, TypeError):
                errors["origin"] = "invalid_origin"
            else:
                token = user_input["token"]
                if not re.fullmatch(r"[A-Za-z0-9._~+/=-]{32,256}", token):
                    errors["token"] = "invalid_token"
                else:
                    try:
                        await check_remote_packages(async_get_clientsession(self.hass), origin, token)
                    except PermissionError:
                        errors["token"] = "invalid_token"
                    except (ClientError, TimeoutError, EOFError, ValueError):
                        errors["base"] = "cannot_connect"
                    else:
                        return self._finish({"source": REMOTE, "origin": str(origin), "token": token})
        schema = vol.Schema({
            vol.Required("origin"): str,
            vol.Required("token"): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
        })
        if self.source == SOURCE_RECONFIGURE:
            schema = self.add_suggested_values_to_schema(
                schema, {"origin": self._get_reconfigure_entry().data.get("origin", "")}
            )
        return self.async_show_form(step_id=REMOTE, data_schema=schema, errors=errors)

    def _finish(self, data):
        if self.source == SOURCE_RECONFIGURE:
            return self.async_update_reload_and_abort(self._get_reconfigure_entry(), data=data)
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")
        return self.async_create_entry(title="3D Floor Plan", data=data)
