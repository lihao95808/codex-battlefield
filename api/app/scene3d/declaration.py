"""场景声明（L0 产物）：槽位表 + 每个槽位被钉死的参数 + 自由参数域 + 精度额。

声明是**只读契约**：worker 只能填自由参数，不能改 mount、不能改受约束参数、不能增删槽位。
失效方向（AGENTS.md 硬约定 5）：
- 声明读不出来 / 字段缺失 / mount 非法 → **阻断**（fail closed），绝不按"默认场景"继续。
- 自由参数缺域 → 阻断（缺域等于没有边界，弱模型会填天上）。
- 容差是常量、不接受外部输入放宽 → 放宽几何判据必须改代码并过审。
"""

from __future__ import annotations

import copy
import json
from .contracts import finite_number, require_finite_tree
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_TOL = 0.02       # 尺寸类受约束参数的容差（米）
DEFAULT_POS_TOL = 0.03   # 位置类（x0/x1/z/cx/cz）的容差（米）


class DeclarationError(ValueError):
    """声明本身不合法。出现即阻断，不做兜底。"""


@dataclass(frozen=True)
class Mount:
    origin: tuple[float, float, float]
    rot: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"origin": list(self.origin), "rot": self.rot}

    @staticmethod
    def load(raw: Any, slot: str) -> "Mount":
        if not isinstance(raw, dict):
            raise DeclarationError(f"slot {slot!r}: mount must be an object")
        origin = raw.get("origin")
        if not isinstance(origin, list) or len(origin) != 3 or not all(isinstance(v, (int, float)) for v in origin):
            raise DeclarationError(f"slot {slot!r}: mount.origin must be 3 numbers")
        if not all(finite_number(v) for v in origin):
            raise DeclarationError(f"slot {slot!r}: mount.origin must be finite")
        raw_rot = raw.get("rot", 0)
        if not finite_number(raw_rot) or raw_rot != int(raw_rot):
            raise DeclarationError(f"slot {slot!r}: mount.rot must be an integer")
        rot = int(raw_rot)
        if rot % 90 != 0 or rot % 360 not in (0, 90, 180, 270):
            raise DeclarationError(f"slot {slot!r}: mount.rot must be 0/90/180/270, got {rot}")
        return Mount((float(origin[0]), float(origin[1]), float(origin[2])), rot % 360)


@dataclass(frozen=True)
class Slot:
    name: str
    builder: str
    mount: Mount
    expect: dict[str, Any] = field(default_factory=dict)   # 受约束参数：逐个核对
    free: tuple[str, ...] = ()                             # 允许 worker 自选的参数
    domain: dict[str, tuple[float, float]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot": self.name,
            "builder": self.builder,
            "mount": self.mount.to_dict(),
            "expect": self.expect,
            "free": list(self.free),
            "domain": {k: list(v) for k, v in self.domain.items()},
        }


@dataclass(frozen=True)
class Precision:
    """全局精度出口：冻结后不再变，细化停止由它判定而不是由模型判断。

    - delta_world_m：世界最小可表达特征尺寸（本契约的体素/度量分辨率口径）
    - min_member_m：任何构件 bbox 的中位维度下限（防尘/刀片；故意做薄的构件不在此列）
    - slot_ceiling：声明允许的槽位总数上限（防"无限细化"）
    - max_depth：允许的递归深度上限
    """

    delta_world_m: float
    min_member_m: float
    slot_ceiling: int
    max_depth: int

    def to_dict(self) -> dict[str, Any]:
        return {"delta_world_m": self.delta_world_m, "min_member_m": self.min_member_m,
                "slot_ceiling": self.slot_ceiling, "max_depth": self.max_depth}

    @staticmethod
    def load(raw: Any) -> "Precision":
        if not isinstance(raw, dict):
            raise DeclarationError("precision must be an object")
        unknown = set(raw) - {"delta_world_m", "min_member_m", "slot_ceiling", "max_depth"}
        if unknown:
            raise DeclarationError(f"precision has unknown keys: {sorted(unknown)}")
        try:
            delta, member = float(raw["delta_world_m"]), float(raw["min_member_m"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DeclarationError(f"precision requires delta_world_m/min_member_m: {exc}") from exc
        # 上限必须严格正整数：`int(2.9)` 静默变 2 会让预算比声明松，是最难发现的放水
        ceiling, depth = raw.get("slot_ceiling"), raw.get("max_depth")
        for key, value in (("slot_ceiling", ceiling), ("max_depth", depth)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise DeclarationError(f"precision.{key} must be a positive integer, got {value!r}")
        if not (finite_number(delta) and finite_number(member)) or delta <= 0 or member <= 0:
            raise DeclarationError("precision deltas must be positive finite numbers")
        return Precision(delta, member, ceiling, depth)


@dataclass(frozen=True)
class Declaration:
    name: str
    slots: tuple[Slot, ...]
    size_m: float = 0.0          # 场景尺度，用来定采样密度与 IoU 阈值
    notes: str = ""
    checks: tuple[dict[str, Any], ...] = ()   # 特征校验清单（数据，不是代码里的隐含约定）
    precision: Precision | None = None        # 全局精度出口；没有则不加精度约束

    def by_name(self, name: str) -> Slot | None:
        for s in self.slots:
            if s.name == name:
                return s
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "size_m": self.size_m,
            "notes": self.notes,
            "slots": [s.to_dict() for s in self.slots],
            "checks": [dict(c) for c in self.checks],
            **({"precision": self.precision.to_dict()} if self.precision is not None else {}),
        }

    def dumps(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @staticmethod
    def loads(text: str) -> "Declaration":
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise DeclarationError(f"declaration is not valid JSON: {exc}") from exc
        return Declaration.from_dict(raw)

    @staticmethod
    def from_path(path: Path) -> "Declaration":
        if not path.exists():
            raise DeclarationError(f"declaration not found: {path}")
        return Declaration.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def from_dict(raw: Any) -> "Declaration":
        if not isinstance(raw, dict):
            raise DeclarationError("declaration must be an object")
        try:
            require_finite_tree(raw)
        except ValueError as exc:
            raise DeclarationError(str(exc)) from exc
        raw_slots = raw.get("slots")
        if not isinstance(raw_slots, list) or not raw_slots:
            raise DeclarationError("declaration.slots must be a non-empty list")
        slots: list[Slot] = []
        seen: set[str] = set()
        for item in raw_slots:
            if not isinstance(item, dict):
                raise DeclarationError("declaration.slots[] must be objects")
            name = str(item.get("slot") or "").strip()
            if not name:
                raise DeclarationError("declaration slot without name")
            if name in seen:
                raise DeclarationError(f"duplicate declaration slot {name!r}")
            seen.add(name)
            builder = str(item.get("builder") or "")
            if builder not in _KNOWN_BUILDERS():
                raise DeclarationError(f"slot {name!r}: unknown builder {builder!r}")
            expect = item.get("expect") or {}
            if not isinstance(expect, dict):
                raise DeclarationError(f"slot {name!r}: expect must be an object")
            free = tuple(str(x) for x in (item.get("free") or []))
            domain_raw = item.get("domain") or {}
            if not isinstance(domain_raw, dict):
                raise DeclarationError(f"slot {name!r}: domain must be an object")
            domain: dict[str, tuple[float, float]] = {}
            for k, v in domain_raw.items():
                if not isinstance(v, list) or len(v) != 2 or not all(isinstance(x, (int, float)) for x in v):
                    raise DeclarationError(f"slot {name!r}: domain[{k!r}] must be [lo, hi]")
                if not all(finite_number(x) for x in v) or v[0] > v[1]:
                    raise DeclarationError(f"slot {name!r}: domain[{k!r}] must be finite and ordered")
                domain[k] = (float(v[0]), float(v[1]))
            missing_domain = [k for k in free if k not in domain]
            if missing_domain:
                raise DeclarationError(f"slot {name!r}: free params without domain {missing_domain}")
            overlap = sorted(set(expect) & set(free))
            if overlap:
                raise DeclarationError(f"slot {name!r}: params both expected and free {overlap}")
            slots.append(
                Slot(
                    name=name,
                    builder=builder,
                    mount=Mount.load(item.get("mount"), name),
                    expect=dict(expect),
                    free=free,
                    domain=domain,
                )
            )
        size = float(raw.get("size_m") or 0.0)
        if size <= 0:
            raise DeclarationError("declaration.size_m must be > 0")
        checks_raw = raw.get("checks") or []
        if not isinstance(checks_raw, list) or not checks_raw:
            raise DeclarationError("declaration.checks must be a non-empty list")
        checks: list[dict[str, Any]] = []
        for c in checks_raw:
            if not isinstance(c, dict) or not c.get("id") or not c.get("rule"):
                raise DeclarationError("declaration.checks[] need id and rule")
            checks.append(dict(c))
        precision = None
        if raw.get("precision") is not None:
            precision = Precision.load(raw["precision"])
            if len(slots) > precision.slot_ceiling:
                raise DeclarationError(
                    f"declaration has {len(slots)} slots, exceeding precision.slot_ceiling={precision.slot_ceiling}"
                )
        return Declaration(
            name=str(raw.get("name") or "scene"),
            slots=tuple(slots),
            size_m=size,
            notes=str(raw.get("notes") or ""),
            checks=tuple(checks),
            precision=precision,
        )


def _KNOWN_BUILDERS() -> tuple[str, ...]:
    from .builders import BUILDERS

    return tuple(BUILDERS)


def reference_parts(decl: Declaration) -> list[dict[str, Any]]:
    """声明自带的"参考答案"：free 参数取域中点。

    用途有两个：给 target 生成几何、给测试当正样本。
    **它不是"唯一正确答案"** —— free 参数由 worker 自选，校验只查域。
    """
    parts = []
    for slot in decl.slots:
        params = copy.deepcopy(slot.expect)
        for k in slot.free:
            lo, hi = slot.domain[k]
            v = (lo + hi) / 2
            # 整数型自由参数（planks / mullions / rails / tiers / steps）取整，否则 builder 会拒绝
            params[k] = int(round(v)) if float(lo).is_integer() and float(hi).is_integer() else round(v, 3)
        parts.append({"slot": slot.name, "builder": slot.builder, "params": params})
    return parts
