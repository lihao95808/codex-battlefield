# Codex Battlefield Pitfalls

## Nested PowerShell Quoting

Avoid complex inline commands:

- Bad: wrapping routine checks in `powershell.exe -NoProfile -Command ...` from inside Codex CLI. Codex already runs commands through the Windows shell.
- Bad: nested `python -c` with dictionaries, quotes, or paths.
- Bad: `> $null; if ($LASTEXITCODE -eq 0) ...` inside nested PowerShell.
- Bad: redirecting validation output to `NUL`.
- Better: `rg --files`.
- Better: `rg -n -F token path`.
- Better: `Test-Path -LiteralPath path`.
- Better: `python -m json.tool docs/file.json`.
- Better for workers that may write files: create a bounded script file, run it, then list that script/result in evidence when appropriate.

## Chinese Text

Terminal output can show valid UTF-8 as mojibake. Do not treat raw `rg` or PowerShell display of Chinese prose as authoritative.

Use Python to read and validate:

```powershell
python -m json.tool docs/report.json
```

For content checks, print ASCII-safe summaries such as `OK utf8-readable`, counts, keys, and file paths.

## Read-Only Verifier Discipline

Verifier and acceptance agents should not spend the run exploring many command variants. If one validation command is rejected by policy, stop retrying variants and decide from existing worker evidence, file existence, and focused checks.

## Godot

Use the Battlefield wrapper. A real Godot modal crash is an automation hazard, not a game-design finding. Static wrapper success is acceptable for structural gates unless the task explicitly requires captured runtime evidence.

## Visual Claims

Do not pass market-quality animation, hit feel, VFX, or UI polish on code existence alone. Require screenshot/frame/replay evidence, or return `visual_uncertain`/repair with concrete evidence-capture children.

## Parent Acceptance

Parent acceptance must verify the merged integration target, not only child local worktrees. A parent should reject children that are individually done but do not combine into the parent objective.
