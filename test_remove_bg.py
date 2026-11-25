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
from remove_bg import _require_public_host, _PublicOnlyRedirects  # noqa: E402

for bad in ("http://127.0.0.1:9000/x.png", "http://localhost/x.png",
            "http://169.254.169.254/latest/meta-data/", "http://192.168.1.1/x.png",
            "http://10.0.3.2/x.png", "http://172.17.0.1/x.png", "http://[::1]/x.png"):
    try:
        _require_public_host(bad)
        raise AssertionError(f"SSRF guard let through: {bad}")
    except AssertionError:
        raise
    except Exception:
        pass

hop = _PublicOnlyRedirects()
for bad in ("http://127.0.0.1:9000/x", "file:///etc/passwd"):
    try:
        hop.redirect_request(None, None, 302, "Found", {}, bad)
        raise AssertionError(f"redirect guard let through: {bad}")
    except AssertionError:
        raise
    except Exception:
        pass

print("ok")
