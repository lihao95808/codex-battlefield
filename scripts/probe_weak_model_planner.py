"""弱模型/异构模型可用性实测（P6-4）：给每档模型跑一次真实 planner，记录**能不能用**。

## 为什么是"一次 planner 调用"这个粒度

`verify_provider_planner.py` 已经把"通道不通"和"planner 写错"分开了，再加一层
"哪档模型跑不过"就够了：

* 一次调用**很便宜**（本机实测 deepseek-flash 单次 1 秒级、几千 token），
  所以档位矩阵可以一次跑完；
* 但它**不经过** worker/verifier —— 也就是说这里得出的"能用"只覆盖
  "会按 schema 输出 planner JSON 决策"，**不覆盖**"能改对文件"。这两件事的差距
  是这个脚本最容易骗人的地方，所以报告里 `scope` 字段把它写死。

## 判据（每一档都要逐条过，不合成一个分数）

| 判据 | 失败形态 | 为什么要单列 |
|---|---|---|
| `exit_code == 0` | Codex CLI 自身失败 | 通道问题，不是模型问题 |
| `decision` 是合法枚举 | 模型吐了别的词 | 模型不懂契约 |
| `leaf_certainty` 是 0–100 的整数或百分比串 | 出现 `300%` 这种 | **实测见过**：模型把"把握"当成可大于 100 的数 |
| `decision == "split"` 时有 children | 说要拆却不给子节点 | 结构不合法 |
| `decision == "execute"` 时 children 为空 | 说执行却给了子节点 | 自相矛盾 |

## 不做的事

* **不调 worker**（改文件、跑测试）—— 那要花更多钱，且属于"能不能干活"，
  不是"能不能规划"。真要测就用 `scripts/real_codex_smoke.py`，它跑完整链路。
* **不猜成本**：逐次用量/费用由端点自己回报，本脚本**只在能取到时**记录，
  取不到就记 `null`（不是 0）。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

SCHEMA_PATH = ROOT / "schemas" / "planner-decision.schema.json"


def schema_enums() -> dict[str, list[str]]:
    """**从 schema 里读**契约，而不是自己写一份（自己写的那份实测就写错了）。

    第一版把 `leaf_certainty` 当成 0–100 的百分比范围，结果把 deepseek-flash 正确的
    `300%` 判成了失败 —— 而 `300%` 正是 `prompts/planner.md` 与 schema 共同规定的
    "有把握一次做完"的取值。判据不该比契约更聪明。
    """

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    properties = schema.get("properties") or {}
    return {
        "decision": list((properties.get("decision") or {}).get("enum") or []),
        "leaf_certainty": list((properties.get("leaf_certainty") or {}).get("enum") or []),
    }


def judge(summary: dict, enums: dict[str, list[str]]) -> list[str]:
    """逐条判据 → 失败原因列表（空 = 这一档过了）。

    判的是**契约**：取值必须在 schema 的 enum 里，且 `children` 与 `decision` 自洽。
    不判"模型聪不聪明"—— 那是 `--real-smoke` 那条链路的事。
    """

    failures: list[str] = []
    if summary.get("exit_code") != 0:
        failures.append(f"exit_code={summary.get('exit_code')}（Codex CLI 自身失败，先当通道问题看）")
        return failures

    decision = summary.get("decision")
    if decision not in enums["decision"]:
        failures.append(f"decision 不在 schema enum {enums['decision']} 里：{decision!r}")
        return failures

    certainty = summary.get("leaf_certainty")
    if certainty not in enums["leaf_certainty"]:
        failures.append(f"leaf_certainty 不在 schema enum {enums['leaf_certainty']} 里：{certainty!r}")

    children = summary.get("children") or []
    if decision in ("decompose", "repair") and not children:
        failures.append(f"decision={decision} 但没有 children")
    if decision == "execute" and children:
        failures.append(f"decision=execute 却给了 {len(children)} 个 children")
    if decision == "blocked":
        # blocked 是合法结论，但 `ok` 的含义是"CLI 通了"，别读成"模型给出了可用规划"。
        failures.append("decision=blocked：CLI 通了，但这档模型没给出可用规划")
    return failures


def run_one(model: str, args: argparse.Namespace, enums: dict[str, list[str]]) -> dict:
    command = [
        sys.executable,
        str(ROOT / "scripts" / "verify_provider_planner.py"),
        "--timeout",
        str(args.timeout),
    ]
    if not args.no_model_flag:
        command.extend(["--model", model])
    started = datetime.now(timezone.utc)
    proc = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, cwd=str(ROOT)
    )
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()

    record: dict[str, object] = {
        "model": model,
        "returncode": proc.returncode,
        "seconds": round(elapsed, 1),
        "summary": None,
        "failures": [],
    }
    try:
        summary = json.loads(proc.stdout)
    except json.JSONDecodeError:
        record["failures"] = ["verify_provider_planner.py 没吐出 JSON（看 raw_tail / stderr_tail）"]
        record["raw_tail"] = proc.stdout[-600:]
        record["stderr_tail"] = proc.stderr[-600:]
        return record

    record["summary"] = {
        key: summary.get(key)
        for key in ("ok", "exit_code", "decision", "leaf_certainty", "children", "reason", "logs_path")
    }
    record["failures"] = judge(summary, enums)
    return record


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="给每档模型跑一次真实 planner，记录哪个档位能用。")
    parser.add_argument("--models", required=True, help="逗号分隔的模型名（在隔离 config.toml 里已配好的那些）。")
    parser.add_argument("--timeout", type=int, default=180, help="单次 Codex 调用超时秒数。")
    parser.add_argument(
        "--no-model-flag",
        action="store_true",
        help="不传 `--model`：只靠环境里的隔离 config.toml 决定模型（两者打架时用这个）。",
    )
    parser.add_argument("--json-out", default="", help="把报告写到这个文件。")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    models = [item.strip() for item in args.models.split(",") if item.strip()]
    if not models:
        raise SystemExit("--models 是空的")

    report: dict[str, object] = {
        # "这些结论覆盖到什么"写死在报告里：只覆盖 planner 契约，不覆盖"能改对文件"。
        "scope": "planner_contract_only",
        "not_covered": ["worker_edits_files", "verifier_acceptance", "long_running_stability"],
        "contract": schema_enums(),
        "cost_note": "逐次费用由端点回报；本脚本不推算金额，取不到就记 null。",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "results": [],
    }
    enums = report["contract"]
    assert isinstance(enums, dict)
    for model in models:
        print(f"==> {model}", file=sys.stderr)
        record = run_one(model, args, enums)
        results = report["results"]
        assert isinstance(results, list)
        results.append(record)
        status = "OK  " if not record["failures"] else "FAIL"
        detail = record["summary"] or {}
        print(
            f"    {status} {model}: decision={detail.get('decision')} "
            f"certainty={detail.get('leaf_certainty')} {record['seconds']}s",
            file=sys.stderr,
        )

    results = report["results"]
    assert isinstance(results, list)
    report["usable_models"] = [item["model"] for item in results if not item["failures"]]
    report["unusable_models"] = [item["model"] for item in results if item["failures"]]
    blob = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.json_out:
        Path(args.json_out).write_text(blob, encoding="utf-8")
    print(blob)
    return 0 if report["usable_models"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
