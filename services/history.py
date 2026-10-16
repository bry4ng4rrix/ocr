"""Undo / redo of page edits (blocks, image settings, geometry), as page JSON snapshots.

A snapshot is taken *before* each change. Undo restores the page as it was and keeps the current
state for redo. Snapshots live in memory (session); the project file itself is always saved.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import config
from core.document_models import Page, Project


@dataclass
class _Entry:
    page_id: str
    label: str
    state: str          # Page JSON


@dataclass
class History:
    project_id: str = ""
    undo_stack: list[_Entry] = field(default_factory=list)
    redo_stack: list[_Entry] = field(default_factory=list)
    depth: int = config.UNDO_DEPTH

    def record(self, page: Page, label: str) -> None:
        """Call before changing `page`."""
        self.undo_stack.append(_Entry(page.id, label, page.model_dump_json()))
        del self.undo_stack[:-self.depth]
        self.redo_stack.clear()

    def can_undo(self) -> bool:
        return bool(self.undo_stack)

    def can_redo(self) -> bool:
        return bool(self.redo_stack)

    def undo_label(self) -> str:
        return self.undo_stack[-1].label if self.undo_stack else ""

    def redo_label(self) -> str:
        return self.redo_stack[-1].label if self.redo_stack else ""

    def _swap(self, project: Project, source: list[_Entry], target: list[_Entry]) -> str | None:
        while source:
            entry = source.pop()
            try:
                index = project.page_index(entry.page_id)
            except KeyError:
                continue        # the page was deleted since: skip this entry
            target.append(_Entry(entry.page_id, entry.label, project.pages[index].model_dump_json()))
            project.pages[index] = Page.model_validate_json(entry.state)
            return entry.label
        return None

    def undo(self, project: Project) -> str | None:
        """Restore the previous state of the last edited page; returns the undone label."""
        return self._swap(project, self.undo_stack, self.redo_stack)

    def redo(self, project: Project) -> str | None:
        return self._swap(project, self.redo_stack, self.undo_stack)

    def clear(self) -> None:
        self.undo_stack.clear()
        self.redo_stack.clear()
