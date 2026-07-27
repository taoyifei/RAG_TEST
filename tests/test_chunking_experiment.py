from evaluation.chunking_experiment import summarize_token_lengths


def test_summarize_token_lengths_reports_tail_and_maximum() -> None:
    summary = summarize_token_lengths([1, 2, 2, 4, 9])

    assert summary == {
        "count": 5,
        "minimum": 1,
        "p50": 2,
        "p95": 9,
        "maximum": 9,
    }
