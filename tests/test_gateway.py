"""Exercise the streaming gateway without a running HA instance."""

import importlib.util
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer

spec = importlib.util.spec_from_file_location(
    "gateway", Path(__file__).resolve().parents[1] / "custom_components/floorplan_3d/gateway.py"
)
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)

PNG = b"\x89PNG\r\n\x1a\n" + b"a" * (129 * 1024)
ETAG = '"' + "a" * 64 + '"'


class GatewayTests(IsolatedAsyncioTestCase):
    def test_publisher_origin_allows_https_and_uses_http_only_on_private_lans(self):
        for origin in ("http://10.20.30.40:12345", "https://publisher.example", "https://[2001:db8::1]:8443"):
            self.assertEqual(str(gateway.publisher_origin(origin)), origin)
        for origin in (
            "http://127.0.0.1", "http://0.0.0.0", "http://1.1.1.1", "http://nas.lan",
            "https://x:y@publisher.example", "https://publisher.example/path",
            "https://publisher.example?token=secret", "https://publisher.example:70000",
            "https://publisher.example/#scene",
        ):
            with self.assertRaises(ValueError, msg=origin):
                gateway.publisher_origin(origin)

    async def test_local_stream_updates_after_atomic_replacement(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            packages = root / "floorplan_models"
            packages.mkdir()
            (packages / "lite.png").write_bytes(PNG)
            (packages / "full.png").write_bytes(PNG + b"full")
            (root / "secret.png").write_bytes(PNG + b"private")

            app = web.Application()

            async def handler(request):
                return await gateway.serve_local_package(request, packages, request.match_info["name"])

            app.router.add_get("/{name}", handler)
            async with TestClient(TestServer(app)) as client:
                async with client.get("/lite.png") as response:
                    etag = response.headers["ETag"]
                    self.assertEqual(await response.read(), PNG)
                    self.assertEqual(int(response.headers["Content-Length"]), len(PNG))
                async with client.get("/lite.png", headers={"If-None-Match": etag}) as response:
                    self.assertEqual(response.status, 304)
                async with client.get("/full.png") as response:
                    self.assertEqual(await response.read(), PNG + b"full")
                (packages / "next.png").write_bytes(PNG + b"updated")
                (packages / "next.png").replace(packages / "lite.png")
                async with client.get("/lite.png", headers={"If-None-Match": etag}) as response:
                    self.assertEqual(response.status, 200)
                    self.assertNotEqual(response.headers["ETag"], etag)
                    self.assertEqual(await response.read(), PNG + b"updated")
                (packages / "full.png").unlink()
                async with client.get("/full.png") as response:
                    self.assertEqual(response.status, 404)
                (packages / "full.png").symlink_to(root / "secret.png")
                async with client.get("/full.png") as response:
                    self.assertEqual(response.status, 404)
                async with client.get("/secret.png") as response:
                    self.assertEqual(response.status, 404)

    def test_local_validation_refuses_invalid_oversized_or_nonregular_files(self):
        with TemporaryDirectory() as folder:
            packages = Path(folder)
            lite = packages / "lite.png"
            lite.write_bytes(b"not png")
            with self.assertRaises(ValueError):
                gateway.check_local_packages(packages)
            with lite.open("wb") as stream:
                stream.write(PNG)
                stream.truncate(gateway.MAX_BYTES + 1)
            with self.assertRaises(ValueError):
                gateway.check_local_packages(packages)
            lite.unlink()
            os.mkfifo(lite)
            with self.assertRaises(ValueError):
                gateway.check_local_packages(packages)

    async def test_streams_only_the_two_selected_packages_with_private_auth(self):
        requests = []

        async def upstream_request(request):
            requests.append(request.path)
            self.assertEqual(request.headers["Authorization"], "Bearer private-token")
            if request.headers.get("If-None-Match") == ETAG:
                return web.Response(status=304, headers={"ETag": ETAG})
            if request.path == "/full.png":
                raise web.HTTPTemporaryRedirect(location="/not-allowed")
            return web.Response(body=PNG, content_type="image/png", headers={"ETag": ETAG})

        upstream = web.Application()
        upstream.router.add_get("/{name}", upstream_request)
        async with TestServer(upstream) as server, ClientSession() as session:
            proxy = web.Application()

            async def handler(request):
                return await gateway.serve_package(session, request, server.make_url("/"),
                                                   "private-token", request.match_info["name"])

            proxy.router.add_get("/{name}", handler)
            async with TestClient(TestServer(proxy)) as client:
                async with client.get("/lite.png") as response:
                    self.assertEqual(response.headers["ETag"], ETAG)
                    self.assertEqual(await response.read(), PNG)
                async with client.get("/lite.png", headers={"If-None-Match": ETAG}) as response:
                    self.assertEqual(response.status, 304)
                async with client.get("/full.png") as response:
                    self.assertEqual(response.status, 502)
                async with client.get("/other.png") as response:
                    self.assertEqual(response.status, 404)
                self.assertEqual(requests, ["/lite.png", "/lite.png", "/full.png"])
