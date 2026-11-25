# nobg

Self-hosted background remover with a web UI, built on [rembg](https://github.com/danielgatis/rembg) and [Gradio](https://www.gradio.app).

| Mode | Use it for | How it works |
| --- | --- | --- |
| AI | Complex or unknown backgrounds | `isnet-general-use` segmentation model |
| Color | Product shots on a solid background | Color threshold, pixel-exact, no model |

Images come from upload, clipboard, or a URL dragged in from another page.

## Run

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv run remove_bg.py        # http://localhost:7860
uv run test_remove_bg.py   # self-check
```

With Docker:

```bash
docker compose -f docker-compose.local.yml up --build
```

The AI model (about 180 MB) downloads on first use of AI mode. Color mode never needs it.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `APP_AUTH` | unset | `user:password` enables the built-in login |
| `U2NET_HOME` | `/models` in the image | Model cache directory |

## Limits

- Images over 40 megapixels or 50 MB are refused.
- URL fetches accept only `http` and `https` and only publicly routable hosts.
- AI mode peaks at about 1.6 GB RAM on a 12 MP image. The container limit is 3 GB.
- `birefnet-general` gives cleaner edges but needs 8 to 13 GB RAM, so it is not used.

## Deploy

A push to `main` runs `.github/workflows/deploy.yml`:

1. GitHub builds the `linux/amd64` image.
2. The image is pushed to `ghcr.io/pyvnoaim/nobg` as `:latest` and `:sha-<short>`.
3. The Dokploy webhook (repo secret `DOKPLOY_WEBHOOK`) triggers a redeploy.

`docker-compose.yml` is the exact file Dokploy runs. To roll back, point the image at an older `:sha-` tag and redeploy.

## Reverse proxy

The container publishes no host port. It joins the external `websites` network and is reached as `remove-bg:7860`.

The proxy must provide:

- WebSocket support. Without it the UI loads but every button hangs.
- Unbuffered responses, 100 MB request bodies, and 300 s timeouts.
- Authentication, unless `APP_AUTH` is set.

For nginx or Nginx Proxy Manager (Advanced tab):

```nginx
client_max_body_size 100m;
proxy_read_timeout 300s;
proxy_send_timeout 300s;
proxy_buffering off;
proxy_cache off;
```

With a Nginx Proxy Manager Access List, add `Allow 0.0.0.0/0` under Rules. An empty rule list denies everyone with a `403`.

To debug without the proxy, add `ports: ["127.0.0.1:7860:7860"]` to the stack and open an SSH tunnel to port 7860.
