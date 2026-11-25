"""Run: uv run test_remove_bg.py"""
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

# An already-transparent input must stay transparent: gradio's Image defaults to
# image_mode="RGB", which silently flattens alpha onto black.
rgba = np.full((40, 40, 4), 255, dtype=np.uint8)
rgba[10:30, 10:30] = (255, 0, 0, 255)
rgba[:5, :5, 3] = 0                       # corner already transparent
out2 = remove_bg_color(rgba, "PNG (transparent)", tolerance=30, edge_smooth=0)
a2 = np.array(out2)[:, :, 3]
assert a2[2, 2] == 0, "pre-existing transparency must survive"
assert a2[20, 20] == 255, "subject must stay opaque"

# A background-colored patch enclosed by the subject (a highlight) is kept by default
# and only cleared with edges_only off.
hole = img.copy()
hole[18:22, 18:22] = 255
kept = np.array(remove_bg_color(hole, "PNG (transparent)", 30, 0))[:, :, 3]
assert kept[0, 0] == 0 and kept[20, 20] == 255, "enclosed patch must stay opaque by default"
assert kept[12, 12] == 255, "subject must stay opaque"
cleared = np.array(remove_bg_color(hole, "PNG (transparent)", 30, 0, False))[:, :, 3]
assert cleared[0, 0] == 0 and cleared[20, 20] == 0, "enclosed patch must clear with edges_only off"
assert cleared[12, 12] == 255, "subject must stay opaque"

# Subject covering one corner must not skew the sampled background color.
cov = np.full((40, 40, 3), 255, dtype=np.uint8)
cov[:20, :20] = (255, 0, 0)
ac = np.array(remove_bg_color(cov, "PNG (transparent)", 30, 0))[:, :, 3]
assert ac[0, 39] == 0 and ac[39, 0] == 0 and ac[39, 39] == 0, "uncovered corners should be transparent"
assert ac[2, 2] == 255, "subject in the corner should stay opaque"

# Same for a subject spanning the whole bottom edge, which covers two corners.
bust = np.full((40, 40, 3), 255, dtype=np.uint8)
bust[25:, :] = (255, 0, 0)
ab = np.array(remove_bg_color(bust, "PNG (transparent)", 30, 0))[:, :, 3]
assert ab[0, 0] == 0 and ab[0, 39] == 0, "background above the subject should be transparent"
assert ab[39, 0] == 255 and ab[39, 39] == 255, "subject along the bottom edge should stay opaque"

# "PNG (transparent)" must download as PNG: gr.Image defaults to format="webp".
import remove_bg  # noqa: E402
for comp in (remove_bg.output_ai, remove_bg.output_col):
    assert comp.postprocess(out).path.endswith(".png"), comp.postprocess(out).path


# SSRF guard: the fetch runs server-side from a public app, so nothing that
# resolves off the public internet may be reachable, including via a redirect.
import socket  # noqa: E402
from remove_bg import load_url, _HttpOnlyRedirects, _PinnedHTTPConnection, _decode  # noqa: E402

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

# Decompression bomb: a small file that declares more than 40MP is refused before decoding, whether it comes from a
# URL (_decode's explicit check) or an upload (Pillow's hard error, which no warnings filter can switch off).
import io  # noqa: E402
import warnings  # noqa: E402
from PIL import Image  # noqa: E402
for side in (7000, 9500):  # 49MP and 90MP
    buf = io.BytesIO()
    Image.new("1", (side, side)).save(buf, "PNG")
    try:
        _decode(buf.getvalue())
        raise AssertionError(f"{side}x{side} image was decoded")
    except AssertionError:
        raise
    except Exception:
        pass
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # what a library could do mid-request: the upload cap must still hold
        try:
            Image.open(io.BytesIO(buf.getvalue())).load()
            raise AssertionError(f"{side}x{side} upload was decoded with warnings ignored")
        except Image.DecompressionBombError:
            pass
ok = io.BytesIO()
Image.new("RGB", (300, 200), "red").save(ok, "PNG")
assert _decode(ok.getvalue()).shape == (200, 300, 4), "a normal image must still decode"

print("ok")
