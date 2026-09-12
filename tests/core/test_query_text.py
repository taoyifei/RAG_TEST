from __future__ import annotations

from rag_app.core.query_text import (
    context_label_variants,
    duty_heading_path_owns_target,
    select_unique_label_owner,
)


def test_context_label_variants_preserve_project_scope_and_word_order() -> None:
    assert context_label_variants("蓝熊交付项目") == (
        "蓝熊交付项目",
        "项目蓝熊交付",
        "蓝熊交付",
    )


def test_unique_label_owner_accepts_one_high_similarity_document() -> None:
    owner = select_unique_label_owner(
        "蓝熊开发全流程工作规范",
        (
            ("doc-intended", "青鹊开发全流程工作规范.docx"),
            ("doc-other", "白鹭交付全流程工作规范.docx"),
        ),
    )

    assert owner == "doc-intended"


def test_unique_label_owner_rejects_ambiguous_near_matches() -> None:
    owner = select_unique_label_owner(
        "蓝熊开发全流程工作规范",
        (
            ("doc-a", "青鹊开发全流程工作规范.docx"),
            ("doc-b", "白鹭开发全流程工作规范.docx"),
        ),
    )

    assert owner is None


def test_unique_label_owner_does_not_fuzzy_match_short_target() -> None:
    owner = select_unique_label_owner(
        "蓝熊流程",
        (("doc-a", "青鹊流程.docx"),),
    )

    assert owner is None


def test_duty_heading_owner_uses_exact_role_boundary() -> None:
    assert duty_heading_path_owns_target(
        "总经理",
        ("4 部门的职责", "4.1 总经理岗位职责"),
    )
    assert duty_heading_path_owns_target(
        "总经理",
        ("四、部门的职责", "一、总经理的安全职责"),
    )
    assert not duty_heading_path_owns_target(
        "总经理",
        ("4 部门的职责", "4.7 生产经理"),
    )
    assert not duty_heading_path_owns_target(
        "总经理",
        ("4 部门的职责", "4.2 副总经理"),
    )
    assert not duty_heading_path_owns_target(
        "总经理",
        ("4.1 总经理", "4.1.1 生产经理"),
    )
