# A named stage, so Dependabot sees the uv pin. It only reads FROM lines.
FROM ghcr.io/astral-sh/uv:0.12.15 AS uv

FROM python:3.12-slim

# libgomp1 is the one system library the installed wheels still link against (ldd scan on amd64).
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=uv /uv /bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-cache
ENV PATH="/app/.venv/bin:$PATH"
COPY remove_bg.py .

ENV U2NET_HOME=/models
# Run as a fixed non-root uid/gid. numba (via pymatting) caches under the home directory.
# /models is owned by that uid so a new or empty volume mounted there becomes writable.
RUN useradd --uid 1000 --create-home app && install -d -o app -g app /models
USER app
EXPOSE 7860
CMD ["python", "remove_bg.py"]
