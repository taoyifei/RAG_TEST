FROM paddlepaddle/paddle:3.3.0-gpu-cuda12.6-cudnn9.5@sha256:bb84347b6365c2980347cf784fc8be3eaa903472f5c40129cb65aaa634ebd776

ARG TARGETARCH

ENV HOME=/tmp \
    HF_HUB_OFFLINE=1 \
    PADDLEOCR_HOME=/opt/rag-ocr/models \
    PADDLE_PDX_CACHE_HOME=/tmp/paddlex \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
    PYTHONPATH=/opt/rag-ocr/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/rag-ocr

COPY deployment/ocr/requirements.lock /tmp/requirements.lock
COPY deployment/ocr/WHEELS.sha256 /tmp/WHEELS.sha256
COPY deployment/ocr/assets/wheelhouse/ /tmp/wheelhouse/

RUN test "${TARGETARCH}" = "amd64" \
    && cd /tmp/wheelhouse \
    && sha256sum --check /tmp/WHEELS.sha256 \
    && python -c "import paddle; assert paddle.__version__ == '3.3.0'" \
    && python -m pip install \
        --disable-pip-version-check \
        --no-cache-dir \
        --no-index \
        --find-links=/tmp/wheelhouse \
        --requirement=/tmp/requirements.lock \
    && python -m pip check \
    && rm -rf /tmp/wheelhouse /tmp/requirements.lock /tmp/WHEELS.sha256

COPY deployment/ocr/assets/models/ /opt/rag-ocr/models/
COPY deployment/ocr/assets/licenses/ /opt/rag-ocr/licenses/
COPY deployment/ocr/pipeline.yaml /opt/rag-ocr/pipeline.yaml
COPY src/rag_app/__init__.py /opt/rag-ocr/src/rag_app/__init__.py
COPY src/rag_app/ocr/ /opt/rag-ocr/src/rag_app/ocr/

RUN groupadd --gid 10001 ragocr \
    && useradd \
        --uid 10001 \
        --gid 10001 \
        --no-create-home \
        --shell /usr/sbin/nologin \
        ragocr \
    && cp \
        /NGC-DL-CONTAINER-LICENSE \
        /opt/rag-ocr/licenses/NVIDIA-CONTAINER-LICENSE \
    && python -c "from rag_app.ocr.main import main; assert callable(main)" \
    && test -x /usr/bin/prlimit \
    && test -r /opt/rag-ocr/licenses/PaddleOCR-LICENSE.txt \
    && test -r /opt/rag-ocr/models/PP-OCRv5_server_det_infer/inference.json \
    && test -r /opt/rag-ocr/models/PP-OCRv5_server_rec_infer/inference.json

USER 10001:10001

EXPOSE 8090

HEALTHCHECK --interval=10s --timeout=3s --start-period=90s --retries=12 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/ready', timeout=2)"]

ENTRYPOINT ["python", "-m", "rag_app.ocr.main"]
