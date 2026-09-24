from __future__ import annotations

from app.utils import (
    has_irreversible_question_mark_corruption,
    has_mojibake_corruption,
    repair_mojibake,
    repair_mojibake_text,
)


def test_repair_mojibake_text_recovers_task_goal_utf8_as_latin1() -> None:
    original = (
        "\u5f00\u53d1\u4e00\u4e2a2D \u8857\u673a \u683c\u6597 \u95ef\u5173 "
        "\u6e38\u620f godot \u54c1\u8d28\u548c\u5e02\u9762\u4e0a\u8857\u673a\u4e00\u4e2a\u6c34\u5e73"
    )
    broken = original.encode("utf-8").decode("latin1")

    assert repair_mojibake_text(broken) == original


def test_repair_mojibake_text_recovers_gbk_style_cjk_mojibake() -> None:
    original = "\u6839\u8282\u70b9\u6700\u7ec8\u6210\u54c1\u95e8\u7981\u672a\u901a\u8fc7"
    broken = original.encode("utf-8").decode("gbk")

    assert repair_mojibake_text(broken) == original


def test_repair_mojibake_text_recovers_utf8_as_latin1() -> None:
    original = "开发一个2D格斗闯关街机游戏。项目宪章：质量下限。"
    broken = original.encode("utf-8").decode("latin1")

    assert repair_mojibake_text(broken) == original


def test_repair_mojibake_keeps_normal_text_and_repairs_nested_values() -> None:
    normal = "正常中文不应该被修改"
    broken = "验收标准".encode("utf-8").decode("latin1")
    payload = {"title": normal, "acceptance": [broken]}

    repaired = repair_mojibake(payload)

    assert repaired["title"] == normal
    assert repaired["acceptance"] == ["验收标准"]


def test_detects_irreversible_question_mark_corruption() -> None:
    assert has_irreversible_question_mark_corruption("title: ????????????\nconclusion: ????????") is True
    assert has_irreversible_question_mark_corruption("Use `?` for optional values and `??` for null coalescing.") is False


def test_detects_residual_mojibake_corruption() -> None:
    broken = "\u9416\u8dfa\u7730\u947a\u509c\u5063\u6960\u5c83\u7609\u95ab\u6c33\u7e43\u9286\u4e7bource_files"

    assert has_mojibake_corruption(broken) is True
    assert has_mojibake_corruption("正常中文说明：父节点验收通过，source_files 有效。") is False
