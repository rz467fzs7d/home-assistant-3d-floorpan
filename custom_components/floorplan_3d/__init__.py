"""Expose floor plan packages through Home Assistant authentication."""

from pathlib import Path
import logging

from aiohttp import ClientError

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .editor import EditorSource
from .gateway import LOCAL_DIRECTORY, publisher_origin, serve_local_package, serve_package

DOMAIN = "floorplan_3d"
LEGACY_DOMAIN = "pascal_floorplan"
LOCAL = "local"
REMOTE = "remote"
EDITOR = "editor"
_LOGGER = logging.getLogger(__name__)


async def async_setup(hass, config):
    hass.http.register_view(FloorPlanView(hass))
    return True


async def async_setup_entry(hass, entry):
    owner = hass.data.get(DOMAIN + "_owner")
    if entry.domain == LEGACY_DOMAIN and owner and owner != entry.entry_id:
        return True
    if entry.domain == LEGACY_DOMAIN:
        await hass.async_add_executor_job(
            _copy_legacy_cache, hass.config.path(".storage"), entry.entry_id, entry.entry_id
        )
    if entry.domain == DOMAIN:
        legacy = hass.config_entries.async_entries(LEGACY_DOMAIN)
        if len(legacy) == 1:
            await hass.async_add_executor_job(
                _copy_legacy_cache, hass.config.path(".storage"), legacy[0].entry_id, entry.entry_id
            )
    hass.data[DOMAIN + "_owner"] = entry.entry_id
    if entry.data.get("source") == EDITOR:
        hass.data[DOMAIN] = EditorSource(
            async_get_clientsession(hass), publisher_origin(entry.data["origin"]), entry.data["token"],
            {"lite.png": entry.data["lite_scene_id"], "full.png": entry.data["full_scene_id"]},
            hass.config.path(".storage", DOMAIN, entry.entry_id),
            hass.config.path(LOCAL_DIRECTORY, "editor-bindings.json"),
            hass.async_add_executor_job,
        )
    elif entry.data.get("source", REMOTE) == LOCAL:
        hass.data[DOMAIN] = Path(hass.config.path(LOCAL_DIRECTORY))
    else:
        hass.data[DOMAIN] = (publisher_origin(entry.data["origin"]), entry.data["token"])
    return True


async def async_unload_entry(hass, entry):
    if hass.data.get(DOMAIN + "_owner") == entry.entry_id:
        hass.data.pop(DOMAIN, None)
        hass.data.pop(DOMAIN + "_owner", None)
    return True


def _copy_legacy_cache(storage, old_id, new_id):
    """Keep reusable resource bytes during a native entry-domain migration."""
    import shutil
    destination = Path(storage) / DOMAIN / new_id
    if destination.exists():
        return
    for domain in (DOMAIN, LEGACY_DOMAIN):
        source = Path(storage) / domain / old_id
        if source.is_dir() and source != destination:
            shutil.copytree(source, destination)
            return


class FloorPlanView(HomeAssistantView):
    url = "/api/floorplan_3d/{name}"
    extra_urls = ["/api/pascal_floorplan/{name}"]
    name = "api:floorplan_3d:package"
    requires_auth = True

    def __init__(self, hass):
        self.hass = hass

    async def get(self, request, name):
        if (settings := self.hass.data.get(DOMAIN)) is None:
            raise web.HTTPServiceUnavailable()
        if isinstance(settings, EditorSource):
            if name not in settings.scenes:
                raise web.HTTPNotFound()
            try:
                directory = await settings.model(name)
            except (ValueError, OSError, TimeoutError, ClientError):
                _LOGGER.warning("Pascal Editor model unavailable")
                raise web.HTTPBadGateway(text="Pascal Editor model unavailable") from None
            return await serve_local_package(request, directory, name, self.hass.async_add_executor_job)
        if isinstance(settings, Path):
            return await serve_local_package(request, settings, name, self.hass.async_add_executor_job)
        return await serve_package(async_get_clientsession(self.hass), request, *settings, name)
