# nobg

Background remover over `rembg`. `remove_bg.py` is the whole server (Starlette). `static/` is the whole page (plain HTML, CSS, JS, no build step).

## Commands

```bash
uv run remove_bg.py        # http://localhost:7860
uv run test_remove_bg.py   # self-check, no pytest
uv add <pkg> / uv lock     # deps live in pyproject.toml + uv.lock, both committed
```

## Constraints

- Model stays `isnet-general-use`. birefnet/bria: 1GB download, OOM-killed on the VPS.
- Container limit is 3g. AI mode peaks ~1.6GB on 12MP. Anything that raises peak RAM needs measuring.
- Images over 40MP are refused before decoding, and URL fetches dial only the IP they checked. Don't weaken either.
- Python 3.12, same in `pyproject.toml` and the `Dockerfile`.
- The page loads nothing from other origins and has no inline script or style. The CSP in `remove_bg.py` enforces it.
- No accent color in the interface. Feedback comes from motion. Transitions and entrances share one easing curve (`--ease`), only the looping and error animations differ.

## Deploy

Push to `main` → self-check passes → GitHub builds x86_64 image → GHCR → Dokploy webhook. Never build on the VPS.
`docker-compose.yml` must stay identical to the copy pasted in Dokploy. `docker-compose.local.yml` is for the Mac.
