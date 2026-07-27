# OCR third-party notices

This file is a distribution inventory, not a replacement for the license texts
or legal review.

| Component | Locked version | Declared license / terms |
| --- | --- | --- |
| PaddleOCR | 3.5.0 | Apache-2.0 |
| PaddleX | 3.5.2 | Apache-2.0 |
| PaddlePaddle | 3.3.0 | Apache-2.0 |
| PP-OCRv5 server det/rec models | fixed SHA256 | PaddleOCR distribution terms; bundled PaddleOCR license |
| FastAPI | 0.140.0 | MIT |
| Uvicorn | 0.51.0 | BSD-3-Clause |
| Pydantic / pydantic-settings | 2.13.4 / 2.14.2 | MIT |
| OpenCV Python wheel | 4.10.0.84 | Apache-2.0 |
| NVIDIA CUDA/cuDNN base layers | CUDA 12.6.3 / cuDNN 9.5.1.17 | NVIDIA container and component terms |
| Ubuntu base packages | 22.04 image layer | Per-package licenses |

All 59 Python distributions and versions are frozen in `requirements.lock`;
their exact wheel hashes are frozen in `WHEELS.sha256`. The release bundle must
also contain the generated CycloneDX SBOM, the PaddleOCR license text, this
notice, and the NVIDIA container license copied from the pinned base image.

The project itself intentionally has no repository-level open-source license.
