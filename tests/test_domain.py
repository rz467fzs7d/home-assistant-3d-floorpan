"""Generic integration identity and the legacy import compatibility contract."""
import json
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]

class DomainTests(TestCase):
    def test_generic_manifest_and_legacy_dependency(self):
        generic = json.loads((ROOT / 'custom_components/floorplan_3d/manifest.json').read_text())
        self.assertEqual(generic['domain'], 'floorplan_3d')
        legacy = json.loads((ROOT / 'custom_components/pascal_floorplan/manifest.json').read_text())
        self.assertIn('floorplan_3d', legacy['dependencies'])
