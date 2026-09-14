"""最终 PDF 高风险原子的有界二次 OCR 门禁。"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from rag_app.adapters.parsers.pdf.critical_ocr import CriticalOcrRead
from rag_app.application.answering.grounded import _verify_critical_ocr_claims
from rag_app.application.answering.ocr_guard import (
    claim_pdf_visual_evidence,
    critical_ocr_atoms,
)
from rag_app.core.errors import ValidationFailed
from rag_app.core.models import (
    AnswerClaim,
    ClaimSupport,
    EvidenceItem,
    OcrClaimVerification,
    OcrVerificationState,
    ProviderCall,
    SourceAnchor,
    SourceSpan,
    SourceSpanKind,
    StoryKind,
)
from rag_app.product.pdf_ocr_verification import ProductCriticalOcrVerifier


def _evidence(text: str) -> EvidenceItem:
    path = ("page:0", "block:critical")
    anchor = SourceAnchor(
        part_uri="/pdf/pages/0",
        story_kind=StoryKind.BODY,
        structural_path=path,
        ordinal=0,
        source_start_char=0,
        source_end_char=len(text),
        page_index=0,
        page_width=100,
        page_height=200,
        bbox=(5, 10, 95, 40),
        pdf_block_id="critical",
    )
    return EvidenceItem(
        evidence_id="S1",
        chunk_id=f"chunk_{'1' * 32}",
        citation_text=text,
        source_label="公开合成.pdf · PDF第1页 · PDF解析文字",
        source_spans=(
            SourceSpan(
                span_type=SourceSpanKind.PDF_PARSED_TEXT,
                node_id=f"node_{'2' * 32}",
                source_anchor=anchor,
                structural_path=path,
                chunk_start_char=0,
                chunk_end_char=len(text),
                source_start_char=0,
                source_end_char=len(text),
            ),
        ),
        document_id=f"doc_{'3' * 32}",
        document_version_id=f"dver_{'4' * 32}",
        display_name="公开合成.pdf",
        page_index=0,
        source_kind=SourceSpanKind.PDF_PARSED_TEXT,
        pdf_block_id="critical",
    )


def _claim(text: str) -> AnswerClaim:
    return AnswerClaim(
        text=text,
        supports=(ClaimSupport(support_id="S1", quote=text),),
    )


def _provider_call() -> ProviderCall:
    return ProviderCall(
        provider_id="conn_synthetic",
        operation="image.ocr.verify",
        call_count=1,
        retry_count=0,
        elapsed_ms=1,
        model="PP-OCRv6",
        status_category="success",
    )


@dataclass
class _Adapter:
    text: str | None
    call_count: int = 0
    close_count: int = 0

    def recognize(self, _image: bytes) -> CriticalOcrRead:
        self.call_count += 1
        return CriticalOcrRead(
            text=self.text,
            call=_provider_call(),
            reason_code=(
                "OCR_VERIFICATION_COMPLETED"
                if self.text is not None
                else "OCR_VERIFICATION_RESPONSE_INSUFFICIENT"
            ),
        )

    def close(self) -> None:
        self.close_count += 1


def test_critical_atom_detection_preserves_sign_unit_negation_and_version() -> (
    None
):
    text = "温度为 -12.5 °C，不得超过 25 mW，版本为 V2.3。"

    assert critical_ocr_atoms(text) == (
        "-12.5",
        "不得",
        "25 mW",
        "V2.3",
    )


def test_only_cited_pdf_visual_evidence_enters_secondary_ocr() -> None:
    evidence = _evidence("不得超过 25 mW。")
    unrelated = evidence.model_copy(
        update={
            "evidence_id": "S2",
            "chunk_id": f"chunk_{'5' * 32}",
        }
    )

    selected = claim_pdf_visual_evidence(
        _claim("不得超过 25 mW。"),
        (evidence, unrelated),
    )

    assert selected == (evidence,)


@pytest.mark.parametrize(
    ("recognized", "expected_state", "expected_reason"),
    [
        (
            "温度为 -12.5 °C，不得超过 25 mW，版本 V2.3",
            OcrVerificationState.VERIFIED,
            "OCR_CRITICAL_ATOMS_MATCH",
        ),
        (
            "温度为 12.5 °C，可以超过 25 MW，版本 V2.4",
            OcrVerificationState.CONFLICT,
            "OCR_CRITICAL_ATOMS_CONFLICT",
        ),
        (
            None,
            OcrVerificationState.UNVERIFIED,
            "OCR_VERIFICATION_RESPONSE_INSUFFICIENT",
        ),
    ],
)
def test_product_verifier_reports_match_conflict_or_unverified(
    monkeypatch: pytest.MonkeyPatch,
    recognized: str | None,
    expected_state: OcrVerificationState,
    expected_reason: str,
) -> None:
    text = "温度为 -12.5 °C，不得超过 25 mW，版本 V2.3。"
    evidence = _evidence(text)
    adapter = _Adapter(recognized)
    render_calls: list[tuple[bytes, int]] = []

    def render(content: bytes, **values: object) -> bytes:
        render_calls.append((content, int(values["page_index"])))
        return b"synthetic-crop"

    monkeypatch.setattr(
        "rag_app.product.pdf_ocr_verification.render_pdf_region",
        render,
    )
    verifier = ProductCriticalOcrVerifier(
        lambda _document, _version: b"synthetic-pdf",
        lambda: adapter,
    )
    claim = _claim(text)
    atoms = critical_ocr_atoms(text)

    result = verifier.verify(claim, (evidence,), atoms)
    cached = verifier.verify(claim, (evidence,), atoms)

    assert result.state is expected_state
    assert result.reason_code == expected_reason
    assert result.support_states == (("S1", expected_state),)
    assert len(render_calls) == 1
    assert adapter.call_count == 1
    assert adapter.close_count == 1
    assert cached.provider_calls == ()


def test_missing_local_verifier_stays_unverified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = "设备不得超过 25 mW。"
    evidence = _evidence(text)
    monkeypatch.setattr(
        "rag_app.product.pdf_ocr_verification.render_pdf_region",
        lambda *_args, **_kwargs: b"synthetic-crop",
    )
    verifier = ProductCriticalOcrVerifier(
        lambda _document, _version: b"synthetic-pdf",
        lambda: (_ for _ in ()).throw(RuntimeError("not configured")),
    )

    result = verifier.verify(
        _claim(text),
        (evidence,),
        critical_ocr_atoms(text),
    )

    assert result.state is OcrVerificationState.UNVERIFIED
    assert result.reason_code == "OCR_VERIFICATION_REGION_UNAVAILABLE"
    assert result.provider_calls == ()


@pytest.mark.parametrize(
    ("state", "expected_code"),
    [
        (OcrVerificationState.CONFLICT, "OCR_CRITICAL_ATOM_CONFLICT"),
        (OcrVerificationState.UNVERIFIED, "OCR_CRITICAL_ATOM_UNVERIFIED"),
    ],
)
def test_answer_publication_blocks_unverified_or_conflicting_atoms(
    state: OcrVerificationState,
    expected_code: str,
) -> None:
    text = "设备不得超过 25 mW。"
    evidence = _evidence(text)

    class Verifier:
        """返回固定复核终态的发布门替身。"""

        def verify(
            self,
            _claim_value: AnswerClaim,
            _evidence_value: tuple[EvidenceItem, ...],
            _atoms: tuple[str, ...],
        ) -> OcrClaimVerification:
            return OcrClaimVerification(
                state=state,
                support_states=(("S1", state),),
                provider_calls=(_provider_call(),),
                reason_code="SYNTHETIC_SECONDARY_OCR",
            )

    with pytest.raises(ValidationFailed) as captured:
        _verify_critical_ocr_claims(
            (_claim(text),),
            (evidence,),
            Verifier(),
            None,
        )

    assert captured.value.code == expected_code
    assert captured.value.provider_calls == (_provider_call(),)


def test_answer_publication_records_verified_support_state() -> None:
    text = "设备不得超过 25 mW。"
    evidence = _evidence(text)

    class Verifier:
        """返回已核验终态的发布门替身。"""

        def verify(
            self,
            _claim_value: AnswerClaim,
            _evidence_value: tuple[EvidenceItem, ...],
            _atoms: tuple[str, ...],
        ) -> OcrClaimVerification:
            return OcrClaimVerification(
                state=OcrVerificationState.VERIFIED,
                support_states=(("S1", OcrVerificationState.VERIFIED),),
                provider_calls=(_provider_call(),),
                reason_code="OCR_CRITICAL_ATOMS_MATCH",
            )

    calls, states = _verify_critical_ocr_claims(
        (_claim(text),),
        (evidence,),
        Verifier(),
        None,
    )

    assert calls == (_provider_call(),)
    assert states == (("S1", OcrVerificationState.VERIFIED),)
