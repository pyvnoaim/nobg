"""
nobg: self-hosted background remover
====================================
Run:
    uv run remove_bg.py

Then open http://localhost:7860 in your browser.
The AI model (about 180MB) downloads on first AI use, after that it's instant.
"""

import asyncio
import base64
import binascii
import ctypes
import http.client
import io
import ipaddress
import math
import os
import secrets
import socket
import threading
import time
import urllib.parse
import urllib.request
import warnings
import webbrowser
from collections import OrderedDict
from pathlib import Path

import numpy as np
import uvicorn
from PIL import Image, ImageFilter, ImageOps, UnidentifiedImageError
from scipy import ndimage  # ships with rembg, which imports it itself
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

# A tiny file can declare a huge canvas (a decompression bomb): the byte caps below only see the file, not the
# pixels. Every image, uploaded or fetched, goes through _open, which checks width x height before anything is
# decoded. Pillow's own hard DecompressionBombError (at twice MAX_IMAGE_PIXELS) stays as a second line that no
# warnings filter can switch off.
# ponytail: 40MP covers any camera photo; isnet peaked ~1.6GB at 12MP, raise with care against the 3g mem_limit.
MAX_PIXELS = 40_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS // 2
# Between 20 and 40MP Pillow only warns. Those images are allowed here, so the warning would just fill the log.
warnings.simplefilter("ignore", Image.DecompressionBombWarning)
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # same as the proxy's client_max_body_size
MAX_FETCH_BYTES = 50 * 1024 * 1024
# Only formats a browser can show, so the page can always display the original. It also keeps Pillow's
# rarely used decoders (TIFF, FPX, PDF and the rest) away from files that strangers send.
FORMATS = ("PNG", "JPEG", "WEBP", "GIF", "BMP", "AVIF")  # multi-picture JPEGs (MPO) open through JPEG

# isnet: about 180MB, 1024² input, mask upscaled to full res. birefnet-general is cleaner
# but a 1GB download and peaks at 8-13GB RAM. It got OOM-killed on the VPS.
MODEL = "isnet-general-use"
_session = None


class UserError(Exception):
    """Something the person can act on. Message and hint are shown in the page exactly as written here."""

    def __init__(self, message, hint="", status=400):
        super().__init__(message)
        self.hint = hint
        self.status = status


TOO_MANY_PIXELS = ("That image is over 40 megapixels", "Scale it down and drop it again.", 413)


def _rembg():
    """
    Imported on first use, not at startup: rembg pulls in numba code that compiles on import, which took 20 to 80
    seconds in a fresh container and kept the page from loading at all. __main__ starts this import in the
    background, so the first AI request after a deploy rarely has to wait for it.
    """
    import rembg
    return rembg


def get_session():
    """Loaded on first AI use: color mode never needs the 180MB download."""
    global _session
    if _session is None:
        print(f"Loading model: {MODEL} (first run downloads about 180MB)...")
        _session = _rembg().new_session(MODEL)
        print("Model ready.")
    return _session


def flatten(img, bg):
    """bg is "transparent", "white" or "black"."""
    if bg == "transparent":
        return img
    out = Image.new("RGB", img.size, bg)
    out.paste(img, mask=img.getchannel("A"))
    return out


def _public_ip(host):
    """
    This fetch runs server-side from a publicly reachable app, so an arbitrary URL
    is an SSRF probe into the VPS: http://127.0.0.1:9000 or the cloud metadata
    address at 169.254.169.254 would otherwise be reachable, with hit/miss leaking
    through timing and error differences. Reject anything not globally routable,
    and return the checked address so the caller connects to exactly that one: a
    second lookup could answer differently (DNS rebinding).
    """
    if not host:
        raise UserError("That link has no host")
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise UserError("That link's host could not be found", "Check the address, or save the image and drop the file.")
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise UserError("That link does not point to the public internet", "Save the image and drop the file instead.")
    return infos[0][4][0]


# The sockets of the fetch running in this thread, so its deadline can cut them off. See fetch_url.
_fetching = threading.local()


def _track(sock):
    getattr(_fetching, "socks", []).append(sock)
    # Listed first, checked second: either the timer finds this socket in the list, or this check finds the timer
    # has fired. Without it a redirect that lands just after the deadline would open a socket nothing ever cuts off.
    expired = getattr(_fetching, "expired", None)
    if expired and expired.is_set():
        sock.close()
        raise OSError("the fetch deadline has passed")
    return sock


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = _track(socket.create_connection((_public_ip(self.host), self.port), self.timeout))


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        sock = socket.create_connection((_public_ip(self.host), self.port), self.timeout)
        # TLS and the Host header still use the name, only the TCP connection goes to the checked IP.
        # The handshake runs after tracking, so a server stalling inside it is cut off as well.
        self.sock = _track(self._context.wrap_socket(sock, server_hostname=self.host, do_handshake_on_connect=False))
        self.sock.do_handshake()


class _PinnedHTTP(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_PinnedHTTPConnection, req)


class _PinnedHTTPS(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PinnedHTTPSConnection, req, context=self._context)


class _HttpOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """A public URL can 302 anywhere; every hop connects through the pinned handlers above, so only the scheme is left."""

    max_redirections = 3  # each hop may take a full socket timeout, and an image link rarely needs more

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith(("http://", "https://")):
            raise UserError("That link redirects somewhere that is not a web address")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


FETCH_SECONDS = 30


def fetch_url(url):
    """Dragging an image off a webpage yields its URL, not a file. Fetch it here and hand back the bytes."""
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise UserError("Only http and https links work")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    # ProxyHandler({}): never route through an environment proxy, which would make the IP check meaningless.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _PinnedHTTP, _PinnedHTTPS, _HttpOnlyRedirects)
    # The socket timeout only bounds one wait for data, so a server dripping a byte at a time, in the handshake,
    # the headers, a redirect or the body, could hold this open for days. One timer covers the whole fetch: when
    # it fires it shuts every socket this fetch opened, and whatever was waiting on them fails at once.
    too_slow = UserError("That link took too long to load", "Save the image and drop the file instead.")
    _fetching.socks = socks = []
    _fetching.expired = expired = threading.Event()
    deadline = time.monotonic() + FETCH_SECONDS

    def cut_off():
        expired.set()
        for sock in list(socks):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # already closed

    timer = threading.Timer(FETCH_SECONDS, cut_off)
    timer.start()
    chunks, size = [], 0
    try:
        with opener.open(req, timeout=10) as r:
            while chunk := r.read1(64 * 1024):
                size += len(chunk)
                if size > MAX_FETCH_BYTES:
                    raise UserError("That image is over 50 MB", "Save a smaller version and drop the file.", 413)
                if time.monotonic() > deadline:  # a second line behind the timer
                    raise too_slow
                chunks.append(chunk)
    except (OSError, http.client.HTTPException, ValueError):
        if expired.is_set():
            raise too_slow
        raise UserError("That link could not be loaded", "Save the image and drop the file instead.")
    finally:
        timer.cancel()
        _fetching.socks, _fetching.expired = [], None
    if expired.is_set():  # a shut socket can also read as a clean end of data
        raise too_slow
    return b"".join(chunks)


def _open(data):
    """Identify the image and check its size. Reads the header only, nothing is decoded yet."""
    try:
        img = Image.open(io.BytesIO(data), formats=FORMATS)
    except Image.DecompressionBombError:
        raise UserError(*TOO_MANY_PIXELS)
    except (UnidentifiedImageError, OSError, ValueError):
        raise UserError("That file is not an image nobg can read", "Use PNG, JPEG, WebP, GIF, BMP or AVIF.", 415)
    if img.width * img.height > MAX_PIXELS:
        raise UserError(*TOO_MANY_PIXELS)
    return img


def _decode(data):
    """The image as RGBA, upright. A PIL image, not an array: at 40MP every extra copy costs 160MB."""
    img = _open(data)
    try:
        # exif_transpose: phone photos store their rotation as a tag, and the browser shows them rotated
        return ImageOps.exif_transpose(img).convert("RGBA")
    except Image.DecompressionBombError:
        raise UserError(*TOO_MANY_PIXELS)
    except (OSError, SyntaxError, ValueError):
        raise UserError("That image could not be read", "The file looks damaged. Try exporting it again.")


def remove_bg_ai(input_image):
    """AI-based removal, good for complex/unknown backgrounds."""
    return _rembg().remove(input_image, session=get_session())


def remove_bg_color(input_image, tolerance, edge_smooth, edges_only=True):
    """
    Color/threshold-based removal, pixel-perfect for solid color backgrounds.
    Samples the background color from the image border and removes matching pixels:
    with edges_only just the ones connected to the image border, so a highlight
    in the background color inside the subject survives. Otherwise all of them.
    Works great for product shots on white/solid backgrounds.
    """
    # An RGBA image from _decode is used as it is and gets its alpha replaced. An array (the self-check) is converted.
    img = input_image if isinstance(input_image, Image.Image) else Image.fromarray(input_image).convert("RGBA")
    data = np.asarray(img)  # uint8, no float copy of the whole image: that tripled peak RAM at 40MP

    # Background color: per-channel median of the one-pixel frame around the image. It holds as long as
    # the subject covers less than half of the border, so a bust shot spanning the bottom edge still works.
    frame = np.concatenate([data[0], data[-1], data[:, 0], data[:, -1]])[:, :3]
    bg_color = np.median(frame, axis=0)

    # Squared per-pixel distance from bg color, one channel at a time to keep the temporaries small
    dist2 = np.zeros(data.shape[:2], dtype=np.float32)
    for c in range(3):
        ch = data[:, :, c].astype(np.float32)
        ch -= bg_color[c]
        ch *= ch
        dist2 += ch

    # Build alpha mask
    match = dist2 <= tolerance ** 2
    if edges_only:
        # label 0 is the non-matching rest, every other label is one 4-connected matching region
        labels, n = ndimage.label(match)  # default structure is 4-connectivity
        keep = np.zeros(n + 1, dtype=bool)
        keep[np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]])] = True
        keep[0] = False
        match = keep[labels]
    alpha = np.where(match, np.uint8(0), np.uint8(255))  # typed scalars: plain ints would build an int64 array first

    # Optional edge smoothing, feathers the transition
    if edge_smooth > 0:
        alpha_img = Image.fromarray(alpha)
        alpha_img = alpha_img.filter(ImageFilter.GaussianBlur(radius=edge_smooth))
        alpha = np.array(alpha_img)

    # keep anything the source already made transparent, putalpha would undo it
    alpha = np.minimum(alpha, data[:, :, 3])

    img.putalpha(Image.fromarray(alpha))
    return img


# Uploaded images, kept in memory only, so tuning a slider or downloading the full size does not upload the
# file again, and switching the background of an AI cutout does not run the model again.
# ponytail: one dict behind a lock, capped by bytes and age. A real cache only if this ever runs as several processes.
CACHE_BYTES = 150 * 1024 * 1024
CACHE_SECONDS = 30 * 60
_cache = OrderedDict()  # id -> {"src": upload bytes, "ai": AI cutout as PNG bytes or None, "t": last use}
_cache_lock = threading.Lock()


def _cache_trim():
    """Call with the lock held. Drops what is too old, then the oldest until the rest fits. The newest always stays."""
    now = time.monotonic()
    for key in [k for k, e in _cache.items() if now - e["t"] > CACHE_SECONDS]:
        del _cache[key]
    while len(_cache) > 1 and sum(len(e["src"]) + len(e["ai"] or b"") for e in _cache.values()) > CACHE_BYTES:
        _cache.popitem(last=False)


def cache_put(entry):
    key = secrets.token_urlsafe(16)
    with _cache_lock:
        _cache[key] = entry
        _cache_trim()
    return key


def cache_get(key):
    with _cache_lock:
        _cache_trim()
        entry = _cache.get(key)
        if entry:
            entry["t"] = time.monotonic()
            _cache.move_to_end(key)
        return entry


def _png(img, max_side=0):
    if max_side and max(img.size) > max_side:
        img.thumbnail((max_side, max_side))  # in place: every caller hands over an image nobody else holds
    buf = io.BytesIO()
    img.save(buf, "PNG", compress_level=3 if max_side else 6)  # previews favor speed, downloads favor size
    return buf.getvalue()


def _release():
    """Hand freed memory back to the system. Without this glibc keeps what a 40MP job used, and the peaks of
    successive jobs add up toward the container limit. Linux only, a no-op elsewhere."""
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


def _work(entry, mode, bg, tolerance, smooth, edges, max_side):
    try:
        return _cutout(entry, mode, bg, tolerance, smooth, edges, max_side)
    finally:
        _release()


def _cutout(entry, mode, bg, tolerance, smooth, edges, max_side):
    if mode == "ai":
        cut = None
        if entry["ai"] is None:
            cut = remove_bg_ai(_decode(entry["src"]))
            entry["ai"] = _png(cut)
        if bg == "transparent" and not max_side:
            return entry["ai"]
        cut = cut or Image.open(io.BytesIO(entry["ai"]))
    else:
        cut = remove_bg_color(_decode(entry["src"]), tolerance, smooth, edges)
    return _png(flatten(cut, bg), max_side)


# One removal at a time: each can take over a gigabyte, and the container has 3g.
_cpu = asyncio.Lock()
# Requests that bring data with them (an upload, or a link to fetch), each holding up to 100MB in memory while
# they wait. Requests that name an image already on the server hold nothing and are not counted.
MAX_BUSY = 2  # measured: two 90MB uploads waiting behind a 30MP AI job peak near 2.5GB of the 3g
_busy = 0
BODY_SECONDS = 600  # a slot is not held longer than this by an upload that never finishes


def _fail(e):
    return JSONResponse({"error": str(e), "hint": e.hint}, status_code=e.status, headers={"Cache-Control": "no-store"})


def limited(handler):
    async def wrapped(request):
        global _busy
        # Decided by what the handler will do, not by whether the word "id" appears: an empty id still uploads.
        counted = request.url.path != "/api/remove" or not request.query_params.get("id")
        if counted and _busy >= MAX_BUSY:
            # Let the upload finish arriving, unkept. Answering while the browser is still sending makes it report
            # a dropped connection, and the person would never see why.
            try:
                async with asyncio.timeout(30):
                    async for _ in request.stream():
                        pass
            except (TimeoutError, ClientDisconnect):
                pass
            return _fail(UserError("The server is busy", "Try again in a moment.", 503))
        _busy += counted
        try:
            return await handler(request)
        except UserError as e:
            return _fail(e)
        except ClientDisconnect:  # the page went away mid-upload, nobody is left to answer
            return Response(status_code=204)
        finally:
            _busy -= counted

    return wrapped


async def _body(request, limit, too_big):
    """The request body, refused as soon as it is known to be over the limit."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise too_big
    chunks, size = [], 0
    try:
        async with asyncio.timeout(BODY_SECONDS):
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise too_big
                chunks.append(chunk)
    except TimeoutError:
        raise UserError("The upload took too long", "Try again on a faster connection.", 408)
    # bytes, not bytearray: every later io.BytesIO(data) then shares this buffer where it would copy a bytearray
    return b"".join(chunks)


def _number(query, name, default, low, high):
    try:
        value = float(query.get(name, default))
    except ValueError:
        value = default
    return min(high, max(low, value)) if math.isfinite(value) else default


@limited
async def api_remove(request):
    """POST an image as the body, or ?id= of one sent before. Answers with a PNG and its id in X-Image-Id."""
    q = request.query_params
    mode, bg = q.get("mode", "ai"), q.get("bg", "transparent")
    if mode not in ("ai", "color") or bg not in ("transparent", "white", "black"):
        raise UserError("Unknown option")
    tolerance = _number(q, "tolerance", 30, 5, 120)
    smooth = _number(q, "smooth", 1, 0, 5)
    max_side = int(_number(q, "max", 0, 0, 4096))
    edges = q.get("edges", "1") != "0"

    key = q.get("id")
    if key:
        entry = cache_get(key)
        if entry is None:
            raise UserError("That image is no longer on the server", "Drop it again.", 410)
    else:
        data = await _body(request, MAX_UPLOAD_BYTES, UserError("That file is over 100 MB", "Scale it down and drop it again.", 413))
        if not data:
            raise UserError("No image arrived", "Drop it again.")
        entry = {"src": data, "ai": None, "t": time.monotonic()}

    async with _cpu:
        if await request.is_disconnected():  # the page moved on while this waited its turn
            return Response(status_code=204)
        png = await run_in_threadpool(_work, entry, mode, bg, tolerance, smooth, edges, max_side)
    if not key:
        if await request.is_disconnected():  # nobody would ever learn the id, so it must not take up cache
            return Response(status_code=204)
        key = cache_put(entry)  # only images that decoded are kept
    return Response(png, media_type="image/png", headers={"X-Image-Id": key, "Cache-Control": "no-store"})


@limited
async def api_fetch(request):
    """POST a URL as the body. Answers with the image behind it, so the page can treat it like a dropped file."""
    url = (await _body(request, 4096, UserError("That link is too long"))).decode("utf-8", "replace")
    data = await run_in_threadpool(fetch_url, url)
    fmt = _open(data).format
    mime = "image/jpeg" if fmt == "MPO" else Image.MIME[fmt]
    return Response(data, media_type=mime, headers={"Cache-Control": "no-store"})


STATIC = Path(__file__).parent / "static"


async def index(request):
    return FileResponse(STATIC / "index.html")


async def healthz(request):
    return Response("ok", media_type="text/plain")


# user:password turns on HTTP Basic auth. Unset means no login, for use behind a proxy that has one.
AUTH = os.getenv("APP_AUTH", "")

SECURITY_HEADERS = [
    # Everything the page needs comes from this server. blob: is for showing the dropped image and the result.
    (b"content-security-policy", b"default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob:; "
                                 b"connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"cache-control", b"no-cache"),  # revalidate the page and its files, so a deploy shows up at once
]


def _authorized(header):
    scheme, _, value = header.partition(" ")
    try:
        given = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return False
    return scheme.lower() == "basic" and secrets.compare_digest(given, AUTH.encode())


def _same_origin(headers):
    """Browsers label where a request comes from. Another site must not be able to make this server work or fetch."""
    site = headers.get("sec-fetch-site")
    if site:
        return site in ("same-origin", "none")
    origin = headers.get("origin")
    return not origin or urllib.parse.urlsplit(origin).netloc == headers.get("host")


class Guard:
    """Login, the same-origin rule and the security headers, in front of every route."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                sent = {k.lower() for k, _ in message.get("headers", [])}
                extra = [h for h in SECURITY_HEADERS if h[0] not in sent]
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        if AUTH and scope["path"] != "/healthz" and not _authorized(headers.get("authorization", "")):
            denied = Response("Sign in to use nobg.", 401, {"WWW-Authenticate": 'Basic realm="nobg", charset="UTF-8"'})
            return await denied(scope, receive, send_with_headers)
        if scope["method"] not in ("GET", "HEAD") and not _same_origin(headers):
            return await _fail(UserError("Cross-site request refused", status=403))(scope, receive, send_with_headers)
        await self.app(scope, receive, send_with_headers)


app = Guard(Starlette(routes=[
    Route("/", index),
    Route("/healthz", healthz),
    Route("/api/remove", api_remove, methods=["POST"]),
    Route("/api/fetch", api_fetch, methods=["POST"]),
    Mount("/static", StaticFiles(directory=STATIC)),
]))


if __name__ == "__main__":
    port = int(os.getenv("PORT", "7860"))
    # Local runs stay on this machine. The image sets HOST=0.0.0.0 so the proxy can reach the container.
    host = os.getenv("HOST", "127.0.0.1")
    if host == "127.0.0.1":
        threading.Timer(1, webbrowser.open, [f"http://localhost:{port}"]).start()
    threading.Thread(target=_rembg, daemon=True).start()
    uvicorn.run(app, host=host, port=port, server_header=False)
