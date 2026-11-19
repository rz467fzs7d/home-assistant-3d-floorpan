"""Exercise the real Home Assistant config flow and signed HTTP routes on loopback."""

from datetime import timedelta
from pathlib import Path
from shutil import copytree, rmtree
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, unused_port

from homeassistant import bootstrap, loader
from homeassistant.components.http.auth import async_sign_path
from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

DOMAIN = "floorplan_3d"
ROUTE = f"/api/{DOMAIN}/lite.png"
TOKEN = "A" * 32
PNG = b"\x89PNG\r\n\x1a\n"


class HomeAssistantTests(IsolatedAsyncioTestCase):
    async def test_configure_both_sources_and_require_auth(self):
        with TemporaryDirectory(delete=False) as folder:
            self.addCleanup(rmtree, folder)
            root = Path(folder)
            copytree(Path(__file__).resolve().parents[1] / "custom_components", root / "custom_components")
            packages = root / "floorplan_models"
            packages.mkdir()
            (packages / "lite.png").write_bytes(PNG + b"local lite")
            hass = HomeAssistant(folder)
            loader.async_setup(hass)
            hass.config.skip_pip = True
            self.addAsyncCleanup(hass.async_stop, force=True)
            with patch.object(bootstrap, "DEFAULT_INTEGRATIONS", set()):
                self.assertIsNotNone(await bootstrap.async_from_config_dict(
                    {"http": {"server_host": "127.0.0.1", "server_port": unused_port()}}, hass
                ))
            flow = hass.config_entries.flow
            first = await flow.async_init(DOMAIN, context={"source": SOURCE_USER})
            self.assertEqual(first["type"], FlowResultType.MENU)
            self.assertEqual(first["menu_options"], ["local", "remote", "editor"])
            current = await flow.async_configure(first["flow_id"], {"next_step_id": "local"})
            self.assertEqual(current["step_id"], "local")
            rejected = await flow.async_configure(first["flow_id"], {})
            self.assertEqual(rejected["errors"], {"base": "invalid_local_packages"})
            (packages / "full.png").write_bytes(PNG + b"local full")
            local = await flow.async_configure(first["flow_id"], {})
            self.assertEqual(local["type"], FlowResultType.CREATE_ENTRY)
            entry = local["result"]
            self.assertEqual(dict(entry.data), {"source": "local"})

            user = await hass.auth.async_create_user("Fixture user")
            refresh = await hass.auth.async_create_refresh_token(user, "http://localhost")
            signed = async_sign_path(hass, ROUTE, timedelta(minutes=1), refresh_token_id=refresh.id)
            async with TestClient(TestServer(hass.http.app)) as client:
                self.assertEqual((await client.get(ROUTE)).status, 401)
                legacy_route = "/api/pascal_floorplan/lite.png"
                self.assertEqual((await client.get(legacy_route)).status, 401)
                legacy_signed = async_sign_path(hass, legacy_route, timedelta(minutes=1), refresh_token_id=refresh.id)
                async with client.get(legacy_signed) as legacy_reply:
                    self.assertEqual(legacy_reply.status, 200)
                async with client.get(signed) as reply:
                    self.assertEqual(reply.status, 200)
                    self.assertEqual(await reply.read(), PNG + b"local lite")

                async def serve(request):
                    if request.headers.get("Authorization") != f"Bearer {TOKEN}":
                        raise web.HTTPUnauthorized()
                    return web.Response(body=PNG + request.match_info["file"].encode(), content_type="image/png")

                app = web.Application()
                app.router.add_get("/{file}", serve)
                async with TestServer(app) as publisher:
                    integration_root = "custom_components.floorplan_3d"
                    # Only the test fixture bypasses the private-address rule to run on loopback.
                    with patch(f"{integration_root}.config_flow.publisher_origin", return_value=publisher.make_url("/")), patch(
                        f"{integration_root}.publisher_origin", return_value=publisher.make_url("/")
                    ):
                        updated = await flow.async_init(
                            DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
                        )
                        await flow.async_configure(updated["flow_id"], {"next_step_id": "remote"})
                        rejected = await flow.async_configure(updated["flow_id"], {
                            "origin": "http://192.168.1.20:8124", "token": "B" * 32,
                        })
                        self.assertEqual(rejected["errors"], {"token": "invalid_token"})
                        result = await flow.async_configure(updated["flow_id"], {
                            "origin": "http://192.168.1.20:8124", "token": TOKEN,
                        })
                        self.assertEqual(result["reason"], "reconfigure_successful")
                        await hass.async_block_till_done()
                        self.assertEqual(len(hass.config_entries.async_entries(DOMAIN)), 1)
                        self.assertEqual(entry.data["source"], "remote")
                        async with client.get(signed) as reply:
                            self.assertEqual(reply.status, 200)
                            self.assertEqual(await reply.read(), PNG + b"lite.png")

                async def native_scene(request):
                    if request.headers.get("Authorization") != f"Bearer {TOKEN}":
                        raise web.HTTPUnauthorized()
                    name = request.match_info["scene"]
                    return web.json_response({"id": name, "version": 1, "graph": {"rootNodeIds": [], "nodes": {}}})

                native = web.Application()
                native.router.add_get("/api/scenes/{scene}", native_scene)
                async with TestServer(native) as editor:
                    with patch(f"{integration_root}.config_flow.publisher_origin", return_value=editor.make_url("/")), patch(
                        f"{integration_root}.publisher_origin", return_value=editor.make_url("/")
                    ):
                        updated = await flow.async_init(
                            DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
                        )
                        await flow.async_configure(updated["flow_id"], {"next_step_id": "editor"})
                        rejected = await flow.async_configure(updated["flow_id"], {
                            "origin": "http://192.168.1.20:3000", "token": "B" * 32,
                            "full_scene_id": "full", "lite_scene_id": "lite",
                        })
                        self.assertEqual(rejected["errors"], {"token": "invalid_editor_token"})
                        result = await flow.async_configure(updated["flow_id"], {
                            "origin": "http://192.168.1.20:3000", "token": TOKEN,
                            "full_scene_id": "full", "lite_scene_id": "lite",
                        })
                        self.assertEqual(result["reason"], "reconfigure_successful")
                        await hass.async_block_till_done()
                        self.assertEqual(entry.data["source"], "editor")
                        self.assertEqual((await client.get(ROUTE)).status, 401)
                        async with client.get(signed) as reply:
                            self.assertEqual(reply.status, 200)
                            content = await reply.read()
                            self.assertTrue(content.startswith(PNG))
                            self.assertNotIn(TOKEN.encode(), content)


    async def test_native_legacy_migration_preserves_settings_and_unload(self):
        with TemporaryDirectory(delete=False) as folder:
            self.addCleanup(rmtree, folder)
            root = Path(folder)
            copytree(Path(__file__).resolve().parents[1] / "custom_components", root / "custom_components")
            packages = root / "floorplan_models"
            packages.mkdir()
            for name in ("lite", "full"):
                (packages / (name + ".png")).write_bytes(PNG + name.encode())
            hass = HomeAssistant(folder)
            loader.async_setup(hass)
            hass.config.skip_pip = True
            self.addAsyncCleanup(hass.async_stop, force=True)
            with patch.object(bootstrap, "DEFAULT_INTEGRATIONS", set()):
                await bootstrap.async_from_config_dict(
                    {"http": {"server_host": "127.0.0.1", "server_port": unused_port()}}, hass
                )
            flow = hass.config_entries.flow
            old = await flow.async_init("pascal_floorplan", context={"source": SOURCE_USER})
            await flow.async_configure(old["flow_id"], {"next_step_id": "local"})
            created = await flow.async_configure(old["flow_id"], {})
            old_entry = created["result"]
            hass.config_entries.async_update_entry(old_entry, options={"fixture": "preserve"})
            await hass.async_block_till_done()
            old_cache = root / ".storage" / "pascal_floorplan" / old_entry.entry_id
            old_cache.mkdir(parents=True)
            (old_cache / "cached-artifact").write_bytes(b"verified original bytes")
            await hass.config_entries.async_reload(old_entry.entry_id)
            current_cache = root / ".storage" / DOMAIN / old_entry.entry_id
            self.assertEqual((current_cache / "cached-artifact").read_bytes(), b"verified original bytes")
            (current_cache / "cached-artifact").write_bytes(b"newer active cache")
            upgrade = await flow.async_init(DOMAIN, context={"source": SOURCE_USER})
            self.assertIn("migrate", upgrade["menu_options"])
            migrated = await flow.async_configure(upgrade["flow_id"], {"next_step_id": "migrate"})
            self.assertEqual(migrated["type"], FlowResultType.CREATE_ENTRY)
            new_entry = migrated["result"]
            await hass.async_block_till_done()
            self.assertEqual(dict(new_entry.data), dict(old_entry.data))
            self.assertEqual(dict(new_entry.options), dict(old_entry.options))
            self.assertEqual(new_entry.state.value, "loaded")
            migrated_cache = root / ".storage" / DOMAIN / new_entry.entry_id
            self.assertEqual((migrated_cache / "cached-artifact").read_bytes(), b"newer active cache")
            self.assertTrue(await hass.config_entries.async_remove(old_entry.entry_id))
            self.assertIn(DOMAIN, hass.data)
            self.assertEqual(hass.data[DOMAIN + "_owner"], new_entry.entry_id)
            user = await hass.auth.async_create_user("Migration fixture")
            refresh = await hass.auth.async_create_refresh_token(user, "http://localhost")
            async with TestClient(TestServer(hass.http.app)) as client:
                for domain in (DOMAIN, "pascal_floorplan"):
                    route = f"/api/{domain}/lite.png"
                    self.assertEqual((await client.get(route)).status, 401)
                    signed = async_sign_path(hass, route, timedelta(minutes=1), refresh_token_id=refresh.id)
                    async with client.get(signed) as reply:
                        self.assertEqual(reply.status, 200)
                        self.assertEqual(await reply.read(), PNG + b"lite")
