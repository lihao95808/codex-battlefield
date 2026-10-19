"""Opt-in real Blender smoke test for the tool delivered by Battlefield runner.

Run from the repository root with api/.venv/Scripts/python.exe.
No model/API calls and no orchestrator task records: this verifies tool deployment only.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))


def main() -> None:
    from app import runner_tools
    from app.config import Settings
    from app.models import TaskNode
    from app.prompts import worker_prompt

    workspace = ROOT / "workspace" / "scene3d-runtime-smoke"
    settings = Settings(workspace_dir=workspace)
    task = TaskNode(id="scene3d-smoke", title="3D Blender smoke", description="Verify a box assembly")
    prompt = worker_prompt(settings, task)
    assert "SCENE3D_TOOL" in prompt
    tool = runner_tools.ensure_scene3d_tool(workspace)
    output = workspace / "output"
    output.mkdir(parents=True, exist_ok=True)
    declaration = output / "scene.declaration.json"
    parts = output / "scene.parts.json"
    started = time.monotonic()
    commands = [
        ["declaration", "--out", str(declaration)],
        ["reference", "--declaration", str(declaration), "--out", str(parts)],
        ["validate", "--declaration", str(declaration), "--parts", str(parts), "--json"],
        ["render", "--declaration", str(declaration), "--parts", str(parts), "--out", str(output),
         "--views", "hero", "--width", "900", "--height", "620", "--samples", "24"],
    ]
    log_paths = []
    for i, args in enumerate(commands):
        log = output / f"step-{i}.log"
        with log.open("wb") as stream:
            proc = subprocess.run([sys.executable, str(tool), *args], cwd=workspace, stdout=stream,
                                  stderr=subprocess.STDOUT, timeout=180, check=False)
        if proc.returncode:
            raise RuntimeError(f"step {args[0]} failed ({proc.returncode}), see {log}")
        log_paths.append(str(log))
    rendered = json.loads(Path(log_paths[-1]).read_text(encoding="utf-8"))
    report = {"scope": "runner tool deployment, reference input, NOT a model task tree",
              "elapsed_seconds": round(time.monotonic() - started, 2), "render": rendered}
    (output / "smoke-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
