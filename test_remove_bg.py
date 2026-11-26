"""Run: uv run test_remove_bg.py"""
import base64
import io
import json
import socket
import threading
import time
import urllib.error
import urllib.request
import warnings

import numpy as np
import uvicorn
from PIL import Image

import remove_bg
from remove_bg import UserError, _decode, _HttpOnlyRedirects, _PinnedHTTPConnection, fetch_url, flatten, remove_bg_color


def refused(fn, *args, why):
    """fn must refuse with a UserError: anything else, including success, fails the check."""
    try:
        fn(*args)
    except UserError:
        return
    raise AssertionError(why)


# White bg, red square in the middle.
img = np.full((40, 40, 3), 255, dtype=np.uint8)
img[10:30, 10:30] = (255, 0, 0)

out = remove_bg_color(img, tolerance=30, edge_smooth=0)
alpha = np.array(out)[:, :, 3]
assert alpha[0, 0] == 0, "corner background should be transparent"
assert alpha[20, 20] == 255, "subject should be opaque"

# White and black must composite onto that color, transparent must pass through untouched.
assert flatten(out, "transparent") is out
for bg, corner in (("white", (255, 255, 255)), ("black", (0, 0, 0))):
    flat = flatten(out, bg)
    assert flat.mode == "RGB" and flat.getpixel((0, 0)) == corner and flat.getpixel((20, 20)) == (255, 0, 0), bg

# An already-transparent input must stay transparent.
rgba = np.full((40, 40, 4), 255, dtype=np.uint8)
rgba[10:30, 10:30] = (255, 0, 0, 255)
rgba[:5, :5, 3] = 0                       # corner already transparent
out2 = remove_bg_color(rgba, tolerance=30, edge_smooth=0)
a2 = np.array(out2)[:, :, 3]
assert a2[2, 2] == 0, "pre-existing transparency must survive"
assert a2[20, 20] == 255, "subject must stay opaque"

# A background-colored patch enclosed by the subject (a highlight) is kept by default
# and only cleared with edges_only off.
hole = img.copy()
hole[18:22, 18:22] = 255
kept = np.array(remove_bg_color(hole, 30, 0))[:, :, 3]
assert kept[0, 0] == 0 and kept[20, 20] == 255, "enclosed patch must stay opaque by default"
assert kept[12, 12] == 255, "subject must stay opaque"
cleared = np.array(remove_bg_color(hole, 30, 0, False))[:, :, 3]
assert cleared[0, 0] == 0 and cleared[20, 20] == 0, "enclosed patch must clear with edges_only off"
assert cleared[12, 12] == 255, "subject must stay opaque"

# Subject covering one corner must not skew the sampled background color.
cov = np.full((40, 40, 3), 255, dtype=np.uint8)
cov[:20, :20] = (255, 0, 0)
ac = np.array(remove_bg_color(cov, 30, 0))[:, :, 3]
assert ac[0, 39] == 0 and ac[39, 0] == 0 and ac[39, 39] == 0, "uncovered corners should be transparent"
assert ac[2, 2] == 255, "subject in the corner should stay opaque"

# Same for a subject spanning the whole bottom edge, which covers two corners.
bust = np.full((40, 40, 3), 255, dtype=np.uint8)
bust[25:, :] = (255, 0, 0)
ab = np.array(remove_bg_color(bust, 30, 0))[:, :, 3]
assert ab[0, 0] == 0 and ab[0, 39] == 0, "background above the subject should be transparent"
assert ab[39, 0] == 255 and ab[39, 39] == 255, "subject along the bottom edge should stay opaque"


# SSRF guard: the fetch runs server-side from a public app, so nothing that
# resolves off the public internet may be reachable, including via a redirect.
for bad in ("http://127.0.0.1:9000/x.png", "http://localhost/x.png",
            "http://169.254.169.254/latest/meta-data/", "http://192.168.1.1/x.png",
            "http://10.0.3.2/x.png", "http://172.17.0.1/x.png", "http://[::1]/x.png",
            "http://[::ffff:127.0.0.1]/x.png", "http://2130706433/x.png",
            "file:///etc/passwd", "ftp://example.com/x.png"):
    refused(fetch_url, bad, why=f"SSRF guard let through: {bad}")

# Every connection, redirect hops included, is checked at connect time, not before.
refused(_PinnedHTTPConnection("127.0.0.1", 9, timeout=1).connect, why="pinned connection reached 127.0.0.1")

# DNS rebinding: the name is looked up once and the socket goes to exactly that checked address.
lookups, dialed = [], []
real_gai, real_cc = socket.getaddrinfo, socket.create_connection
socket.getaddrinfo = lambda *a, **k: (lookups.append(a[0]), [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))])[1]
socket.create_connection = lambda addr, *a, **k: (dialed.append(addr[0]), (_ for _ in ()).throw(OSError("stop")))[1]
try:
    _PinnedHTTPConnection("rebind.example", 80, timeout=1).connect()
except OSError:
    pass
finally:
    socket.getaddrinfo, socket.create_connection = real_gai, real_cc
assert lookups == ["rebind.example"] and dialed == ["93.184.216.34"], (lookups, dialed)

refused(_HttpOnlyRedirects().redirect_request, None, None, 302, "Found", {}, "file:///etc/passwd",
        why="redirect guard let through a file:// URL")


# A server that drips its answer a byte at a time, headers included, is cut off by one deadline for the whole fetch.
drip = socket.socket()
drip.bind(("127.0.0.1", 0))
drip.listen(1)


def drip_forever():
    conn, _ = drip.accept()
    conn.recv(4096)
    try:
        for byte in b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nX-Pad: " + b"a" * 5000:
            conn.send(bytes([byte]))
            time.sleep(0.2)
    except OSError:
        pass  # cut off, as intended


threading.Thread(target=drip_forever, daemon=True).start()
real_ip, real_seconds = remove_bg._public_ip, remove_bg.FETCH_SECONDS
remove_bg._public_ip, remove_bg.FETCH_SECONDS = (lambda host: "127.0.0.1"), 1
started = time.monotonic()
try:
    fetch_url(f"http://drip.test:{drip.getsockname()[1]}/x.png")
    raise AssertionError("a dripping server was read to the end")
except UserError as e:
    assert "too long" in str(e), e
finally:
    remove_bg._public_ip, remove_bg.FETCH_SECONDS = real_ip, real_seconds
assert time.monotonic() - started < 5, f"the fetch deadline took {time.monotonic() - started:.1f}s to fire"

# A connection opened after the deadline has fired, by a redirect that lands just past it, is refused and closed.
late = socket.socket()
remove_bg._fetching.socks, remove_bg._fetching.expired = [], threading.Event()
remove_bg._fetching.expired.set()
try:
    remove_bg._track(late)
    raise AssertionError("a socket opened after the deadline was accepted")
except OSError:
    assert late.fileno() == -1, "the late socket must be closed"
finally:
    remove_bg._fetching.socks, remove_bg._fetching.expired = [], None


def encoded(image, fmt="PNG", **options):
    buf = io.BytesIO()
    image.save(buf, fmt, **options)
    return buf.getvalue()


# Decompression bomb: a small file that declares more than 40MP is refused before decoding, by the explicit check
# and, behind it, by Pillow's hard error, which no warnings filter can switch off.
for side in (7000, 9500):  # 49MP and 90MP
    bomb = encoded(Image.new("1", (side, side)))
    refused(_decode, bomb, why=f"{side}x{side} image was decoded")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # what a library could do mid-request: the cap must still hold
        try:
            Image.open(io.BytesIO(bomb)).load()
            raise AssertionError(f"{side}x{side} image was decoded with warnings ignored")
        except Image.DecompressionBombError:
            pass
ok = _decode(encoded(Image.new("RGB", (300, 200), "red")))
assert ok.size == (300, 200) and ok.mode == "RGBA", "a normal image must still decode"

# Only formats a browser can show are decoded: TIFF and the other rarely used decoders stay out of reach.
refused(_decode, encoded(Image.new("RGB", (8, 8)), "TIFF"), why="a TIFF was decoded")
refused(_decode, b"not an image at all", why="junk was decoded")
refused(_decode, encoded(Image.new("RGB", (64, 64), "red"))[:60], why="a truncated PNG was decoded")

# Phone photos carry their rotation as a tag. The cutout must match what the browser shows.
exif = Image.Exif()
exif[0x0112] = 6  # rotated a quarter turn
assert _decode(encoded(Image.new("RGB", (40, 20), "red"), "JPEG", exif=exif)).size == (20, 40), "EXIF rotation ignored"

# The image cache drops the oldest once over its byte cap, and always keeps the newest.
cap = remove_bg.CACHE_BYTES
remove_bg.CACHE_BYTES = 250
first, second, third = (remove_bg.cache_put({"src": bytes(100), "ai": None, "t": time.monotonic()}) for _ in range(3))
assert remove_bg.cache_get(first) is None and remove_bg.cache_get(second) and remove_bg.cache_get(third), "cache cap"
huge = remove_bg.cache_put({"src": bytes(1000), "ai": None, "t": time.monotonic()})
assert remove_bg.cache_get(huge) and remove_bg.cache_get(third) is None, "newest must stay, the rest must go"
remove_bg.CACHE_BYTES = cap
remove_bg._cache.clear()


# The real server, over real HTTP.
probe = socket.socket()
probe.bind(("127.0.0.1", 0))
port = probe.getsockname()[1]
probe.close()
server = uvicorn.Server(uvicorn.Config(remove_bg.app, host="127.0.0.1", port=port, log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
while not server.started:
    time.sleep(0.02)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(path, data=None, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers or {})
    try:
        with opener.open(req, timeout=30) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


status, headers, page = call("/")
assert status == 200 and b"<title>nobg</title>" in page
csp = headers["Content-Security-Policy"]
assert "default-src 'none'" in csp and "script-src 'self'" in csp and "unsafe" not in csp, csp
assert headers["X-Content-Type-Options"] == "nosniff" and "frame-ancestors 'none'" in csp
assert call("/healthz")[0] == 200
for asset in ("app.js", "style.css", "icon.svg"):
    assert call(f"/static/{asset}")[0] == 200, asset
for escape in ("/static/../remove_bg.py", "/static/..%2fremove_bg.py", "/static/%2e%2e/pyproject.toml", "/remove_bg.py"):
    assert call(escape)[0] == 404, f"served a file outside static: {escape}"

sample = Image.fromarray(img)
status, headers, png = call("/api/remove?mode=color&tolerance=30&smooth=0", encoded(sample))
assert status == 200 and headers["Content-Type"] == "image/png", (status, png[:200])
cut = Image.open(io.BytesIO(png))
assert cut.mode == "RGBA" and cut.getpixel((0, 0))[3] == 0 and cut.getpixel((20, 20)) == (255, 0, 0, 255)
key = headers["X-Image-Id"]

# The id stands in for the upload: other settings, another background, a smaller preview.
status, _, png = call(f"/api/remove?mode=color&smooth=0&bg=black&id={key}", b"")
flat = Image.open(io.BytesIO(png))
assert status == 200 and flat.mode == "RGB" and flat.getpixel((0, 0)) == (0, 0, 0)
status, _, png = call(f"/api/remove?mode=color&max=256&id={key}", encoded(Image.new("RGB", (1000, 500), "white")))
assert Image.open(io.BytesIO(png)).size == (40, 40), "an id must win over a body, and small images are not upscaled"
status, headers, png = call("/api/remove?mode=color&max=256", encoded(Image.new("RGB", (1000, 500), "white")))
assert status == 200 and Image.open(io.BytesIO(png)).size == (256, 128), "preview size"
status, _, body = call("/api/remove?mode=color&id=nope", b"")
assert status == 410 and "error" in json.loads(body)

# AI mode with the model swapped out: it must run once per image, whatever is asked of the cutout afterwards.
runs = []
real_ai = remove_bg.remove_bg_ai
remove_bg.remove_bg_ai = lambda arr: (runs.append(1), remove_bg_color(arr, 30, 0))[1]
status, headers, png = call("/api/remove?mode=ai", encoded(sample))
assert status == 200 and Image.open(io.BytesIO(png)).getpixel((0, 0))[3] == 0
for query in ("bg=white", "bg=black&max=16", "bg=transparent"):
    assert call(f"/api/remove?mode=ai&{query}&id={headers['X-Image-Id']}", b"")[0] == 200
assert len(runs) == 1, f"the model ran {len(runs)} times for one image"
remove_bg.remove_bg_ai = real_ai

# What must be refused, each with a message the page can show.
for body, want, why in ((b"not an image", 415, "junk"), (b"", 400, "empty body"),
                        (encoded(Image.new("1", (7000, 7000))), 413, "49MP bomb"),
                        (encoded(Image.new("RGB", (8, 8)), "TIFF"), 415, "TIFF")):
    status, _, answer = call("/api/remove?mode=color", body)
    assert status == want and json.loads(answer)["error"], (why, status, answer[:200])
assert call("/api/remove?mode=evil", encoded(sample))[0] == 400
assert call("/api/remove?mode=color&bg=red", encoded(sample))[0] == 400
assert call("/api/remove?mode=color&tolerance=nan&smooth=1e999", encoded(sample))[0] == 200, "odd numbers fall back"
assert call("/api/remove?mode=color")[0] == 405, "GET must not run a removal"

limit = remove_bg.MAX_UPLOAD_BYTES
remove_bg.MAX_UPLOAD_BYTES = 1000
assert call("/api/remove?mode=color", bytes(5000))[0] == 413, "upload cap"
remove_bg.MAX_UPLOAD_BYTES = limit

remove_bg._busy = remove_bg.MAX_BUSY
assert call("/api/remove?mode=color", encoded(sample))[0] == 503, "busy cap"
assert call("/api/remove?mode=color&id=", encoded(sample))[0] == 503, "an empty id still uploads, so it must count"
assert call("/api/fetch?id=x", b"http://example.com/x.png")[0] == 503, "a fetch always counts"
assert call(f"/api/remove?mode=color&id={key}", b"")[0] == 200, "an image already here holds nothing and is let through"
remove_bg._busy = 0

# The URL fetch over HTTP keeps the same guard.
for bad in (b"http://127.0.0.1:1/x.png", b"http://169.254.169.254/latest/", b"file:///etc/passwd", b"x" * 5000):
    status, _, answer = call("/api/fetch", bad)
    assert status == 400 and json.loads(answer)["error"], (bad[:40], status, answer[:200])

# Another site must not be able to make this server work or fetch.
for hostile in ({"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}, {"Origin": "https://evil.example"}):
    assert call("/api/remove?mode=color", encoded(sample), hostile)[0] == 403, hostile
    assert call("/api/fetch", b"http://example.com/x.png", hostile)[0] == 403, hostile
for friendly in ({"Sec-Fetch-Site": "same-origin"}, {"Origin": f"http://127.0.0.1:{port}"}):
    assert call("/api/remove?mode=color", encoded(sample), friendly)[0] == 200, friendly

# APP_AUTH puts a login in front of everything except the health check.
remove_bg.AUTH = "leon:s3cret"


def basic(credentials):
    return {"Authorization": "Basic " + base64.b64encode(credentials.encode()).decode()}


status, headers, _ = call("/")
assert status == 401 and headers["WWW-Authenticate"].startswith("Basic"), status
assert call("/static/app.js")[0] == 401 and call("/api/remove?mode=color", encoded(sample))[0] == 401
for wrong in (basic("leon:wrong"), basic("leon"), basic(":"), {"Authorization": "Basic !!!"}, {"Authorization": "Bearer leon:s3cret"}):
    assert call("/", headers=wrong)[0] == 401, wrong
assert call("/", headers=basic("leon:s3cret"))[0] == 200
assert call("/api/remove?mode=color", encoded(sample), basic("leon:s3cret"))[0] == 200
assert call("/healthz")[0] == 200, "the container health check has no password"
remove_bg.AUTH = ""

server.should_exit = True

# rembg is imported on first AI use, so nothing above has touched it. A broken install must fail here, not in production.
ai = remove_bg._rembg()
assert callable(ai.remove) and callable(ai.new_session)

print("ok")
