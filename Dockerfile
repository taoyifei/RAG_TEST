# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

ARG NODE_IMAGE=node:24-alpine@sha256:e67514e5d0f6c46656005e1b693b2ec9d52e80b641307de684d4a015ba7a4eaf
ARG PYTHON_IMAGE=python:3.11-slim-trixie@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534

FROM ${NODE_IMAGE} AS frontend-build

WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
COPY docs/public/openapi-v1.json /build/docs/public/openapi-v1.json
RUN npm run build

FROM ${PYTHON_IMAGE} AS python-build

ARG VCS_REF=development-unset
WORKDIR /build
COPY pyproject.toml ./
COPY src/ ./src/
RUN python -c \
    'import pathlib, sys; pathlib.Path("src/rag_app/_build_revision.py").write_text(f"SOURCE_REVISION = {sys.argv[1]!r}\n", encoding="utf-8")' \
    "${VCS_REF}" \
    && python -m pip wheel \
    --disable-pip-version-check \
    --no-cache-dir \
    --no-deps \
    --wheel-dir=/wheels \
    .

FROM ${PYTHON_IMAGE} AS runtime

ARG VCS_REF=development-unset
LABEL org.opencontainers.image.revision="${VCS_REF}"

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.runtime.lock ./
COPY --from=python-build /wheels /wheels
RUN apt-get update \
    && apt-get install --yes --no-install-recommends antiword=0.37-17 \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install \
    --disable-pip-version-check \
    --no-cache-dir \
    --requirement=/app/requirements.runtime.lock \
    && python -m pip install \
    --disable-pip-version-check \
    --no-cache-dir \
    --no-deps \
    /wheels/docx_rag-*.whl \
    && python -m pip check \
    && python -c \
    'import sys; from rag_app._build_revision import SOURCE_REVISION; expected = sys.argv[1]; sys.exit(0 if expected == "development-unset" or SOURCE_REVISION == sys.argv[1] else 1)' \
    "${VCS_REF}" \
    && rm -rf /wheels \
    && python -m pip uninstall --yes pip setuptools wheel \
    && groupadd --gid 10001 rag \
    && useradd --uid 10001 --gid rag --no-create-home rag \
    && mkdir -p /data /run/rag-secrets \
    && chown -R rag:rag /data /run/rag-secrets

COPY --from=frontend-build --chown=rag:rag /build/frontend/dist/ ./frontend/
COPY --chown=rag:rag migrations/ ./migrations/
COPY --chown=rag:rag compatibility-manifest.json ./compatibility-manifest.json
COPY --chown=rag:rag evaluation/__init__.py evaluation/p11_pilot.py evaluation/p11_pilot_data.py evaluation/p11_pilot_runtime.py ./evaluation/
COPY --chown=rag:rag evaluation/v2/*.py ./evaluation/v2/
COPY --chown=rag:rag evaluation/gates/p08-gates.json ./evaluation/gates/p08-gates.json
COPY --chown=rag:rag evaluation/datasets/p11-pilot/ ./evaluation/datasets/p11-pilot/
RUN python -c \
    'from rag_app.product.compatibility import write_manifest; write_manifest("/app/compatibility-manifest.json")'

USER rag:rag
EXPOSE 8088
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=12 \
    CMD python -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8088/live', timeout=2)"
ENTRYPOINT ["rag-app"]
CMD ["serve"]
