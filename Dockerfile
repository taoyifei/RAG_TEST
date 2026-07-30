ARG PYTHON_IMAGE=python@sha256:86adf8dbadc3d6e82ee5dd2c74bec2e1c2467cdad47886280501df722372d2e1

FROM ${PYTHON_IMAGE} AS runtime

ARG VCS_REF

LABEL org.opencontainers.image.revision="${VCS_REF}"

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY deployment/runtime/wheelhouse /wheelhouse
COPY deployment/runtime/WHEELS.sha256 /wheelhouse/WHEELS.sha256
COPY requirements.runtime.lock ./
RUN test "$(printf '%s' "${VCS_REF}" | wc -c)" -eq 40 \
    && printf '%s' "${VCS_REF}" | grep -Eq '^[0-9a-f]{40}$' \
    && cd /wheelhouse \
    && sha256sum --check WHEELS.sha256 \
    && python -m pip install \
    --disable-pip-version-check \
    --no-cache-dir \
    --no-index \
    --find-links=/wheelhouse \
    --requirement=/app/requirements.runtime.lock \
    docx-rag==0.1.0 \
    && python -m pip check \
    && python -c \
    'import sys; from rag_app._build_revision import SOURCE_REVISION; sys.exit(0 if SOURCE_REVISION == sys.argv[1] else 1)' \
    "${VCS_REF}" \
    && rm -rf /wheelhouse \
    && groupadd --gid 10001 rag \
    && useradd --uid 10001 --gid rag --no-create-home rag \
    && mkdir -p /state \
    && chown rag:rag /state
COPY --chown=rag:rag frontend ./frontend
COPY --chown=rag:rag deployment/config ./deployment/config
COPY --chown=rag:rag deployment/assets ./deployment/assets
COPY --chown=rag:rag deployment/ASSETS.sha256 ./deployment/ASSETS.sha256

USER rag:rag
EXPOSE 8088
ENTRYPOINT ["rag-app"]
CMD ["serve"]
