FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    EVIDENCE_ROOT=/home/vitrial/evidence

RUN groupadd --system --gid 10001 vitrial \
    && useradd --system --uid 10001 --gid vitrial --create-home --home-dir /home/vitrial vitrial \
    && mkdir -p /home/vitrial/evidence

WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app
COPY scripts ./scripts
RUN pip install --no-cache-dir . \
    && chown -R vitrial:vitrial /app /home/vitrial
COPY alembic.ini ./
COPY migrations ./migrations

USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD ["python", "scripts/probe_dependencies.py", "--quiet"]

# NOTE on --forwarded-allow-ips: in production this API container publishes NO host port;
# its only TCP peer is the Caddy ingress on the internal network, so `*` is reachable only
# from Caddy. The deployment trust boundary is Caddyfile: it now overwrites
# X-Forwarded-For with {remote_host} rather than appending to a client-supplied value.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
