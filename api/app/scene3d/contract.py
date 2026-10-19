"""scene3d.contract.json 的解析与冻结校验（v1 / v2-r2）。

v1：完整场景契约，保持历史语义（字段名 `manifest` 的旧快照仍按原规则复验）。
v2-r2：分件契约 —— `bundles[]` 划分独占交付的 slot，`scopes[]` 给出 C → C1/C2 → C21 的责任域图；
        叶子的验收域由 scope 图**计算**，不能由模型自报一个更小的集合。

失效方向：v2 缺 `schema_revision`、缺 bundles/scopes、scope 图有环/孤儿、
bundle 覆盖不等于声明、路径越界、id 重复 → 一律拒开树。
不接受裸 `version: 2`：原型与 v2-r2 语义不同，静默接受会让"v2"出现两种含义。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONTRACT_NAME = "scene3d.contract.json"
SCHEMA_REVISION = "v2-r2"
SCOPE_MARKER_PREFIX = "scene3d-scope:"


@dataclass(frozen=True)
class Bundle:
    id: str
    slots: tuple[str, ...]
    file: str

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "slots": list(self.slots), "file": self.file}


@dataclass(frozen=True)
class Scope:
    id: str
    kind: str                      # "assembly" | "bundle"
    children: tuple[str, ...] = ()
    bundle: str = ""               # kind == bundle 时必填
    output: str = ""               # kind == assembly 时必填（根为空表示顶层 parts）
    importance: float = 1.0        # 0.5–4；只影响 M2 的细节下限，不影响归属
    connectivity: str = ""         # assembly 必填：required | not_required

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "kind": self.kind}
        if self.kind == "bundle":
            out["bundle"] = self.bundle
        else:
            out["children"] = list(self.children)
            out["importance"] = self.importance
            out["connectivity"] = self.connectivity
            if self.output:
                out["output"] = self.output
        return out


@dataclass(frozen=True)
class SceneContract:
    version: int
    declaration: str
    parts: str
    bundles: tuple[Bundle, ...] = ()
    scopes: tuple[Scope, ...] = ()
    root_scope: str = ""
    schema_revision: str = ""

    @property
    def is_partitioned(self) -> bool:
        return self.version == 2

    def bundle(self, bundle_id: str) -> Bundle | None:
        return next((b for b in self.bundles if b.id == bundle_id), None)

    def scope(self, scope_id: str) -> Scope | None:
        return next((s for s in self.scopes if s.id == scope_id), None)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"version": self.version, "declaration": self.declaration, "parts": self.parts}
        if self.is_partitioned:
            out["schema_revision"] = self.schema_revision
            out["root_scope"] = self.root_scope
            out["bundles"] = [b.to_dict() for b in self.bundles]
            out["scopes"] = [s.to_dict() for s in self.scopes]
        return out


def relative_file(root: Path, name: Any) -> Path:
    """契约内引用只允许工程内的相对路径；绝对路径/.. 一律拒。"""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("contract paths must be non-empty relative file names")
    relative = Path(name)
    if relative.is_absolute() or relative.drive or ".." in relative.parts:
        raise ValueError(f"contract path must stay within project: {name!r}")
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"contract path escapes project: {name!r}")
    return path


def parse_contract(raw: bytes, root: Path) -> SceneContract:
    """解析契约清单。格式非法即 ValueError（调用方负责把它变成阻断）。"""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"scene3d contract is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("scene3d contract must be an object")
    version = data.get("version")
    if type(version) is not int or version not in (1, 2):
        raise ValueError("scene3d contract version must be 1 or 2")
    declaration = data.get("declaration")
    parts = data.get("parts")
    relative_file(root, declaration)
    relative_file(root, parts)
    if len({CONTRACT_NAME, declaration, parts}) != 3:
        raise ValueError("contract, declaration and parts must be separate files")

    if version == 1:
        if set(data) != {"version", "declaration", "parts"}:
            raise ValueError("scene3d v1 contract requires exactly version, declaration, parts")
        return SceneContract(version=1, declaration=declaration, parts=parts)

    allowed = {"version", "schema_revision", "declaration", "parts", "root_scope", "bundles", "scopes"}
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"scene3d v2 contract has unknown keys: {sorted(unknown)}")
    revision = data.get("schema_revision")
    if revision != SCHEMA_REVISION:
        raise ValueError(
            f"scene3d v2 contract requires schema_revision={SCHEMA_REVISION!r}; "
            f"bare or unknown revision is rejected (got {revision!r})"
        )

    raw_bundles = data.get("bundles")
    if not isinstance(raw_bundles, list) or not raw_bundles:
        raise ValueError("scene3d v2 contract requires a non-empty bundles list")
    bundles: list[Bundle] = []
    seen_ids: set[str] = set()
    seen_files: set[Path] = {relative_file(root, declaration), relative_file(root, parts), (root / CONTRACT_NAME).resolve()}
    for item in raw_bundles:
        if not isinstance(item, dict):
            raise ValueError("scene3d v2 bundles[] must be objects")
        bid = str(item.get("id") or "").strip()
        if not bid:
            raise ValueError("scene3d v2 bundle without id")
        if bid in seen_ids:
            raise ValueError(f"scene3d v2 duplicate bundle id {bid!r}")
        seen_ids.add(bid)
        bfile = item.get("file")
        path = relative_file(root, bfile).resolve()
        if path in seen_files:
            raise ValueError(f"scene3d v2 bundle file must be unique and separate: {bfile!r}")
        seen_files.add(path)
        slots = item.get("slots")
        if not isinstance(slots, list) or not slots or not all(isinstance(s, str) and s.strip() for s in slots):
            raise ValueError(f"scene3d v2 bundle {bid!r} needs a non-empty slots list")
        if len(set(slots)) != len(slots):
            raise ValueError(f"scene3d v2 bundle {bid!r} has duplicate slots")
        bundles.append(Bundle(id=bid, slots=tuple(slots), file=bfile))

    root_scope = str(data.get("root_scope") or "").strip()
    raw_scopes = data.get("scopes")
    if not isinstance(raw_scopes, list) or not raw_scopes:
        raise ValueError("scene3d v2 contract requires a non-empty scopes list")
    scopes: list[Scope] = []
    seen_scope_ids: set[str] = set()
    for item in raw_scopes:
        if not isinstance(item, dict):
            raise ValueError("scene3d v2 scopes[] must be objects")
        sid = str(item.get("id") or "").strip()
        if not sid:
            raise ValueError("scene3d v2 scope without id")
        if sid in seen_scope_ids:
            raise ValueError(f"scene3d v2 duplicate scope id {sid!r}")
        seen_scope_ids.add(sid)
        kind = str(item.get("kind") or "")
        if kind not in {"assembly", "bundle"}:
            raise ValueError(f"scope {sid!r}: kind must be assembly or bundle")
        if kind == "bundle":
            bid = str(item.get("bundle") or "").strip()
            if bid not in seen_ids:
                raise ValueError(f"scope {sid!r} references unknown bundle {bid!r}")
            if "children" in item:
                # 空数组也拒：叶子带子节点是歧义来源，不允许"看起来像但没用"
                raise ValueError(f"bundle scope {sid!r} must not declare children")
            scopes.append(Scope(id=sid, kind=kind, bundle=bid))
        else:
            children = item.get("children")
            if not isinstance(children, list) or not children or not all(isinstance(c, str) and c.strip() for c in children):
                raise ValueError(f"assembly scope {sid!r} needs a non-empty children list")
            importance = item.get("importance", 1.0)
            if isinstance(importance, bool) or not isinstance(importance, (int, float)):
                raise ValueError(f"scope {sid!r}: importance must be a number")
            importance = float(importance)
            if not (0.5 <= importance <= 4.0):
                raise ValueError(f"scope {sid!r}: importance must be within 0.5..4")
            connectivity = str(item.get("connectivity") or "")
            if connectivity not in {"required", "not_required"}:
                raise ValueError(f"assembly scope {sid!r}: connectivity must be required or not_required")
            output = str(item.get("output") or "").strip()
            if output:
                path = relative_file(root, output).resolve()
                if path in seen_files:
                    raise ValueError(f"assembly scope {sid!r} output must be unique: {output!r}")
                seen_files.add(path)
            scopes.append(Scope(id=sid, kind=kind, children=tuple(children), output=output,
                                importance=importance, connectivity=connectivity))

    if not root_scope or root_scope not in seen_scope_ids:
        raise ValueError("scene3d v2 contract root_scope must name a declared scope")
    return SceneContract(
        version=2, declaration=declaration, parts=parts, bundles=tuple(bundles),
        scopes=tuple(scopes), root_scope=root_scope, schema_revision=SCHEMA_REVISION,
    )


# ---------------------------------------------------------------- 覆盖与责任域


def validate_bundle_partition(contract: SceneContract, declaration_slots: list[str]) -> None:
    """bundles 的 slots 必须恰好覆盖声明槽位：不漏、不重、不多。"""
    declared = set(declaration_slots)
    covered: list[str] = []
    for bundle in contract.bundles:
        for slot in bundle.slots:
            if slot not in declared:
                raise ValueError(f"bundle {bundle.id!r} references undeclared slot {slot!r}")
            if slot in covered:
                raise ValueError(f"bundle slot {slot!r} appears in more than one bundle")
            covered.append(slot)
    missing = sorted(declared - set(covered))
    if missing:
        raise ValueError(f"bundles do not cover declaration slots: {missing}")
    if len(covered) != len(declaration_slots):
        raise ValueError("bundle coverage does not match declaration slot count")


def _children_map(contract: SceneContract) -> dict[str, tuple[str, ...]]:
    return {s.id: s.children for s in contract.scopes if s.kind == "assembly"}


def validate_scope_graph(contract: SceneContract) -> None:
    """scope 必须是单根有限树：无环、无孤儿、每个非根恰有一个父级、每个 bundle 恰被一个叶 scope 拥有。"""
    children = _children_map(contract)
    parents: dict[str, list[str]] = {}
    for parent, kids in children.items():
        for kid in kids:
            if kid not in {s.id for s in contract.scopes}:
                raise ValueError(f"scope {parent!r} references unknown child {kid!r}")
            parents.setdefault(kid, []).append(parent)
    for scope in contract.scopes:
        got = parents.get(scope.id, [])
        if scope.id == contract.root_scope:
            if got:
                raise ValueError(f"root scope {scope.id!r} must not have a parent")
            continue
        if len(got) != 1:
            raise ValueError(f"scope {scope.id!r} must have exactly one parent, got {got}")

    seen: set[str] = set()
    stack = [contract.root_scope]
    while stack:
        sid = stack.pop()
        if sid in seen:
            raise ValueError(f"scope graph has a cycle at {sid!r}")
        seen.add(sid)
        stack.extend(children.get(sid, ()))
    orphans = sorted({s.id for s in contract.scopes} - seen)
    if orphans:
        raise ValueError(f"scopes unreachable from root: {orphans}")

    owned = [s.bundle for s in contract.scopes if s.kind == "bundle"]
    if len(owned) != len(set(owned)):
        raise ValueError("a bundle is owned by more than one leaf scope")
    missing = sorted({b.id for b in contract.bundles} - set(owned))
    if missing:
        raise ValueError(f"bundles without an owning scope: {missing}")
    root = contract.scope(contract.root_scope)
    if root is None or root.kind != "assembly":
        raise ValueError("root_scope must be an assembly scope")


def scope_slots(contract: SceneContract, scope_id: str) -> tuple[str, ...]:
    """责任域由后代 bundle 计算，不接受模型自报的槽位集合。"""
    scope = contract.scope(scope_id)
    if scope is None:
        raise ValueError(f"unknown scope {scope_id!r}")
    if scope.kind == "bundle":
        bundle = contract.bundle(scope.bundle)
        if bundle is None:
            raise ValueError(f"scope {scope_id!r} references unknown bundle {scope.bundle!r}")
        return bundle.slots
    slots: list[str] = []
    for child in scope.children:
        slots.extend(scope_slots(contract, child))
    if len(set(slots)) != len(slots):
        raise ValueError(f"scope {scope_id!r} covers a slot twice (bundle partition broken)")
    return tuple(slots)


def slot_owner_map(contract: SceneContract) -> dict[str, str]:
    """每个 slot 的唯一 owner bundle scope。分区已校验，这里只做映射。"""
    owners: dict[str, str] = {}
    for scope in contract.scopes:
        if scope.kind != "bundle":
            continue
        bundle = contract.bundle(scope.bundle)
        if bundle is None:
            raise ValueError(f"scope {scope.id!r} references unknown bundle {scope.bundle!r}")
        for slot in bundle.slots:
            if slot in owners:
                raise ValueError(f"slot {slot!r} has more than one owner scope")
            owners[slot] = scope.id
    return owners


def ancestor_chain(contract: SceneContract, scope_id: str) -> tuple[str, ...]:
    """从该 scope 到根的祖先链（含自身），用于求 LCA。"""
    parents: dict[str, str] = {}
    for scope in contract.scopes:
        if scope.kind != "assembly":
            continue
        for child in scope.children:
            parents[child] = scope.id
    chain = [scope_id]
    while chain[-1] != contract.root_scope:
        parent = parents.get(chain[-1])
        if parent is None:
            raise ValueError(f"scope {scope_id!r} is not connected to root")
        chain.append(parent)
    return tuple(chain)


def responsible_scope(contract: SceneContract, requires_slots: tuple[str, ...], owners: dict[str, str]) -> str:
    """责任 scope = 各依赖 slot 的 owner 的**最近公共祖先**。

    图不是链：不能按深度取最浅，也不能"就近取父"，必须真的求 LCA。
    任一依赖无 owner 或 LCA 不存在 → ValueError（拒开树），不做兜底。
    """
    if not requires_slots:
        return contract.root_scope
    missing = [s for s in requires_slots if s not in owners]
    if missing:
        raise ValueError(f"check dependencies have no owner scope: {missing}")
    chains = [ancestor_chain(contract, owners[s]) for s in requires_slots]
    common = set(chains[0])
    for chain in chains[1:]:
        common &= set(chain)
    if not common:
        raise ValueError(f"no common ancestor for dependencies {list(requires_slots)}")
    # 取公共祖先中最深的一个（链上距离 owner 最近）
    depth = {sid: len(ancestor_chain(contract, sid)) for sid in common}
    return max(common, key=lambda sid: (depth[sid], sid))


def validate_scope_responsibility(contract: SceneContract, declaration_slots: list[str]) -> None:
    """根的责任域必须等于全部声明槽位；任何 assembly 至少要有一个后代。"""
    root_slots = scope_slots(contract, contract.root_scope)
    if set(root_slots) != set(declaration_slots):
        raise ValueError("root scope responsibility must equal all declaration slots")
    for scope in contract.scopes:
        if scope.kind == "assembly" and not scope_slots(contract, scope.id):
            raise ValueError(f"assembly scope {scope.id!r} has empty responsibility")


def scope_by_marker(contract: SceneContract, markers: tuple[str, ...]) -> Scope | None:
    """精确匹配 `scene3d-scope:<id>`；不做子串、不从标题猜。命中≥2 视为歧义。"""
    wanted = {m.strip() for m in markers if m.strip()}
    hits = [s for s in contract.scopes if f"{SCOPE_MARKER_PREFIX}{s.id}" in wanted]
    if len(hits) > 1:
        raise ValueError(f"node declares more than one scene3d scope: {[s.id for s in hits]}")
    return hits[0] if hits else None


# ---------------------------------------------------------------- 合并


def merge_bundles(contract: SceneContract, root: Path, inputs: dict[str, Path]) -> list[dict[str, Any]]:
    """确定性合并 bundle 文件为完整 parts。结构问题一律 ValueError（不静默补齐）。

    多余输入、缺输入、槽位与契约不符、同槽位跨包重复 → 全部拒绝。
    """
    if not contract.is_partitioned:
        raise ValueError("merge requires a version-2 (partitioned) contract")
    expected = {b.id for b in contract.bundles}
    extra = sorted(set(inputs) - expected)
    if extra:
        raise ValueError(f"merge got unknown bundle inputs: {extra}")
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for bundle in contract.bundles:
        path = inputs.get(bundle.id)
        if path is None:
            raise ValueError(f"merge input missing for bundle {bundle.id!r}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"bundle {bundle.id!r} is not readable JSON: {exc}") from exc
        parts = raw.get("parts") if isinstance(raw, dict) else raw
        if not isinstance(parts, list):
            raise ValueError(f"bundle {bundle.id!r} must contain a parts list")
        slot_names = []
        for part in parts:
            if not isinstance(part, dict) or not part.get("slot") or not part.get("builder") or not isinstance(part.get("params"), dict):
                raise ValueError(f"bundle {bundle.id!r} has a malformed part entry")
            slot_names.append(str(part["slot"]))
        declared = set(bundle.slots)
        got = set(slot_names)
        if got != declared:
            raise ValueError(f"bundle {bundle.id!r} slots mismatch: missing={sorted(declared - got)} extra={sorted(got - declared)}")
        for slot in slot_names:
            if slot in seen:
                raise ValueError(f"slot {slot!r} appears in more than one bundle")
            seen.add(slot)
        merged.extend(parts)
    return merged
