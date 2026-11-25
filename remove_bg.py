"""
Local Background Remover
========================
Run:
    uv run remove_bg.py

Then open http://localhost:7860 in your browser.
First run will download the model (about 180MB), after that it's instant.
"""

import gradio as gr
from gradio.themes import Monochrome  # gradio doesn't re-export `themes` on the package
from rembg import remove, new_session
from PIL import Image, ImageFilter
import numpy as np
from scipy import ndimage  # ships with rembg, which imports it itself
import http.client
import io
import ipaddress
import os
import socket
import urllib.parse
import urllib.request

# A tiny file can declare a huge canvas (a decompression bomb): the 50MB fetch cap and the upload limit only see the
# file, not the pixels. URL images get an explicit check in _decode. Uploads are decoded by Gradio, so they rely on
# Pillow's hard DecompressionBombError, which fires at twice MAX_IMAGE_PIXELS and, unlike the warning below that,
# can't be switched off by a library resetting the warnings filters under concurrent requests.
# ponytail: 40MP covers any camera photo; isnet peaked ~1.6GB at 12MP, raise with care against the 3g mem_limit.
MAX_PIXELS = 40_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS // 2

# isnet: about 180MB, 1024² input, mask upscaled to full res. birefnet-general is cleaner
# but a 1GB download and peaks at 8-13GB RAM. It got OOM-killed on the VPS.
MODEL = "isnet-general-use"
_session = None


def get_session():
    """Loaded on first AI use: color mode never needs the 180MB download."""
    global _session
    if _session is None:
        print(f"Loading model: {MODEL} (first run downloads about 180MB)...")
        _session = new_session(MODEL)
        print("Model ready.")
    return _session


def apply_output_format(result_rgba, output_format):
    if output_format == "PNG (transparent)":
        return result_rgba
    fill = (0, 0, 0, 255) if output_format == "Black background" else (255, 255, 255, 255)
    bg = Image.new("RGBA", result_rgba.size, fill)
    bg.paste(result_rgba, mask=result_rgba.split()[3])
    return bg.convert("RGB")


MAX_FETCH_BYTES = 50 * 1024 * 1024


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
        raise gr.Error("That URL has no host.")
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise gr.Error("Could not resolve that host.")
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise gr.Error("That host is not publicly routable.")
    return infos[0][4][0]


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.create_connection((_public_ip(self.host), self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        sock = socket.create_connection((_public_ip(self.host), self.port), self.timeout)
        # TLS and the Host header still use the name, only the TCP connection goes to the checked IP.
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _PinnedHTTP(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_PinnedHTTPConnection, req)


class _PinnedHTTPS(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PinnedHTTPSConnection, req, context=self._context)


class _HttpOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """A public URL can 302 anywhere; every hop connects through the pinned handlers above, so only the scheme is left."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith(("http://", "https://")):
            raise gr.Error("Redirect to a non-http scheme was blocked.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def load_url(url):
    """Dragging an image off a webpage yields its URL, not a file. Fetch it here."""
    url = (url or "").strip()
    if not url:
        return None
    if not url.startswith(("http://", "https://")):
        raise gr.Error("Only http:// and https:// URLs are supported.")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    # ProxyHandler({}): never route through an environment proxy, which would make the IP check meaningless.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _PinnedHTTP, _PinnedHTTPS, _HttpOnlyRedirects)
    with opener.open(req, timeout=15) as r:
        data = r.read(MAX_FETCH_BYTES + 1)
    if len(data) > MAX_FETCH_BYTES:
        raise gr.Error("Image is larger than 50MB.")
    return _decode(data)


def _decode(data):
    try:
        img = Image.open(io.BytesIO(data))  # reads the header only, nothing is decoded yet
    except Image.DecompressionBombError:
        raise gr.Error("Image is larger than 40 megapixels.")
    if img.width * img.height > MAX_PIXELS:
        raise gr.Error("Image is larger than 40 megapixels.")
    return np.array(img.convert("RGBA"))


def remove_bg_ai(input_image, output_format):
    """AI-based removal, good for complex/unknown backgrounds."""
    if input_image is None:
        return None
    img = Image.fromarray(input_image)
    result = remove(img, session=get_session())
    return apply_output_format(result, output_format)


def remove_bg_color(input_image, output_format, tolerance, edge_smooth, edges_only=True):
    """
    Color/threshold-based removal, pixel-perfect for solid color backgrounds.
    Samples the background color from the 4 corners and removes matching pixels:
    with edges_only just the ones connected to the image border, so a highlight
    in the background color inside the subject survives. Otherwise all of them.
    Works great for product shots on white/solid backgrounds.
    """
    if input_image is None:
        return None

    img = Image.fromarray(input_image).convert("RGBA")
    data = np.asarray(img)  # uint8 view, no float copy of the whole image: that tripled peak RAM at 40MP

    # Sample background color from corners (5x5 patch average). Median across the
    # four, so one corner covered by the subject doesn't skew it.
    corners = [
        data[:5, :5],
        data[:5, -5:],
        data[-5:, :5],
        data[-5:, -5:],
    ]
    bg_color = np.median([c.mean(axis=(0, 1)) for c in corners], axis=0)[:3]

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
    return apply_output_format(img, output_format)


# UI
THEME = Monochrome(
    spacing_size="md",
    radius_size="lg",
    text_size="sm",
    font=["ui-sans-serif", "system-ui", "sans-serif"],  # default is Lora, a serif
)

CSS = """
/* gradio versions the class: .gradio-container-6-20-0, match it whatever the version.
   width:100% is load-bearing: the parent <gradio-app> is a column flex, so the auto
   margins below cancel cross-axis stretch and the container shrink-wraps to ~500px. */
[class*="gradio-container"] {
    width: 100% !important;
    max-width: min(1600px, 94vw) !important;
    margin: 0 auto !important;
    padding: 2.5rem 0 4rem !important;
}
footer { display: none !important; }
h1 { font-size: 1.4rem; font-weight: 600; margin: 0 0 1.25rem; letter-spacing: -.015em; }
.tabs { margin-top: .5rem; }
.tabitem { padding-top: .75rem !important; }
/* the drop handler clicks this; visible=False would remove it from the DOM */
.url-load-btn, .url-relay { display: none !important; }

/* gradio's loader is an orange logo SVG + a "processing | 0.0s" timer.
   Swap both for a plain spinner that inherits the monochrome palette. */
[data-testid="status-tracker"]:not(.hide) {
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    background: var(--block-background-fill) !important;
    border: 0 !important;
    color: var(--body-text-color);
    animation: rbg-fade .25s ease both;
}
[data-testid="status-tracker"]:not(.hide) svg,
[data-testid="status-tracker"]:not(.hide) .progress-text { display: none !important; }
[data-testid="status-tracker"]:not(.hide)::after {
    content: "";
    width: 24px;
    height: 24px;
    border-radius: 50%;
    border: 2px solid rgba(127, 127, 127, .3);
    border-top-color: currentColor;
    animation: rbg-spin .65s linear infinite;
}
@keyframes rbg-spin { to { transform: rotate(360deg); } }
@keyframes rbg-fade { from { opacity: 0 } to { opacity: 1 } }
/* and let the finished image fade in rather than pop */
[data-testid="image"] img { animation: rbg-fade .3s ease both; }
"""

# Dragging an image off a webpage carries a URL, not a file, so gradio's dropzone
# rejects it, and gradio's own drag handlers swallow the drop before anything else
# sees it. Catch it first (capture phase) and feed the visible tab's URL field.
#
# Delivered via launch(head=...), NOT launch(js=...): gradio 6.20 accepts `js` and
# silently never runs it. The script is re-inserted after DOMContentLoaded has already
# fired, so it must self-init rather than wait for that event.
HEAD = """
<script>
(function init() {
  if (!document.body) { return setTimeout(init, 50); }
  const urlOf = (dt) => {
    if (!dt || (dt.files && dt.files.length)) return null;
    const raw = (dt.getData('text/uri-list') || dt.getData('text/plain') || '').trim();
    const first = raw.split('\\n').find(l => /^https?:\\/\\/\\S+$/.test(l.trim()));
    return first ? first.trim() : null;
  };
  document.addEventListener('dragover', (e) => {
    if (urlOf(e.dataTransfer)) { e.preventDefault(); e.stopPropagation(); }
  }, true);
  document.addEventListener('drop', (e) => {
    const url = urlOf(e.dataTransfer);
    if (!url) return;
    const pane = [...document.querySelectorAll('.tabitem')].find(p => p.offsetParent !== null);
    const relay = pane && pane.querySelector('.url-relay');
    const box = relay && relay.querySelector('textarea, input[type="text"]');
    if (!box) return;   // no URL relay on this tab, leave the drop alone
    e.preventDefault(); e.stopPropagation();
    const proto = box.tagName === 'TEXTAREA' ? HTMLTextAreaElement : HTMLInputElement;
    Object.getOwnPropertyDescriptor(proto.prototype, 'value').set.call(box, url);
    box.dispatchEvent(new Event('input', { bubbles: true }));
    // synthetic key events do not drive gradio's submit; click its hidden loader
    // button instead, after a tick so svelte has taken the new value
    setTimeout(() => {
      const host = pane.querySelector('.url-load-btn');
      const btn = host && (host.tagName === 'BUTTON' ? host : host.querySelector('button'));
      if (btn) btn.click();
    }, 250);
  }, true);
})();
</script>
"""

# delete_cache: uploads and results are swept hourly once they are a day old, instead of piling up until the next deploy.
with gr.Blocks(title="Background Remover", fill_width=True, delete_cache=(3600, 86400)) as app:
    gr.Markdown("# Background Remover")

    with gr.Tabs():

        with gr.Tab("AI"):
            gr.Markdown("Complex, gradient, or unknown backgrounds.")
            with gr.Row(equal_height=False):
                with gr.Column(min_width=260):
                    input_ai = gr.Image(label="Image", type="numpy", image_mode="RGBA", sources=["upload", "clipboard"])
                    url_ai = gr.Textbox(  # hidden relay for the drop handler; see HEAD
                        label="Image URL", elem_classes=["url-relay"]
                    )
                    fmt_ai = gr.Radio(
                        ["PNG (transparent)", "White background", "Black background"],
                        value="PNG (transparent)", label="Output format"
                    )
                    load_ai = gr.Button("load", elem_classes=["url-load-btn"])  # hidden via CSS: visible=False omits it from the DOM
                    btn_ai = gr.Button("Remove background", variant="primary")
                with gr.Column(min_width=260):
                    output_ai = gr.Image(label="Result", type="pil", image_mode="RGBA", format="png", interactive=False, sources=[])

            url_ai.submit(fn=load_url, inputs=url_ai, outputs=input_ai)
            load_ai.click(fn=load_url, inputs=url_ai, outputs=input_ai)
            btn_ai.click(fn=remove_bg_ai, inputs=[input_ai, fmt_ai], outputs=output_ai)

        with gr.Tab("Solid color"):
            gr.Markdown("Product shots on a solid background. Pixel-perfect, no artifacts.")
            with gr.Row(equal_height=False):
                with gr.Column(min_width=260):
                    input_col = gr.Image(label="Image", type="numpy", image_mode="RGBA", sources=["upload", "clipboard"])
                    url_col = gr.Textbox(  # hidden relay for the drop handler; see HEAD
                        label="Image URL", elem_classes=["url-relay"]
                    )
                    fmt_col = gr.Radio(
                        ["PNG (transparent)", "White background", "Black background"],
                        value="PNG (transparent)", label="Output format"
                    )
                    tolerance = gr.Slider(
                        minimum=5, maximum=120, value=30, step=1, label="Tolerance",
                        info="Raise if background remnants remain, lower if the subject is being eaten."
                    )
                    edge_smooth = gr.Slider(
                        minimum=0, maximum=5, value=1, step=0.5, label="Edge smoothing",
                        info="0 = hard edge."
                    )
                    edges_only = gr.Checkbox(
                        value=True, label="Only remove from the edges",
                        info="Keeps background-colored spots inside the subject. Turn off to also clear enclosed areas, like the gap in a mug handle."
                    )
                    load_col = gr.Button("load", elem_classes=["url-load-btn"])  # hidden via CSS: visible=False omits it from the DOM
                    btn_col = gr.Button("Remove background", variant="primary")
                with gr.Column(min_width=260):
                    output_col = gr.Image(label="Result", type="pil", image_mode="RGBA", format="png", interactive=False, sources=[])

            url_col.submit(fn=load_url, inputs=url_col, outputs=input_col)
            load_col.click(fn=load_url, inputs=url_col, outputs=input_col)
            btn_col.click(
                fn=remove_bg_color,
                inputs=[input_col, fmt_col, tolerance, edge_smooth, edges_only],
                outputs=output_col
            )

app.queue(default_concurrency_limit=1, max_size=20)

if __name__ == "__main__":
    user, _, password = os.getenv("APP_AUTH", "").partition(":")  # unset = no login
    app.launch(
        server_name="0.0.0.0",
        server_port=int(os.getenv("PORT", "7860")),
        inbrowser=not os.path.exists("/.dockerenv"),
        auth=(user, password) if user else None,
        max_file_size="100mb",  # same as the proxy's client_max_body_size
        theme=THEME,
        css=CSS,
        head=HEAD,
    )