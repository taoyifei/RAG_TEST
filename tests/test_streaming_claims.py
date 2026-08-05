import json

import pytest

from rag_app.generation.streaming_claims import IncrementalClaimsParser


def test_incremental_claim_parser_waits_for_complete_claim_object() -> None:
    payload = json.dumps(
        {
            "claims": [
                {
                    "text": "需求变更应提交\"书面\"申请。",
                    "support_ids": ["E1:S1", "E1:S2"],
                },
                {
                    "text": "完成影响评估后更新需求基线。",
                    "support_ids": ["E2:S1"],
                },
            ]
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    parser = IncrementalClaimsParser(max_claims=4, max_buffer_chars=4096)
    emitted: list[dict[str, object]] = []

    for character in payload:
        emitted.extend(parser.feed(character))
        if not parser.claims:
            assert emitted == []

    parser.finish()

    assert emitted == json.loads(payload)["claims"]
    assert parser.claims == tuple(emitted)


def test_incremental_claim_parser_never_reads_an_unrelated_array() -> None:
    parser = IncrementalClaimsParser(max_claims=4, max_buffer_chars=4096)

    with pytest.raises(ValueError, match="INVALID_CLAIMS_PREFIX"):
        parser.feed(
            '{"untrusted":[{"text":"不得发布",'
            '"support_ids":["E1:S1"]}],"claims":[]}'
        )

    assert parser.claims == ()


def test_incremental_claim_parser_rejects_incomplete_and_unbounded_output(
) -> None:
    incomplete = IncrementalClaimsParser(
        max_claims=4,
        max_buffer_chars=4096,
    )
    incomplete.feed('{"claims":[{"text":"未完成"')

    with pytest.raises(ValueError, match="INCOMPLETE_CLAIMS_STREAM"):
        incomplete.finish()

    bounded = IncrementalClaimsParser(max_claims=4, max_buffer_chars=32)
    with pytest.raises(ValueError, match="CLAIMS_STREAM_TOO_LARGE"):
        bounded.feed('{"claims":[' + (" " * 40))


def test_incremental_claim_parser_rejects_more_than_four_claims() -> None:
    parser = IncrementalClaimsParser(max_claims=4, max_buffer_chars=4096)
    payload = {
        "claims": [
            {"text": f"claim-{index}", "support_ids": ["E1:S1"]}
            for index in range(5)
        ]
    }

    with pytest.raises(ValueError, match="TOO_MANY_STREAMED_CLAIMS"):
        parser.feed(json.dumps(payload, separators=(",", ":")))
