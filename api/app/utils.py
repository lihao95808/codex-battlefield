from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_PRIVATE_USE_RE = re.compile(r"[\ue000-\uf8ff]")
_COMMON_CJK_CHARS = set(
    "的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会可主发年动同工也能下过子说产种面而方后多定行学法所民得经"
    "任务节点项目目标开发游戏街机格斗闯关品质完成验收修复递归父子资源场景脚本玩家敌人关卡生命攻击命中伤害菜单暂停重开自动恢复"
)
_MOJIBAKE_MARKERS = (
    "å",
    "æ",
    "ç",
    "è",
    "é",
    "ä",
    "ã",
    "Â",
    "â",
    "€",
    "�",
    "椤",
    "瀹",
    "诲",
    "妗",
    "浠",
    "鍙",
    "竴",
    "涓",
    "鏍",
    "娓",
    "垙",
    "鐩",
    "楠",
    "绛",
    "鍓",
    "绾",
)
_STRONG_MOJIBAKE_MARKERS = (
    "\u9416\u8dfa\u7730",  # 鐖跺眰
    "\u947a\u509c\u5063",  # 鑺傜偣
    "\u6960\u5c83\u7609",  # 楠岃瘉
    "\u95ab\u6c33\u7e43",  # 閫氳繃
    "\u6924\u572d\u6d30",  # 椤圭洰
    "\u9286",  # 銆
    "\u4e7b",  # 乻
)
_LATIN1_MOJIBAKE_RE = re.compile(r"(?:Ã.|Â.|â.|ä¸|å.|æ.|ç.|è.|é.)")
_QUESTION_MARK_CORRUPTION_RE = re.compile(r"\?{3,}")


def new_id(prefix: str = "node") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(value: str | None, default: Any) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)


def coerce_utc(value: datetime | None) -> datetime | None:
    """把 naive datetime 视为 UTC，其余统一换算到 UTC。无时区输入的 DB 列在 Windows 上必须走这里。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def path_digest(path: Path) -> str:
    """产物指纹：文件 → `sha256:<hex>`，目录 → `tree:<hex>`（按相对路径排序后逐个文件哈希）。

    目录也必须能指纹化：`changed_files` 允许声明目录，只认文件的写法会让"整个目录被删掉"
    既进不了回归基线，也让 worktree 回收判据说不出这个目录到底归档了没有。
    """
    path = Path(path)
    digest = hashlib.sha256()
    if path.is_dir():
        for item in sorted(path.rglob("*")):
            digest.update(item.relative_to(path).as_posix().encode("utf-8", "replace"))
            if item.is_dir():
                digest.update(b"/dir")
                continue
            digest.update(b"/file")
            with item.open("rb") as handle:
                for chunk in iter(lambda: handle.read(65536), b""):
                    digest.update(chunk)
        return f"tree:{digest.hexdigest()}"
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def repair_mojibake(value: Any) -> Any:
    if isinstance(value, str):
        return repair_mojibake_text(value)
    if isinstance(value, list):
        return [repair_mojibake(item) for item in value]
    if isinstance(value, dict):
        return {repair_mojibake(key): repair_mojibake(item) for key, item in value.items()}
    return value


def repair_mojibake_text(text: str) -> str:
    if not text:
        return text
    best = text
    best_score = _text_quality_score(text)
    threshold = 0 if _looks_like_mojibake(text) else max(3, len(text) // 60)
    for candidate in _repair_candidates(text):
        score = _text_quality_score(candidate)
        if score > best_score + threshold:
            best = candidate
            best_score = score
    return best


def has_irreversible_question_mark_corruption(text: str) -> bool:
    """Detect Windows command-line loss where CJK text became literal question marks."""
    if not text:
        return False
    runs = _QUESTION_MARK_CORRUPTION_RE.findall(text)
    if not runs:
        return False
    run_chars = sum(len(run) for run in runs)
    total_question_marks = text.count("?")
    cjk_count = len(_CJK_RE.findall(text))
    # A few question marks can be legitimate punctuation or code. Long runs plus
    # little/no CJK indicate the original user-visible Chinese was destroyed.
    return (run_chars >= 6 or total_question_marks >= 12) and cjk_count < max(2, total_question_marks // 20)


def has_mojibake_corruption(text: str) -> bool:
    """Detect residual mojibake that should not pass as readable deliverable text."""
    if not text:
        return False
    if "\ufffd" in text or _PRIVATE_USE_RE.search(text):
        return True
    if any(marker in text for marker in _STRONG_MOJIBAKE_MARKERS):
        return True
    if _LATIN1_MOJIBAKE_RE.search(text):
        return True
    candidate = repair_mojibake_text(text)
    if candidate != text:
        return True
    if "\ufffd" in candidate or _PRIVATE_USE_RE.search(candidate):
        return True
    if any(marker in candidate for marker in _STRONG_MOJIBAKE_MARKERS):
        return True
    if _LATIN1_MOJIBAKE_RE.search(candidate):
        return True
    suspicious_count = sum(candidate.count(marker) for marker in _MOJIBAKE_MARKERS)
    if suspicious_count < 3:
        return False
    common_cjk_count = sum(1 for char in candidate if char in _COMMON_CJK_CHARS)
    cjk_count = len(_CJK_RE.findall(candidate))
    return common_cjk_count < max(2, suspicious_count) and suspicious_count >= max(3, cjk_count // 8)


def _repair_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    for encoding in ("latin1", "cp1252", "gbk", "cp936"):
        try:
            candidate = text.encode(encoding).decode("utf-8")
        except UnicodeError:
            continue
        if candidate != text:
            candidates.append(candidate)
    return candidates


def _looks_like_mojibake(text: str) -> bool:
    return (
        any(marker in text for marker in _MOJIBAKE_MARKERS)
        or any(marker in text for marker in _STRONG_MOJIBAKE_MARKERS)
        or bool(_PRIVATE_USE_RE.search(text))
        or "\ufffd" in text
        or any(ord(char) < 32 and char not in "\r\n\t" for char in text)
    )


def _text_quality_score(text: str) -> int:
    cjk_count = len(_CJK_RE.findall(text))
    common_cjk_count = sum(1 for char in text if char in _COMMON_CJK_CHARS)
    private_use_count = len(_PRIVATE_USE_RE.findall(text))
    suspicious_count = sum(text.count(marker) for marker in _MOJIBAKE_MARKERS)
    replacement_count = text.count("\ufffd")
    question_penalty = max(0, text.count("?") - 2)
    control_count = sum(1 for char in text if ord(char) < 32 and char not in "\r\n\t")
    return (
        cjk_count * 4
        + common_cjk_count * 3
        - suspicious_count * 10
        - private_use_count * 14
        - replacement_count * 18
        - question_penalty * 4
        - control_count * 6
    )
