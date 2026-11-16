"""Serve one of two approved floor plan packages."""

from asyncio import to_thread
from ipaddress import IPv4Address, IPv4Network
import os
import re
from stat import S_ISREG

from aiohttp import ClientError, ClientTimeout, web
from yarl import URL

FILES = ("lite.png", "full.png")
LOCAL_DIRECTORY = "floorplan_models"
PNG = b"\x89PNG\r\n\x1a\n"
MAX_BYTES = 64 * 1024 * 1024
CHUNK_SIZE = 64 * 1024
PRIVATE_LANS = tuple(IPv4Network(cidr) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
RESPONSE_HEADERS = {"Cache-Control": "private, no-cache", "X-Content-Type-Options": "nosniff"}


def publisher_origin(value: str) -> URL:
    origin = URL(value)
    if (
        not origin.host
        or origin.scheme not in {"http", "https"}
        or origin.user is not None
        or origin.path != "/"
        or origin.query_string
        or origin.fragment
        or not 1 <= origin.port <= 65535
    ):
        raise ValueError("Enter a publisher origin without a path or credentials")
    if origin.scheme == "http" and not any(IPv4Address(origin.host) in network for network in PRIVATE_LANS):
        raise ValueError("Unencrypted HTTP requires a private IPv4 address")
    return origin.origin()


def _open_local_package(directory, name):
    # Open both the directory and the fixed filename without following symlinks.
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        file_fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)

    stream = os.fdopen(file_fd, "rb")
    try:
        info = os.fstat(stream.fileno())
        if not S_ISREG(info.st_mode) or not len(PNG) <= info.st_size <= MAX_BYTES or stream.read(len(PNG)) != PNG:
            raise ValueError("Not a readable PNG package")
        stream.seek(0)
    except (ValueError, OSError):
        stream.close()
        raise
    return stream, info


def check_local_packages(directory):
    for name in FILES:
        stream, _ = _open_local_package(directory, name)
        stream.close()


async def serve_local_package(request, directory, name, executor=to_thread):
    if name not in FILES:
        raise web.HTTPNotFound()
    try:
        stream, info = await executor(_open_local_package, directory, name)
    except (OSError, ValueError):
        raise web.HTTPNotFound(text="Local floor plan package unavailable") from None

    try:
        etag = f'W/"local-{info.st_ino:x}-{info.st_mtime_ns:x}-{info.st_size:x}"'
        headers = {**RESPONSE_HEADERS, "ETag": etag}
        if request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers=headers)
        response = web.StreamResponse(headers={
            **headers, "Content-Type": "image/png", "Content-Length": str(info.st_size),
        })
        await response.prepare(request)
        remaining = info.st_size
        try:
            while remaining:
                part = await executor(stream.read, min(CHUNK_SIZE, remaining))
                if not part:
                    raise OSError("Package truncated during streaming")
                await response.write(part)
                remaining -= len(part)
            await response.write_eof()
        except OSError:
            if request.transport is not None:
                request.transport.close()
        return response
    finally:
        await executor(stream.close)


def _valid_package_response(response):
    return (
        response.status == 200
        and response.content_type == "image/png"
        and response.content_length is not None
        and len(PNG) <= response.content_length <= MAX_BYTES
        and response.headers.get("Content-Encoding", "identity") == "identity"
    )


async def check_remote_packages(session, origin, token):
    for name in FILES:
        async with session.get(
            origin / name,
            headers={"Authorization": f"Bearer {token}"},
            allow_redirects=False,
            timeout=ClientTimeout(total=12),
        ) as response:
            if response.status in {401, 403}:
                raise PermissionError("Publisher rejected the read token")
            if not _valid_package_response(response) or await response.content.readexactly(len(PNG)) != PNG:
                raise ValueError("Expected a PNG package")


async def serve_package(session, request, origin, token, name):
    if name not in FILES:
        raise web.HTTPNotFound()

    headers = {"Authorization": f"Bearer {token}"}
    if etag := request.headers.get("If-None-Match"):
        headers["If-None-Match"] = etag

    result = None
    try:
        async with session.get(
            origin / name,
            headers=headers,
            allow_redirects=False,
            timeout=ClientTimeout(total=90, connect=6, sock_read=20),
        ) as upstream:
            etag = upstream.headers.get("ETag", "")
            output = RESPONSE_HEADERS.copy()
            if re.fullmatch(r'"[a-f0-9]{64}"', etag):
                output["ETag"] = etag
            if upstream.status == 304 and "If-None-Match" in headers and "ETag" in output:
                return web.Response(status=304, headers=output)
            if not _valid_package_response(upstream):
                raise web.HTTPBadGateway(text="Remote floor plan package unavailable")
            output.update({"Content-Length": str(upstream.content_length), "Content-Type": "image/png"})
            result = web.StreamResponse(headers=output)
            await result.prepare(request)
            async for part in upstream.content.iter_chunked(CHUNK_SIZE):
                await result.write(part)
            await result.write_eof()
            return result
    except (TimeoutError, ClientError, ConnectionError):
        if result is not None and result.prepared:
            if request.transport is not None:
                request.transport.close()
            return result
        raise web.HTTPBadGateway(text="Remote floor plan service unavailable") from None
