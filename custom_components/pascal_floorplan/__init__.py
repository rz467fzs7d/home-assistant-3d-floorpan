"""Expose floor plan packages through Home Assistant authentication."""

from pathlib import Path

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .gateway import LOCAL_DIRECTORY, publisher_origin, serve_local_package, serve_package

DOMAIN = "pascal_floorplan"
LOCAL = "local"
REMOTE = "remote"


async def async_setup(hass, config):
    hass.http.register_view(PascalFloorPlanView(hass))
    return True


async def async_setup_entry(hass, entry):
    if entry.data.get("source", REMOTE) == LOCAL:
        hass.data[DOMAIN] = Path(hass.config.path(LOCAL_DIRECTORY))
    else:
        hass.data[DOMAIN] = (publisher_origin(entry.data["origin"]), entry.data["token"])
    return True


async def async_unload_entry(hass, entry):
    hass.data.pop(DOMAIN, None)
    return True


class PascalFloorPlanView(HomeAssistantView):
    url = "/api/pascal_floorplan/{name}"
    name = "api:pascal_floorplan:package"
    requires_auth = True

    def __init__(self, hass):
        self.hass = hass

    async def get(self, request, name):
        if (settings := self.hass.data.get(DOMAIN)) is None:
            raise web.HTTPServiceUnavailable()
        if isinstance(settings, Path):
            return await serve_local_package(request, settings, name, self.hass.async_add_executor_job)
        return await serve_package(async_get_clientsession(self.hass), request, *settings, name)
