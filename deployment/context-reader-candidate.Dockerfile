# 仅用于已隔离的 8289 候选验证；从登记过的候选镜像复用运行时资产。
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ARG VCS_REF
USER root
RUN python -c 'import pathlib,shutil; shutil.rmtree(pathlib.Path("/usr/local/lib/python3.11/site-packages/rag_app"))'
COPY --chown=rag:rag src/rag_app/ /usr/local/lib/python3.11/site-packages/rag_app/
RUN python -c 'import pathlib,sys; pathlib.Path("/usr/local/lib/python3.11/site-packages/rag_app/_build_revision.py").write_text("SOURCE_REVISION = " + repr(sys.argv[1]) + "\n", encoding="utf-8")' "${VCS_REF}" \
    && python -c 'from rag_app.product.compatibility import write_manifest; write_manifest("/app/compatibility-manifest.json")' \
    && python -c 'import pathlib,sys; from rag_app.product.asset_manifest import write_product_asset_manifest; write_product_asset_manifest(root=pathlib.Path("/app"), manifest_path=pathlib.Path("/app/product-assets.json"), source_revision=sys.argv[1])' "${VCS_REF}" \
    && rag-app product-asset-selfcheck --expected-revision "${VCS_REF}" \
    && chown rag:rag /app/product-assets.json /app/compatibility-manifest.json
LABEL org.opencontainers.image.revision="${VCS_REF}"
USER rag:rag
