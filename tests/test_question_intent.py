import pytest

from rag_app.generation.question_intent import (
    QuestionIntent,
    classify_question_intent,
)


@pytest.mark.parametrize(
    ("question", "expected"),
    (
        (
            "一个项目要开始了，我是开发，接下来要干啥",
            QuestionIntent.PROCEDURE,
        ),
        (
            "没有合同和长期规划，只想验证可行性，还需要启动评审吗",
            QuestionIntent.DECISION,
        ),
        (
            "我是 OPC owner，项目开始了我要干啥",
            QuestionIntent.PROCEDURE,
        ),
        ("三种模式区别", QuestionIntent.COMPARE),
        ("快验还是产品开发", QuestionIntent.DECISION),
        ("快验可灵活、项目交付不能省略", QuestionIntent.COMPARE),
    ),
)
def test_free_question_regression_intents(
    question: str,
    expected: QuestionIntent,
) -> None:
    assert classify_question_intent(question) is expected
