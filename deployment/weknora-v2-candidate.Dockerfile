# 仅在 8289 隔离验证中叠加已登记候选镜像；不构建或替换生产镜像。
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ARG VCS_REF
USER root
RUN python -c 'import pathlib,shutil; shutil.rmtree(pathlib.Path("/usr/local/lib/python3.11/site-packages/rag_app"))'
COPY --chown=rag:rag src/rag_app/ /usr/local/lib/python3.11/site-packages/rag_app/
COPY --chown=rag:rag migrations/universal_rag/0037_weknora_parent_passages.sql /app/migrations/universal_rag/0037_weknora_parent_passages.sql
COPY --chmod=755 vendor/weknora-chunker/bin/linux-amd64/wb-chunker /usr/local/bin/wb-chunker
COPY --chown=rag:rag vendor/weknora-chunker/LICENSE /app/licenses/WeKnora-LICENSE
COPY --chown=rag:rag frontend/dist/ /app/frontend/
RUN python -c 'import pathlib,sys; pathlib.Path("/usr/local/lib/python3.11/site-packages/rag_app/_build_revision.py").write_text("SOURCE_REVISION = " + repr(sys.argv[1]) + "\n", encoding="utf-8")' "${VCS_REF}" \
    && python -c 'from rag_app.product.compatibility import write_manifest; write_manifest("/app/compatibility-manifest.json")' \
    && python -c 'import pathlib,sys; from rag_app.product.asset_manifest import write_product_asset_manifest; write_product_asset_manifest(root=pathlib.Path("/app"), manifest_path=pathlib.Path("/app/product-assets.json"), source_revision=sys.argv[1])' "${VCS_REF}" \
    && rag-app product-asset-selfcheck --expected-revision "${VCS_REF}" \
    && chown rag:rag /app/product-assets.json /app/compatibility-manifest.json
LABEL org.opencontainers.image.revision="${VCS_REF}"
USER rag:rag
