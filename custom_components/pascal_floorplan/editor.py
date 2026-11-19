"""Read native Pascal Editor scenes and build private, cached Pascal PNG packages."""

import asyncio
import base64
from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
import re
import struct
import uuid
import zlib

from aiohttp import ClientError, ClientTimeout

PNG = b"\x89PNG\r\n\x1a\n"
MAX_PACKAGE = 64 * 1024 * 1024
MAX_ARTIFACT = 48 * 1024 * 1024
MAX_SCENE = 8 * 1024 * 1024
ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
ARTIFACT = re.compile(r"artifact://([0-9a-f]{64})")


class EditorError(ValueError):
    """Editor data is unavailable or does not satisfy the native API contract."""


def validate_scene(scene, expected_id):
    if not isinstance(scene, dict) or scene.get("id") != expected_id:
        raise EditorError("Unexpected scene identity")
    version = scene.get("version")
    if type(version) is not int or version < 0:
        raise EditorError("Invalid scene version")
    graph = scene.get("graph")
    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), dict):
        raise EditorError("Invalid scene graph")
    if not isinstance(graph.get("rootNodeIds"), list):
        raise EditorError("Invalid graph roots")
    hashes = set()
    for node in graph["nodes"].values():
        if not isinstance(node, dict):
            raise EditorError("Invalid scene node")
        src = (node.get("asset") or {}).get("src")
        if src is not None:
            match = ARTIFACT.fullmatch(src) if isinstance(src, str) else None
            if not match:
                raise EditorError("Scene assets must use native artifact references")
            hashes.add(match[1])
    return sorted(hashes)


def _chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def pack_scene(scene, assets, metadata):
    """Keep graph and renderer metadata intact; this does not simplify geometry."""
    payload = deepcopy(metadata)
    payload.update(version=1, graph=scene["graph"], assets=assets, sceneVersion=scene["version"])
    payload.setdefault("bindings", {})
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    if len(encoded) > MAX_PACKAGE:
        raise EditorError("Scene package exceeds size limit")
    return b"".join((PNG,
        _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)),
        _chunk(b"IDAT", zlib.compress(b"\0" * 5)),
        _chunk(b"psCl", gzip.compress(encoded, mtime=0)), _chunk(b"IEND", b"")))


def _atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            temporary.chmod(0o600)
            stream.write(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_metadata(path, scene_id):
    if not path.exists():
        return {}
    if path.stat().st_size > 1024 * 1024:
        raise EditorError("Renderer metadata exceeds size limit")
    metadata = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict) or not isinstance(metadata.get(scene_id, {}), dict):
        raise EditorError("Invalid renderer metadata")
    value = metadata.get(scene_id, {})
    if any(key in value for key in ("version", "graph", "assets", "sceneVersion")):
        raise EditorError("Renderer metadata cannot override scene data")
    return value


class EditorSource:
    """On-demand snapshots; credentials never enter model files or frontend URLs."""

    def __init__(self, session, origin, token, scenes, cache_dir, metadata_file, executor=None):
        if set(scenes) != {"lite.png", "full.png"} or any(
            not isinstance(s, str) or not ID.fullmatch(s) for s in scenes.values()
        ):
            raise EditorError("Invalid scene IDs")
        self.session, self.origin, self.token = session, origin, token
        self.scenes = dict(scenes)
        self.cache_dir, self.metadata_file = Path(cache_dir), Path(metadata_file)
        self.executor = executor or asyncio.to_thread
        self._lock = asyncio.Lock()
        self._records = {}

    async def _get(self, path, limit):
        async with self.session.get(
            self.origin / path, headers={"Authorization": f"Bearer {self.token}"},
            allow_redirects=False, timeout=ClientTimeout(total=45, connect=6, sock_read=20),
        ) as response:
            if response.status in (401, 403):
                raise PermissionError("Editor rejected the scene token")
            if response.status != 200:
                raise EditorError(f"Editor returned HTTP {response.status}")
            if response.content_length is not None and response.content_length > limit:
                raise EditorError("Editor response exceeds size limit")
            result = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                result.extend(chunk)
                if len(result) > limit:
                    raise EditorError("Editor response exceeds size limit")
            return bytes(result)

    async def scene(self, scene_id):
        if not ID.fullmatch(scene_id):
            raise EditorError("Invalid scene ID")
        scene = await self.executor(json.loads, await self._get("api/scenes/" + scene_id, MAX_SCENE))
        validate_scene(scene, scene_id)
        return scene

    async def validate(self):
        for scene_id in dict.fromkeys(self.scenes.values()):
            await self.scene(scene_id)

    async def _artifact(self, digest):
        path = self.cache_dir / "artifacts" / digest
        def cached():
            if not path.exists() or path.stat().st_size > MAX_ARTIFACT:
                return None
            content = path.read_bytes()
            return content if hashlib.sha256(content).hexdigest() == digest else None
        content = await self.executor(cached)
        if content is None:
            content = await self._get("api/artifacts/" + digest, MAX_ARTIFACT)
            if hashlib.sha256(content).hexdigest() != digest or content[:4] != b"glTF":
                raise EditorError("Invalid or corrupt GLB artifact")
            await self.executor(_atomic_write, path, content)
        return content

    async def _prune_artifacts(self):
        # Retain only hashes referenced by the two current scenes. A failure to
        # inspect either scene defers cleanup rather than risking useful files.
        try:
            used = set()
            for scene_id in dict.fromkeys(self.scenes.values()):
                used.update(validate_scene(await self.scene(scene_id), scene_id))
        except (ValueError, PermissionError, ClientError, TimeoutError):
            return
        def prune():
            folder = self.cache_dir / "artifacts"
            if folder.is_dir():
                for path in folder.iterdir():
                    if re.fullmatch(r"[0-9a-f]{64}", path.name) and path.name not in used:
                        path.unlink(missing_ok=True)
        await self.executor(prune)

    async def model(self, name):
        if name not in self.scenes:
            raise EditorError("Unknown model variant")
        async with self._lock:
            scene_id = self.scenes[name]
            scene = await self.scene(scene_id)
            metadata = await self.executor(_read_metadata, self.metadata_file, scene_id)
            key = hashlib.sha256(json.dumps(
                {"scene": scene, "metadata": metadata}, sort_keys=True, allow_nan=False
            ).encode()).hexdigest()
            path = self.cache_dir / name
            if self._records.get(name) == key and await self.executor(path.is_file):
                return self.cache_dir
            assets = {}
            total = 0
            for digest in validate_scene(scene, scene_id):
                content = await self._artifact(digest)
                total += len(content)
                if total * 4 // 3 > MAX_PACKAGE:
                    raise EditorError("Scene artifacts exceed package size limit")
                assets[digest] = (await self.executor(base64.b64encode, content)).decode("ascii")
            package = await self.executor(pack_scene, scene, assets, metadata)
            # A changed scene must not expose a partially updated asset snapshot.
            if await self.scene(scene_id) != scene:
                raise EditorError("Editor scene changed during download; reload the model")
            await self.executor(_atomic_write, path, package)
            self._records[name] = key
            await self._prune_artifacts()
            return self.cache_dir
