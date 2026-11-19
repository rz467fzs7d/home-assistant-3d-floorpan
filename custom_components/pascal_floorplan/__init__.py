"""Compatibility for existing Pascal Floor Plan configuration entries."""
from custom_components.floorplan_3d import async_setup_entry, async_unload_entry

async def async_setup(hass, config):
    return True
