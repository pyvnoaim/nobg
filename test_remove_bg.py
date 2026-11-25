"""Run: .venv/bin/python test_remove_bg.py"""
import numpy as np
from remove_bg import remove_bg_color, apply_output_format

# White bg, red square in the middle.
img = np.full((40, 40, 3), 255, dtype=np.uint8)
img[10:30, 10:30] = (255, 0, 0)

out = remove_bg_color(img, "PNG (transparent)", tolerance=30, edge_smooth=0)
alpha = np.array(out)[:, :, 3]
assert alpha[0, 0] == 0, "corner background should be transparent"
assert alpha[20, 20] == 255, "subject should be opaque"

# Non-PNG formats must composite, not fall through to None.
for fmt in ("White background", "Black background"):
    flat = apply_output_format(out, fmt)
    assert flat is not None and flat.mode == "RGB", fmt

# An already-transparent input must stay transparent — gradio's Image defaults to
# image_mode="RGB", which silently flattens alpha onto black.
rgba = np.full((40, 40, 4), 255, dtype=np.uint8)
rgba[10:30, 10:30] = (255, 0, 0, 255)
rgba[:5, :5, 3] = 0                       # corner already transparent
out2 = remove_bg_color(rgba, "PNG (transparent)", tolerance=30, edge_smooth=0)
a2 = np.array(out2)[:, :, 3]
assert a2[2, 2] == 0, "pre-existing transparency must survive"
assert a2[20, 20] == 255, "subject must stay opaque"


# SSRF guard: the fetch runs server-side from a public app, so nothing that
# resolves off the public internet may be reachable — including via a redirect.
import socket  # noqa: E402
from remove_bg import load_url, _public_ip, _HttpOnlyRedirects, _PinnedHTTPConnection, _decode  # noqa: E402

for bad in ("http://127.0.0.1:9000/x.png", "http://localhost/x.png",
            "http://169.254.169.254/latest/meta-data/", "http://192.168.1.1/x.png",
            "http://10.0.3.2/x.png", "http://172.17.0.1/x.png", "http://[::1]/x.png"):
    try:
        load_url(bad)
        raise AssertionError(f"SSRF guard let through: {bad}")
    except AssertionError:
        raise
    except Exception:
        pass

# Every connection, redirect hops included, is checked at connect time, not before.
try:
    _PinnedHTTPConnection("127.0.0.1", 9, timeout=1).connect()
    raise AssertionError("pinned connection reached 127.0.0.1")
except AssertionError:
    raise
except Exception:
    pass

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

hop = _HttpOnlyRedirects()
try:
    hop.redirect_request(None, None, 302, "Found", {}, "file:///etc/passwd")
    raise AssertionError("redirect guard let through a file:// URL")
except AssertionError:
    raise
except Exception:
    pass

# Decompression bomb: a small file that declares more than 40MP is refused before decoding,
# whether it comes from a URL (_decode) or an upload (Pillow's own limit, set in remove_bg).
import io  # noqa: E402
from PIL import Image  # noqa: E402
for side in (7000, 9500):  # 49MP: over the limit (warning turned into an error); 90MP: over twice the limit
    buf = io.BytesIO()
    Image.new("1", (side, side)).save(buf, "PNG")
    try:
        _decode(buf.getvalue())
        raise AssertionError(f"{side}x{side} image was decoded")
    except AssertionError:
        raise
    except Exception:
        pass

print("ok")
