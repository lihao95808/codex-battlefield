"""显式 seed 契约的冻结与完成前判定（v1 完整场景 / v2-r2 分件装配）。

失效方向（承接 AGENTS.md 硬约定 5）：
- 无契约 → 普通项目行为完全不变；
- 契约一旦绑定，冻结快照损坏、声明被改、scope 未绑定、依赖未交付、预算超限 → 一律阻断；
- 门的验收域由 scope 图**计算**，不接受模型自报的槽位集合，因此父节点不能降级成叶子，
  叶子也不能用"兄弟还没交"解释自己的缺件。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .audit import audit_declaration
from .checks import check_requires, evaluate, evaluate_scope
from .contract import (
    CONTRACT_NAME,
    SceneContract,
    merge_bundles,
    parse_contract,
    relative_file,
    responsible_scope,
    scope_by_marker,
    scope_slots,
    slot_owner_map,
    validate_bundle_partition,
    validate_scope_graph,
    validate_scope_responsibility,
)
from .declaration import Declaration


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def contract_from_snapshot(snapshot: dict[str, Any], workspace: Path) -> SceneContract:
    """兼容旧 v1 快照（`manifest`）与新格式（`contract`）。

    旧快照不重写、不降级：把 `manifest` 归一化成 v1 契约视图，再按原规则验收。
    两种格式都读不出 → ValueError（阻断），不"当没有契约"。
    """
    if isinstance(snapshot.get("contract"), dict):
        return parse_contract(json.dumps(snapshot["contract"]).encode("utf-8"), Path(workspace))
    legacy = snapshot.get("manifest")
    if isinstance(legacy, dict):
        raw = {"version": 1, "declaration": legacy.get("declaration"), "parts": legacy.get("parts")}
        return parse_contract(json.dumps(raw).encode("utf-8"), Path(workspace))
    raise ValueError("frozen snapshot has neither contract nor manifest")


def responsibility_map(contract: SceneContract, declaration: Declaration) -> dict[str, str]:
    """每一项 check 的责任 scope（LCA）。任何依赖没有 owner / 无 LCA 都拒。"""
    if not contract.is_partitioned:
        return {str(c.get("id") or ""): contract.root_scope for c in declaration.checks}
    owners = slot_owner_map(contract)
    return {str(c.get("id") or ""): responsible_scope(contract, check_requires(c), owners)
            for c in declaration.checks}


def checks_for_scope(declaration: Declaration, contract: SceneContract, scope_id: str) -> tuple[str, ...]:
    """该 scope 必须自己执行的检查。

    根要重验全部（R-C6）；其它 scope 只跑 LCA 落在自己身上的那部分（R-C1），
    跨包检查在这里被延后，而不是被误判为失败或误判为通过。
    """
    responsible = responsibility_map(contract, declaration)
    if scope_id == contract.root_scope:
        return tuple(str(c.get("id") or "") for c in declaration.checks)
    return tuple(cid for cid, sid in responsible.items() if sid == scope_id)


def _subset_declaration(decl: Declaration, slots: tuple[str, ...], only_check_ids: tuple[str, ...] | None = None) -> Declaration:
    """作用域视图：只保留该域的空槽位，以及**依赖全部落在域内**的检查。

    跨包检查（例如安装检查同时需要墙与窗框）不属于任何一个儿子，由覆盖两者的祖先履责——
    它们在这里被排除，而不是被一个缺少依赖的节点误判为失败。
    """
    keep = set(slots)
    wanted = None if only_check_ids is None else set(only_check_ids)
    checks = tuple(
        c for c in decl.checks
        if set(check_requires(c)) <= keep and (wanted is None or str(c.get("id") or "") in wanted)
    )
    return Declaration(
        name=decl.name,
        slots=tuple(s for s in decl.slots if s.name in keep),
        size_m=decl.size_m,
        notes=decl.notes,
        checks=checks,
        precision=decl.precision,
    )


def deferred_checks(decl: Declaration, slots: tuple[str, ...], responsible: dict[str, str] | None = None,
                    scope_id: str = "") -> list[str]:
    """不属于本 scope 的检查：显式延后，不能算 passed。"""
    keep = set(slots)
    out: list[str] = []
    for check in decl.checks:
        cid = str(check.get("id") or "")
        if responsible is not None and scope_id:
            if responsible.get(cid) != scope_id:
                out.append(cid)
        elif not set(check_requires(check)) <= keep:
            out.append(cid)
    return out


def freeze_contract(workspace: Path, root_id: str, target: str | None) -> dict[str, Any] | None:
    """在 worker 开始前冻结原始契约与声明；非法 opt-in 直接拒开树。"""
    if target is None:
        return None
    seed = Path(target)
    manifest_path = seed / CONTRACT_NAME
    try:
        manifest_raw = manifest_path.read_bytes()
    except FileNotFoundError:
        if manifest_path.is_symlink():
            raise ValueError("scene3d contract is an unreadable symlink") from None
        return None
    contract = parse_contract(manifest_raw, seed)
    declaration_raw = relative_file(seed, contract.declaration).read_bytes()
    declaration = Declaration.loads(declaration_raw.decode("utf-8"))
    slot_names = [s.name for s in declaration.slots]
    if contract.is_partitioned:
        validate_bundle_partition(contract, slot_names)
        validate_scope_graph(contract)
        validate_scope_responsibility(contract, slot_names)
        responsible = responsibility_map(contract, declaration)
        for scope in contract.scopes:
            if scope.kind != "bundle":
                continue
            # R-C5：叶子必须至少有一项**真正由它执行**的检查，否则"子集通过"是空的
            if not any(sid == scope.id for sid in responsible.values()):
                raise ValueError(f"bundle scope {scope.id!r} has no intrinsic check to verify")
        # R-C7 / R-C8：冻结期审计。恒真的检查、以及"合法参数下会失败"的检查，都必须在这里拦下，
        # 不能等到 worker 交付后才暴露——那时模型会以为是自己做错了。
        audit = audit_declaration(declaration)
        if not audit["ok"]:
            problems = (audit["negative_problems"] + audit["domain_problems"])[:5]
            raise ValueError(f"declaration audit failed: {problems}")
    snapshot = json.dumps({
        "root_id": root_id,
        "audit": {k: audit[k] for k in ("ok", "checks", "slots", "samples")} if contract.is_partitioned else {},
        "contract": contract.to_dict(),
        "contract_sha256": _hash(manifest_raw),
        "declaration_sha256": _hash(declaration_raw),
        "declaration_text": declaration_raw.decode("utf-8"),
    }, ensure_ascii=False, sort_keys=True).encode("utf-8")
    directory = Path(workspace) / "scene3d_contracts" / root_id
    directory.mkdir(parents=True, exist_ok=False)
    (directory / CONTRACT_NAME).write_bytes(manifest_raw)
    (directory / "scene.declaration.json").write_bytes(declaration_raw)
    (directory / "snapshot.json").write_bytes(snapshot)
    return {"version": contract.version, "root_id": root_id, "snapshot_sha256": _hash(snapshot),
            **({"schema_revision": contract.schema_revision} if contract.schema_revision else {})}


def _frozen_snapshot(workspace: Path, root_id: str, binding: dict[str, Any]) -> dict[str, Any]:
    if binding.get("root_id") != root_id:
        raise ValueError("frozen contract binding does not match root")
    directory = Path(workspace) / "scene3d_contracts" / root_id
    snapshot_raw = (directory / "snapshot.json").read_bytes()
    if _hash(snapshot_raw) != binding.get("snapshot_sha256"):
        raise ValueError("frozen contract snapshot hash mismatch")
    snapshot = json.loads(snapshot_raw)
    if snapshot["root_id"] != root_id:
        raise ValueError("frozen snapshot root mismatch")
    return snapshot


def _read_parts(path: Path) -> Any:
    raw = json.loads(path.read_bytes().decode("utf-8"))
    return raw.get("parts") if isinstance(raw, dict) else raw


def evaluate_contract(
    workspace: Path,
    root_id: str,
    binding: dict[str, Any] | None,
    worktree: str | None,
    scope_markers: tuple[str, ...] = (),
    depth: int = 0,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "required": binding is not None, "passed": binding is None,
        "root_id": root_id, "target": worktree, "stage": "not_required",
        "rejects": [], "checks": [], "geometry_only": True, "scope": "none",
    }
    if binding is None:
        return report
    stage = "frozen_contract"
    try:
        snapshot = _frozen_snapshot(workspace, root_id, binding)
        contract = contract_from_snapshot(snapshot, workspace)
        declaration = Declaration.loads(snapshot["declaration_text"])
        report["declaration_sha256"] = snapshot["declaration_sha256"]
        report["precision_exit"] = "not_evaluated"  # M2 之前必须显式标明，不能静默省略
        report["contract_version"] = contract.version
        if contract.schema_revision:
            report["schema_revision"] = contract.schema_revision

        if declaration.precision is not None and depth > 0 and depth > declaration.precision.max_depth:
            raise ValueError(f"task depth {depth} exceeds precision.max_depth={declaration.precision.max_depth}")
        if not worktree:
            raise ValueError("scene3d completion requires the delivery worktree")
        delivery = Path(worktree)

        stage = "declaration_integrity"
        current = relative_file(delivery, contract.declaration).read_bytes()
        report["actual_declaration_sha256"] = _hash(current)
        if _hash(current) != snapshot["declaration_sha256"]:
            raise ValueError("delivery declaration differs from frozen seed declaration")

        scope = scope_by_marker(contract, scope_markers) if contract.is_partitioned else None
        if scope is not None:
            stage = "scope_parts"
            domain = scope_slots(contract, scope.id)
            if scope.kind == "bundle":
                bundle = contract.bundle(scope.bundle)
                assert bundle is not None
                parts_path = relative_file(delivery, bundle.file)
            else:
                parts_path = relative_file(delivery, scope.output or contract.parts)
            parts_raw = parts_path.read_bytes()
            report["scope_id"] = scope.id
            report["scope_kind"] = scope.kind
            report["expected_slots"] = list(domain)
            report["parts_sha256"] = _hash(parts_raw)
            responsible = responsibility_map(contract, declaration)
            own_checks = checks_for_scope(declaration, contract, scope.id)
            deferred = deferred_checks(declaration, domain, responsible, scope.id)
            report["deferred_checks"] = deferred
            report["responsible_scope"] = {cid: responsible[cid] for cid in deferred}
            parts = _read_parts(parts_path)
            stage = "geometry"
            result = evaluate_scope(
                _subset_declaration(declaration, domain, own_checks), parts,
                require_connectivity=scope.connectivity == "required",
                frames=declaration,  # 坐标系来自完整声明，不因本包未交某个 slot 而失效
            )
            report.update(result)
            report["scope"] = scope.kind
            report["passed"] = result["ok"] is True
            return report

        stage = "parts"
        parts_path = relative_file(delivery, contract.parts)
        parts_raw = parts_path.read_bytes()
        report["parts_sha256"] = _hash(parts_raw)
        parts = _read_parts(parts_path)
        stage = "geometry"
        result = evaluate(declaration, parts)
        report.update(result)
        report["scope"] = "full"
        report["deferred_checks"] = []
        report["responsible_scope"] = {}
        report["passed"] = result["ok"] is True
    except Exception as exc:
        # Fail closed even if an evaluator raises on malformed worker data.
        report.update(passed=False, stage=stage, rejects=[{
            "slot": "*", "field": stage, "expected": "readable frozen-contract delivery",
            "actual": f"{type(exc).__name__}: {exc}", "rule": "scene3d_gate",
        }])
    return report


def assemble_from_bundles(
    workspace: Path,
    root_id: str,
    binding: dict[str, Any] | None,
    target_dir: str,
) -> dict[str, Any]:
    """确定性装配：校验输入 → merge → 原子发布合并后的 parts。

    这是唯一允许"产生 scene.parts.json"的地方；门自己不拼。
    任何结构问题（缺 bundle、槽位不符、重复槽位、多余输入）都拒，不产出半成品。
    """
    report: dict[str, Any] = {
        "root_id": root_id, "target": target_dir, "stage": "assembly", "ok": False,
        "bundles": [], "rejects": [],
    }
    if binding is None:
        report["rejects"] = [{"rule": "no-contract", "actual": "no scene3d contract bound"}]
        return report
    try:
        snapshot = _frozen_snapshot(workspace, root_id, binding)
        contract = contract_from_snapshot(snapshot, workspace)
        if not contract.is_partitioned:
            report["ok"] = True
            report["note"] = "v1 contract: nothing to assemble"
            return report
        target = Path(target_dir)
        inputs: dict[str, Path] = {}
        for bundle in contract.bundles:
            path = relative_file(target, bundle.file)
            if not path.is_file():
                raise ValueError(f"bundle {bundle.id!r} file missing: {bundle.file}")
            inputs[bundle.id] = path
        merged = merge_bundles(contract, target, inputs)
        names = [str(p["slot"]) for p in merged]
        if len(names) != len(set(names)):
            raise ValueError("merged parts contain duplicate slots")
        by_slot = {str(p["slot"]): p for p in merged}
        # 自底向上装配每个非根 assembly 的责任域（含中间父 C2），根写顶层 parts
        assembly_writes: list[dict[str, Any]] = []
        for scope in contract.scopes:
            if scope.kind != "assembly" or scope.id == contract.root_scope or not scope.output:
                continue
            domain = scope_slots(contract, scope.id)
            payload_parts = [by_slot[s] for s in domain if s in by_slot]
            if len(payload_parts) != len(domain):
                raise ValueError(f"assembly scope {scope.id!r} missing slots for its output")
            out = relative_file(target, scope.output)
            out.parent.mkdir(parents=True, exist_ok=True)
            body = json.dumps({"parts": payload_parts}, ensure_ascii=False, indent=2) + "\n"
            tmp = out.with_suffix(out.suffix + ".tmp")
            tmp.write_text(body, encoding="utf-8")
            os.replace(tmp, out)
            assembly_writes.append({"scope": scope.id, "output": scope.output, "sha256": _hash(body.encode("utf-8")),
                                    "slots": list(domain)})
        out_path = relative_file(target, contract.parts)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"parts": merged}, ensure_ascii=False, indent=2) + "\n"
        tmp = out_path.with_suffix(out_path.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, out_path)
        report["ok"] = True
        report["parts_sha256"] = _hash(payload.encode("utf-8"))
        report["slot_count"] = len(merged)
        report["assemblies"] = assembly_writes
        report["bundles"] = [{"id": b.id, "sha256": _hash(inputs[b.id].read_bytes())} for b in contract.bundles]
    except Exception as exc:
        report["rejects"] = [{"slot": "*", "field": "assembly", "expected": "complete bundle coverage",
                              "actual": f"{type(exc).__name__}: {exc}", "rule": "scene3d_assembly"}]
    return report
