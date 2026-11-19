"""Legacy-domain reconfiguration; new installations use floorplan_3d."""
from custom_components.floorplan_3d.config_flow import FloorPlanConfigFlow

class LegacyFloorPlanConfigFlow(FloorPlanConfigFlow, domain="pascal_floorplan"):
    pass
