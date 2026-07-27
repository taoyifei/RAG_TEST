ARG PYTHON_IMAGE=python@sha256:86adf8dbadc3d6e82ee5dd2c74bec2e1c2467cdad47886280501df722372d2e1

FROM ${PYTHON_IMAGE} AS runtime

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY deployment/wheelhouse /wheelhouse
COPY requirements.runtime.lock ./
RUN python -m pip install \
    --disable-pip-version-check \
    --no-cache-dir \
    --no-index \
    --find-links=/wheelhouse \
    docx-rag==0.1.0 \
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
