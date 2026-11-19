"""Native Editor transport, snapshot consistency, model packaging and cache tests."""
import asyncio
import base64
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

spec = importlib.util.spec_from_file_location("editor", Path(__file__).resolve().parents[1] / "custom_components/pascal_floorplan/editor.py")
editor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(editor)
TOKEN = "test-scene-token-" + "A" * 32
GLB = b"glTF" + struct.pack("<II", 2, 12)
DIGEST = hashlib.sha256(GLB).hexdigest()


def unpack(path):
    content = path.read_bytes()
    offset = 8
    while offset < len(content):
        size = struct.unpack_from(">I", content, offset)[0]
        if content[offset + 4:offset + 8] == b"psCl":
            return json.loads(gzip.decompress(content[offset + 8:offset + 8 + size]))
        offset += size + 12
    raise AssertionError("missing scene")


class EditorTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.metadata = self.root / "bindings.json"
        self.metadata.write_text(json.dumps({"full": {"bindings": {"lights": {"lamp": "light.example"}}, "modelSha256": "baseline"}}))
        self.scenes = {name: {"id": name, "version": 1, "graph": {"rootNodeIds": ["item"], "nodes": {"item": {"id": "item", "name": "客厅", "asset": {"src": "artifact://" + DIGEST}}}}} for name in ("lite", "full")}
        self.artifact_calls = 0
        self.scene_calls = 0
        self.corrupt = False
        self.change_during_download = False
        app = web.Application()
        async def scene(request):
            self.assertEqual(request.headers.get("Authorization"), "Bearer " + TOKEN)
            self.scene_calls += 1
            return web.json_response(self.scenes[request.match_info["id"]])
        async def artifact(request):
            self.assertEqual(request.headers.get("Authorization"), "Bearer " + TOKEN)
            self.artifact_calls += 1
            if self.change_during_download:
                self.scenes["full"]["version"] += 1
            return web.Response(body=b"bad" if self.corrupt else GLB)
        app.router.add_get("/api/scenes/{id}", scene)
        app.router.add_get("/api/artifacts/{id}", artifact)
        self.server = TestServer(app)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.session = ClientSession()
        self.addAsyncCleanup(self.session.close)
        self.source = editor.EditorSource(self.session, self.server.make_url("/"), TOKEN,
            {"full.png": "full", "lite.png": "lite"}, self.root / "cache", self.metadata)

    async def test_native_roundtrip_preserves_scene_assets_and_bindings(self):
        await self.source.validate()
        folder = await self.source.model("full.png")
        payload = unpack(folder / "full.png")
        self.assertEqual(payload["graph"], self.scenes["full"]["graph"])
        self.assertEqual(base64.b64decode(payload["assets"][DIGEST]), GLB)
        self.assertEqual(payload["bindings"]["lights"]["lamp"], "light.example")
        self.assertEqual(payload["modelSha256"], "baseline")
        self.assertNotIn(TOKEN.encode(), (folder / "full.png").read_bytes())
        self.assertEqual((folder / "full.png").stat().st_mode & 0o777, 0o600)

    async def test_version_update_and_shared_asset_cache(self):
        folder = await self.source.model("full.png")
        first = (folder / "full.png").read_bytes()
        await self.source.model("full.png")
        await self.source.model("lite.png")
        self.assertEqual(self.artifact_calls, 1)
        self.scenes["full"]["version"] = 2
        self.scenes["full"]["graph"]["nodes"]["item"]["name"] = "updated"
        await self.source.model("full.png")
        self.assertNotEqual(first, (folder / "full.png").read_bytes())
        self.assertEqual(unpack(folder / "full.png")["sceneVersion"], 2)
        self.assertEqual(self.artifact_calls, 1)

    async def test_metadata_update_invalidates_model(self):
        folder = await self.source.model("full.png")
        self.metadata.write_text(json.dumps({"full": {"bindings": {"doors": {"door": "binary_sensor.entry"}}}}))
        await self.source.model("full.png")
        self.assertIn("doors", unpack(folder / "full.png")["bindings"])

    async def test_concurrent_downloads_are_serialized(self):
        await asyncio.gather(self.source.model("full.png"), self.source.model("full.png"))
        self.assertEqual(self.artifact_calls, 1)

    async def test_corrupt_artifact_never_publishes(self):
        self.corrupt = True
        with self.assertRaises(editor.EditorError):
            await self.source.model("full.png")
        self.assertFalse((self.root / "cache/full.png").exists())

    async def test_scene_change_during_download_never_publishes(self):
        self.change_during_download = True
        with self.assertRaisesRegex(editor.EditorError, "changed during"):
            await self.source.model("full.png")
        self.assertFalse((self.root / "cache/full.png").exists())

    async def test_external_asset_reference_is_rejected(self):
        self.scenes["full"]["graph"]["nodes"]["item"]["asset"]["src"] = "http://internal/secret"
        with self.assertRaises(editor.EditorError):
            await self.source.model("full.png")
        self.assertEqual(self.artifact_calls, 0)

    async def test_failed_update_retains_previous_file_without_serving_as_success(self):
        folder = await self.source.model("full.png")
        old = (folder / "full.png").read_bytes()
        self.scenes["full"]["graph"]["nodes"]["item"]["asset"]["src"] = "invalid"
        with self.assertRaises(editor.EditorError):
            await self.source.model("full.png")
        self.assertEqual((folder / "full.png").read_bytes(), old)

    async def test_unknown_variant_and_invalid_ids_rejected(self):
        with self.assertRaises(editor.EditorError):
            await self.source.model("../secret")
        with self.assertRaises(editor.EditorError):
            await self.source.scene("../secret")

    async def test_unreferenced_artifacts_are_removed_after_successful_snapshot(self):
        folder = self.root / "cache/artifacts"
        folder.mkdir(parents=True)
        stale = folder / ("f" * 64)
        stale.write_bytes(b"stale")
        await self.source.model("full.png")
        self.assertFalse(stale.exists())
        self.assertTrue((folder / DIGEST).exists())
