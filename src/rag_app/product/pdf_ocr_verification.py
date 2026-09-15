"""把 PDF 来源身份、区域渲染和 PP-OCRv6 结果接到回答门禁。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from rag_app.adapters.parsers.pdf import recognized_contains, render_pdf_region
from rag_app.adapters.parsers.pdf.critical_ocr import CriticalOcrRead
from rag_app.core.errors import RagError
from rag_app.core.models import (
    AnswerClaim,
    EvidenceItem,
    OcrClaimVerification,
    OcrVerificationState,
    ProviderCall,
    SourceAnchor,
)

_MAX_REGIONS_PER_CLAIM = 4


class CriticalOcrAdapter(Protocol):
    """本地与官方 PP-OCRv6 Adapter 的最小共享形状。"""

    def recognize(self, image: bytes) -> CriticalOcrRead:
        """识别一个已裁剪 PNG。"""
        ...

    def close(self) -> None:
        """关闭当前 Adapter 拥有的资源。"""
        ...


SourcePdfLoader = Callable[[str, str], bytes]
CriticalOcrAdapterFactory = Callable[[], CriticalOcrAdapter]


class ProductCriticalOcrVerifier:
    """每次查询最多复核四个最终 Claim 来源区域并缓存同一区域。"""

    def __init__(
        self,
        source_loader: SourcePdfLoader,
        adapter_factory: CriticalOcrAdapterFactory,
    ) -> None:
        self._source_loader = source_loader
        self._adapter_factory = adapter_factory
        self._cache: dict[
            tuple[object, ...], tuple[OcrVerificationState, str]
        ] = {}
        self._pdf_cache: dict[tuple[str, str], bytes] = {}

    def verify(  # noqa: PLR0912
        self,
        claim: AnswerClaim,
        evidence: tuple[EvidenceItem, ...],
        critical_atoms: tuple[str, ...],
    ) -> OcrClaimVerification:
        """复核 Claim 引用中实际承载关键原子的 PDF block。

        Args:
            claim: 已通过现有 Grounded Claim 校验的最终事实。
            evidence: Claim 引用的 PDF 视觉解析证据。
            critical_atoms: 大小写与符号必须保持的原子。

        Returns:
            一致、冲突或无法证明的脱敏结果。

        """
        requests = _verification_requests(claim, evidence, critical_atoms)
        support_quotes = {
            support.support_id: support.quote for support in claim.supports
        }
        support_ids = tuple(
            item.evidence_id
            for item in evidence
            if any(
                atom in support_quotes.get(item.evidence_id, "")
                for atom in critical_atoms
            )
        )
        covered_atoms = {
            atom
            for _support, _item, atoms, _anchor in requests
            for atom in atoms
        }
        if (
            not requests
            or len(requests) > _MAX_REGIONS_PER_CLAIM
            or any(atom not in covered_atoms for atom in critical_atoms)
        ):
            return _result(
                support_ids or tuple(item.evidence_id for item in evidence),
                OcrVerificationState.UNVERIFIED,
                "OCR_VERIFICATION_REGION_UNAVAILABLE",
            )
        calls: list[ProviderCall] = []
        adapter: CriticalOcrAdapter | None = None
        outcome = OcrVerificationState.VERIFIED
        reason = "OCR_CRITICAL_ATOMS_MATCH"
        try:
            for _support_id, item, atoms, anchor in requests:
                key = (
                    item.document_version_id,
                    anchor.page_index,
                    anchor.bbox,
                    atoms,
                )
                cached = self._cache.get(key)
                if cached is not None:
                    state, state_reason = cached
                else:
                    try:
                        image = self._render(item, anchor)
                        if adapter is None:
                            adapter = self._adapter_factory()
                        read = adapter.recognize(image)
                        calls.append(read.call)
                        if read.text is None:
                            state = OcrVerificationState.UNVERIFIED
                            state_reason = read.reason_code
                        elif all(
                            recognized_contains(read.text, atom)
                            for atom in atoms
                        ):
                            state = OcrVerificationState.VERIFIED
                            state_reason = "OCR_CRITICAL_ATOMS_MATCH"
                        else:
                            state = OcrVerificationState.CONFLICT
                            state_reason = "OCR_CRITICAL_ATOMS_CONFLICT"
                    except (RagError, RuntimeError, ValueError, LookupError):
                        state = OcrVerificationState.UNVERIFIED
                        state_reason = "OCR_VERIFICATION_REGION_UNAVAILABLE"
                    self._cache[key] = (state, state_reason)
                if state is OcrVerificationState.CONFLICT:
                    outcome = state
                    reason = state_reason
                    break
                if state is OcrVerificationState.UNVERIFIED:
                    outcome = state
                    reason = state_reason
        finally:
            if adapter is not None:
                adapter.close()
        return _result(support_ids, outcome, reason, tuple(calls))

    def _render(self, item: EvidenceItem, anchor: SourceAnchor) -> bytes:
        if (
            item.document_id is None
            or item.document_version_id is None
            or anchor.page_index is None
            or anchor.page_width is None
            or anchor.page_height is None
            or anchor.bbox is None
        ):
            raise LookupError("PDF 复核来源身份或 bbox 不完整。")
        source_key = (item.document_id, item.document_version_id)
        content = self._pdf_cache.get(source_key)
        if content is None:
            content = self._source_loader(*source_key)
            self._pdf_cache[source_key] = content
        return render_pdf_region(
            content,
            page_index=anchor.page_index,
            page_width=anchor.page_width,
            page_height=anchor.page_height,
            bbox=anchor.bbox,
        )


def _verification_requests(
    claim: AnswerClaim,
    evidence: tuple[EvidenceItem, ...],
    atoms: tuple[str, ...],
) -> tuple[tuple[str, EvidenceItem, tuple[str, ...], SourceAnchor], ...]:
    support_quotes = {
        support.support_id: support.quote for support in claim.supports
    }
    requests = []
    for item in evidence:
        quote = support_quotes.get(item.evidence_id, "")
        relevant = tuple(atom for atom in atoms if atom in quote)
        anchor = next(
            (
                span.source_anchor
                for span in item.source_spans
                if span.source_anchor is not None
                and span.source_anchor.page_index is not None
                and span.source_anchor.bbox is not None
            ),
            None,
        )
        if relevant and anchor is not None:
            requests.append((item.evidence_id, item, relevant, anchor))
    return tuple(requests)


def _result(
    support_ids: tuple[str, ...],
    state: OcrVerificationState,
    reason_code: str,
    calls: tuple[ProviderCall, ...] = (),
) -> OcrClaimVerification:
    return OcrClaimVerification(
        state=state,
        support_states=tuple((support_id, state) for support_id in support_ids),
        provider_calls=calls,
        reason_code=reason_code,
    )


__all__ = ["ProductCriticalOcrVerifier"]
