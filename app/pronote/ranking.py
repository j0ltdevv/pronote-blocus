"""Classement : en retard d'abord, rien de caché.

Une matière non prioritaire n'est JAMAIS masquée : elle descend.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .normalize import Item

NO_PRIORITY = 99


@dataclass(frozen=True)
class Row:
    item: Item
    summary: str
    action: str
    effort: str

    @property
    def key(self) -> str:
        return self.item.key


def _fallback(item: Item) -> tuple[str, str, str]:
    return (item.body[:140], item.title, "moyen")


def to_row(item: Item, summary: dict | None) -> Row:
    s = summary or {}
    fb_summary, fb_action, fb_effort = _fallback(item)
    return Row(
        item=item,
        summary=(s.get("summary") or fb_summary),
        action=(s.get("action") or fb_action),
        effort=(s.get("effort") or fb_effort),
    )


def sort_key(row: Row, priorities: dict[str, int], today: date) -> tuple:
    item = row.item
    due = item.due_date
    urgency_flag = 0 if (item.kind == "controle" and due is not None and due >= today) else 1
    age = (today - due).days if (due is not None and due < today) else 0
    return (
        urgency_flag,
        -age,
        priorities.get(item.subject, NO_PRIORITY),
        due or date.max,
        len(item.body),
    )


def split_sections(
    items: list[Item],
    summary: dict[str, dict],
    priorities: dict[str, int],
    today: date,
    done: set[str] | None = None,
    include_done: bool = False,
) -> tuple[list[Row], list[Row]]:
    """(retard, à venir). Les `cours` sont exclus : ils alimentent /cours."""
    done = done or set()
    overdue: list[Row] = []
    upcoming: list[Row] = []
    for item in items:
        if item.kind == "cours" or (not include_done and item.key in done):
            continue
        row = to_row(item, summary.get(item.key))
        if item.due_date is not None and item.due_date < today:
            overdue.append(row)
        else:
            upcoming.append(row)
    overdue.sort(key=lambda r: sort_key(r, priorities, today))
    upcoming.sort(key=lambda r: sort_key(r, priorities, today))
    return overdue, upcoming


def group_cours(items: list[Item], priorities: dict[str, int]) -> list[tuple[str, list[Item]]]:
    """Par matière (ordre de priorité puis alphabétique), récent d'abord."""
    buckets: dict[str, list[Item]] = {}
    for item in items:
        if item.kind != "cours":
            continue
        buckets.setdefault(item.subject, []).append(item)
    ordered = sorted(
        buckets.items(),
        key=lambda kv: (priorities.get(kv[0], NO_PRIORITY), kv[0].lower()),
    )
    out = []
    for subject, lessons in ordered:
        lessons.sort(key=lambda i: (i.due_date or date.min), reverse=True)
        out.append((subject, lessons))
    return out
