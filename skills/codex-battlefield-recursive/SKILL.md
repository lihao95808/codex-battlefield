---
name: codex-battlefield-recursive
description: Use for Codex Battlefield recursive agents, parent acceptance, blocker recovery, Windows command safety, Godot validation, visual uncertainty, and skill-note capture.
---

# Codex Battlefield Recursive

This is the project-level hot-load skill for Codex Battlefield agents. Treat it as the shared operating manual for recursive planning, execution, verification, parent acceptance, and self-rescue.

## Core Rules

- Keep the original user goal and project constitution as the boundary. Do not shrink a large game goal into a tiny demo, and do not expand beyond the stated quality ceiling.
- A node is a leaf only when one Codex CLI run can 300% complete and verify it. If not, decompose into independent, balanced child tasks.
- A worker that fails validation once, cannot prove completion, or finds visual/technical uncertainty that can be reduced by more evidence must convert itself into a parent by returning suggested child tasks.
- Parent acceptance must integrate child outputs before marking the parent done. Child `done` status is necessary but not sufficient.
- Do not manually invent certainty. If evidence is weak, report the missing evidence and split work to produce it.

## Windows Command Safety

- Codex CLI already executes commands through the Windows shell. Do not wrap routine checks in another `powershell.exe -Command ...` or `cmd /c ...`.
- Run direct commands instead: `rg --files`, `rg -n -F token path`, `Test-Path -LiteralPath path`, `python -m json.tool file.json`, or `python scripts/tool.py --arg value`.
- Only use `powershell.exe -NoProfile -File script.ps1` when intentionally running a standalone `.ps1` child process.
- Treat any older note recommending `powershell.exe -NoProfile -Command ...` for routine checks as stale. Rewrite it into the current direct-command rule when merging skill notes.
- Do not use inline `python -c` inside nested shell strings. Prefer `python -m json.tool file.json`, a real script file, or a very small direct command.
- Do not redirect to `NUL`; let validation output print, rely on process exit codes, or use `Out-Null` only inside pure PowerShell.
- Do not use `$null`, `$LASTEXITCODE`, or other PowerShell variables inside nested shell strings; outer quoting can strip `$`. Run validation commands plainly and rely on process exit codes.
- Do not use Bash heredoc syntax such as `python - <<'PY'` under PowerShell.
- Use `rg` for ASCII tokens, file paths, and line numbers. Do not quote Chinese/user-visible prose from terminal output because Windows console rendering can mojibake valid UTF-8.
- For absence checks such as proving `???`/`????` corruption is absent, prefer the Battlefield `assert_no_match.py` helper when the prompt provides it. A plain `rg` no-match exits 1 and can pollute logs as a failed command even when the check semantically passes.
- For JSON validity checks, prefer the Battlefield `json_check.py` helper when the prompt provides it. It prints a compact summary instead of dumping large evidence JSON bodies into logs.
- Do not use `Get-Content`, `Set-Content`, or `type` for Chinese/user-visible file bodies. Read or write such text with Python `encoding='utf-8'`.
- Do not run `git status` or `git diff` unless `.git` exists in the current worktree.

## Godot Validation

- Use the Battlefield wrapper exactly: `python "<workspace>/tools/godot_with_timeout.py" --headless --path . --quit`.
- Do not bypass the wrapper to call the real Godot binary directly during automation. Real Godot may crash or show modal Windows error dialogs.
- Treat `GODOT_STATIC_CHECK ok` as the expected sandbox-safe validation signal when full runtime launch is unsafe.
- Every external command must be bounded by a timeout or watchdog. The Codex agent may run long, but child tools must not hang forever.

## Text And Asset Integrity

- Chinese Markdown/JSON/Godot text must remain readable UTF-8.
- Repeated `???` or `????` in user-visible Chinese text is irreversible corruption and must trigger repair/decomposition.
- For Chinese file creation through shell commands, use ASCII-safe writes: Python `\uXXXX` strings, base64-decoded UTF-8, or another path that avoids passing Chinese literals through PowerShell.
- Image/audio assets must be valid non-empty files with correct headers. Never satisfy references with zero-byte files or renamed text files.

## Visual Validation

- Objective visual evidence includes nonblank render output, actors/UI visible, frame or screenshot evidence for idle/run/attack/hit/hurt/death, VFX near hit points, and no obvious clipping or incoherent overlap.
- If visual evidence is missing but can be generated, return repair/children for screenshot, frame sequence, replay capture, frame-diff proxy, or visual report tasks.
- If only subjective quality remains, return `visual_uncertain` with the exact human visual review question. Do not pretend subjective arcade feel is proven.

## Parent Skill-Note Task

Each parent should have at most one lightweight skill-note child. Its job is not to build game content; its job is to capture reusable lessons from this parent subtree and report them upward.

Every parent layer must summarize all skill notes created below it:

- command patterns that failed or succeeded;
- encoding/Chinese readability issues;
- Godot launch or static-check issues;
- blocker reasons and retryability;
- visual evidence gaps;
- any new rule that should be promoted into this skill.

The parent summary must merge duplicates and similar issues instead of concatenating logs. Keep one rule per category/problem/solution cluster, record source nodes/files, and include a source count. The summary must be written under `docs/skill_notes/` so the next parent can integrate and merge it again.

The child should write a small JSON note under `docs/skill_notes/<TASK_ID>.json` in its assigned worktree and return changed_files/evidence. If it discovers a reusable rule that belongs in the global skill, report it clearly in `promote_to_project_skill` so the tool maintainer can promote it.

## Self-Rescue

- Before returning blocked for an unknown error, missing tool, validation failure, or external integration problem, search the configured Skill/KM URL if accessible.
- If Skill/KM needs human login, SSO, CAPTCHA, or permissions, report a human-auth blocker and continue other independent branches.
- Use blockers as parent opportunities: include source, category, retryability, evidence, and recommended parent action.

For detailed pitfall examples, read `references/pitfalls.md` when debugging command quoting, encoding, Godot, or visual-validation failures.
