# Checkpoint Git Anchor

Codex Battlefield keeps two kinds of recovery evidence:

- `workspace/full_checkpoints/<root>/<timestamp>/`: local full checkpoint, including the SQLite database copy, target project copy, and manifest.
- `workspace/checkpoint_ledger/<root>/<timestamp>.json`: lightweight Git ledger for GitHub/GitHub Desktop.

The ledger records the checkpoint manifest hash, database hash, target Godot repo commit/tag, and Battlefield repo tag. It is meant to be committed to Git. The full checkpoint directory is intentionally ignored by Git because it can become large.

When `CODEX_BATTLEFIELD_CHECKPOINT_LEDGER_ENABLED=1` (default), every full checkpoint automatically writes a lightweight ledger and updates `workspace/checkpoint_ledger/<root>/latest.json`.

In L4 Goal Mode, the watchdog also anchors each hourly checkpoint once by default: it commits the target Godot project, commits the lightweight ledger in the Battlefield repo, and creates matching annotated tags:

- `battlefield-<root>-<checkpoint>-target`
- `battlefield-<root>-<checkpoint>-battlefield`

Set `CODEX_BATTLEFIELD_CHECKPOINT_GIT_ANCHOR=off` before starting the watchdog to disable this, or set it to `ledger` to keep the older read-only ledger behavior.

## Create A Local Git Anchor

```powershell
cd C:\projects\codex-battlefield
python .\scripts\git_snapshot_anchor.py --root-id root-a50ce4a514 --init-git --commit --tag
```

This will:

- initialize Git in `C:\projects\codex-battlefield` if needed;
- initialize Git in the target Godot project if the checkpoint manifest has `target_source`;
- commit target project changes;
- create annotated tags for the target project and Battlefield ledger;
- write `workspace/checkpoint_ledger/<root>/<checkpoint>.json`.

## Publish With GitHub Desktop

Open both local repositories in GitHub Desktop:

- `C:\projects\codex-battlefield`
- `C:\projects\demo-target`

Use **Publish repository** or configure an existing remote. Then push commits and tags. If GitHub Desktop does not push tags automatically, use:

```powershell
git -C C:\projects\codex-battlefield push --tags
git -C C:\projects\demo-target push --tags
```

## Push With Existing Remote

If both repositories already have an `origin` remote:

```powershell
python .\scripts\git_snapshot_anchor.py --root-id root-a50ce4a514 --commit --tag --push
```

## Important Limits

Do not commit `workspace/full_checkpoints`, `workspace/state_snapshots`, `workspace/runs`, `workspace/worktrees`, or SQLite/WAL files into GitHub. Use the Git ledger and tags as the version number; keep large recovery files local or move them to a dedicated backup/Release/LFS flow later.
