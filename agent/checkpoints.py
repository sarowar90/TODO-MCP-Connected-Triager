"""Workspace checkpointing: snapshot the outbox, roll back to any snapshot.

Why this exists alongside the SDK's own file checkpointing
----------------------------------------------------------
The SDK tracks Write/Edit/NotebookEdit calls and can `rewind_files()` to a
checkpoint UUID. Two properties make it insufficient on its own here:

  * **Checkpoints are tied to the session that created them.** This agent runs
    *one session per step* (see plan.py), so a checkpoint taken during the
    triage of message 1 cannot be rewound from the digest step's session. The
    thing most worth undoing — "the digest step trashed the tickets the earlier
    steps produced" — spans sessions, and is exactly what the SDK cannot cover.
  * **File content only.** Creating or deleting files is not fully undone.

So the SDK's mechanism protects *within* a step, and this one protects *across*
the whole run. They are complementary, not redundant.

A checkpoint is a plain copy of every file under the outbox plus a manifest of
sha256 digests. Restoring makes the outbox match the snapshot exactly: modified
files are rewritten, deleted files come back, and files created after the
snapshot are removed.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from fs_policy import OUTBOX, WORKSPACE

CHECKPOINT_DIR = WORKSPACE / ".checkpoints"
MANIFEST = "manifest.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


@dataclass
class Checkpoint:
    id: str
    label: str
    created_at: str
    files: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        return f"{self.id}  {self.label}  ({len(self.files)} file(s))"


@dataclass
class RestoreReport:
    checkpoint: Checkpoint
    restored: list[str] = field(default_factory=list)
    recreated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.restored or self.recreated or self.removed)

    def describe(self) -> str:
        if not self.changed:
            return "nothing to roll back - the outbox already matches"
        parts = []
        if self.restored:
            parts.append(f"{len(self.restored)} restored")
        if self.recreated:
            parts.append(f"{len(self.recreated)} recreated")
        if self.removed:
            parts.append(f"{len(self.removed)} removed")
        return ", ".join(parts)


class CheckpointStore:
    """Snapshots of a directory tree, taken and restored by label."""

    def __init__(self, root: Path = OUTBOX, store: Path = CHECKPOINT_DIR) -> None:
        self.root = root
        self.store = store

    # --- internals -----------------------------------------------------------

    def _tracked(self) -> list[Path]:
        if not self.root.exists():
            return []
        return sorted(
            p for p in self.root.rglob("*") if p.is_file() and p.name != ".gitkeep"
        )

    def _rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def directory_for(self, checkpoint_id: str) -> Path:
        """Where a checkpoint's copies live. Owned by the store, not the
        Checkpoint, so a store pointed at a different root cannot write into
        the default one."""
        return self.store / checkpoint_id

    def _next_id(self) -> str:
        existing = len(self.list())
        stamp = datetime.now(timezone.utc).strftime("%H%M%S")
        return f"cp{existing + 1:02d}-{stamp}"

    # --- public API ----------------------------------------------------------

    def create(self, label: str) -> Checkpoint:
        """Snapshot every file under the root."""
        self.store.mkdir(parents=True, exist_ok=True)
        checkpoint = Checkpoint(
            id=self._next_id(),
            label=label,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        target = self.directory_for(checkpoint.id)
        target.mkdir(parents=True, exist_ok=True)

        for source in self._tracked():
            rel = self._rel(source)
            checkpoint.files[rel] = _sha256(source)
            destination = target / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

        (target / MANIFEST).write_text(
            json.dumps(
                {
                    "id": checkpoint.id,
                    "label": checkpoint.label,
                    "created_at": checkpoint.created_at,
                    "files": checkpoint.files,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return checkpoint

    def list(self) -> list[Checkpoint]:
        if not self.store.exists():
            return []
        found = []
        for manifest in sorted(self.store.glob(f"*/{MANIFEST}")):
            data = json.loads(manifest.read_text(encoding="utf-8"))
            found.append(
                Checkpoint(
                    id=data["id"],
                    label=data["label"],
                    created_at=data["created_at"],
                    files=data["files"],
                )
            )
        return found

    def get(self, checkpoint_id: str) -> Checkpoint | None:
        for checkpoint in self.list():
            if checkpoint.id == checkpoint_id:
                return checkpoint
        return None

    def latest(self) -> Checkpoint | None:
        found = self.list()
        return found[-1] if found else None

    def drift(self, checkpoint: Checkpoint) -> dict[str, list[str]]:
        """What has changed under the root since this checkpoint."""
        now = {self._rel(p): _sha256(p) for p in self._tracked()}
        then = checkpoint.files
        return {
            "modified": sorted(k for k in now.keys() & then.keys() if now[k] != then[k]),
            "added": sorted(now.keys() - then.keys()),
            "deleted": sorted(then.keys() - now.keys()),
        }

    def restore(self, checkpoint_id: str) -> RestoreReport:
        """Make the root match the snapshot exactly."""
        checkpoint = self.get(checkpoint_id)
        if checkpoint is None:
            raise KeyError(f"no such checkpoint: {checkpoint_id}")

        report = RestoreReport(checkpoint=checkpoint)
        self.root.mkdir(parents=True, exist_ok=True)

        # Rewrite or recreate everything the snapshot knows about.
        for rel, digest in checkpoint.files.items():
            source = self.directory_for(checkpoint.id) / rel
            target = self.root / rel
            if not source.is_file():
                continue
            if target.is_file():
                if _sha256(target) == digest:
                    report.unchanged.append(rel)
                    continue
                report.restored.append(rel)
            else:
                report.recreated.append(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

        # Remove anything created after the snapshot.
        for path in self._tracked():
            rel = self._rel(path)
            if rel not in checkpoint.files:
                path.unlink()
                report.removed.append(rel)

        return report

    def clear(self) -> None:
        """Drop all checkpoints. Used between runs and by the tests."""
        if self.store.exists():
            shutil.rmtree(self.store)
