# Local Background Remover

Gradio UI over `rembg`. Two removal modes: AI (isnet-general-use, for complex
backgrounds) and color-threshold (pixel-perfect on solid-color product shots).

Why not birefnet-general: 1GB download and 8–13GB peak RAM — it was OOM-killed
on the VPS ("Connection to the server was lost"). isnet peaks ~1.6GB.

## Run locally

```bash
.venv/bin/python remove_bg.py      # opens http://localhost:7860
.venv/bin/python test_remove_bg.py # self-check on the color-mask math
```

The AI model (~170MB) downloads on first *AI* use, not at startup — color mode
never touches it.

## Deploy

A push to `main` runs `.github/workflows/deploy.yml`: GitHub builds the image (x86_64, so nothing to cross-build on
the Mac), pushes it to `ghcr.io/pyvnoaim/nobg` (`:latest` + `:sha-<short>`), then calls the Dokploy deploy webhook
(repo secret `DOKPLOY_WEBHOOK`). Dokploy pulls `:latest` and restarts. `docker-compose.yml` is exactly the file
Dokploy runs (Compose > Raw); `docker-compose.local.yml` is for running it on the Mac. Rollback: point the image in
Dokploy at an older `:sha-` tag and deploy.

## Reaching it

The container publishes **no host port**. It joins the external `websites`
network and the proxy routes to it by container name. Whatever the proxy is,
it needs what the NPM settings below spell out: websockets/SSE unbuffered,
100 MB uploads, 300 s timeouts, and a login in front.

NPM → Hosts → Proxy Hosts → Add Proxy Host:

- Domain: your subdomain (use a subdomain, not a subpath — a subpath needs
  Gradio's `root_path` set as well)
- Scheme `http`, Forward Hostname `remove-bg`, Forward Port `7860`
- **Websockets Support: ON** — Gradio's event queue needs it; without it the UI
  loads but every button hangs
- SSL tab: request a Let's Encrypt cert, Force SSL
- Advanced tab:

  ```nginx
  client_max_body_size 100m;   # default rejects anything but tiny images
  proxy_read_timeout 300s;     # AI mode on CPU can exceed the 60s default
  proxy_send_timeout 300s;
  proxy_buffering off;         # Gradio's queue is SSE — buffering breaks it
  proxy_cache off;             # symptom: "Connection errored out" after upload
  ```

Put an NPM **Access List** (basic auth) on the proxy host — it's public once it
has a domain. Details tab: leave *Satisfy Any* and *Pass Auth to Upstream* off.
Authorizations tab: add the user/password. **Rules tab: add `Allow 0.0.0.0/0`** —
leaving it empty does *not* mean "any IP"; NPM appends a `deny all` and you get a
flat `403` for everyone, credentials or not. Correct setup answers `401` with a
`www-authenticate` header. Alternatively set `APP_AUTH=user:password` in the Dokploy env for
Gradio's own login; the Access List is easier since it needs no rebuild.

To debug without NPM, temporarily add `ports: ["127.0.0.1:7860:7860"]` to the
stack and tunnel in with
`ssh -L 7860:localhost:7860 user@host`.

## Volumes

- `models:/models` — model cache (`U2NET_HOME`), so the 170MB survives restarts
