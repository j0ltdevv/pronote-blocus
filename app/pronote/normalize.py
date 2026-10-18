"""Normalisation : du brut PRONOTE à un `Item` comparable entre élèves.

`key` est un hash de CONTENU, jamais un identifiant PRONOTE : PRONOTE
régénère tous ses ids à chaque session (même `Information.id` change à chaque
reconnexion). Les noms de pièces jointes entrent dans le hash, sinon un PDF
ajouté à un devoir existant ne produirait aucune mise à jour.
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from ..config import today as _today
from .dates import resolve_due

CONTROLE_RE = re.compile(
    r"\b(?:contr[oô]le|évaluation|evaluation|interrogation|ds\b|dm\s+not[ée])", re.I
)
_SPACES = re.compile(r"\s+")


@dataclass(frozen=True)
class Item:
    key: str
    source: str
    subject: str
    title: str
    body: str
    due_date: date | None
    kind: str
    attachments: list[str] = field(default_factory=list)


def clean_text(raw: Any) -> str:
    """HTML → texte, espaces compactés, casse PRÉSERVÉE (affichage)."""
    if not raw:
        return ""
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(raw)))
    return _SPACES.sub(" ", text).strip()


def short_title(body: str, limit: int = 90) -> str:
    """Première phrase, tronquée : sert de titre lisible."""
    if not body:
        return ""
    first = re.split(r"(?<=[.!?])\s+", body, maxsplit=1)[0]
    if len(first) <= limit:
        return first
    cut = first[:limit].rstrip()
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut + "…"


def content_key(
    *,
    kind: str,
    subject: str,
    body: str,
    due_date: date | None,
    attachments: list[str],
) -> str:
    names = ",".join(sorted(attachments))
    payload = (
        f"{kind}|{subject}|{_SPACES.sub(' ', body).strip().lower()}|"
        f"{due_date.isoformat() if due_date else ''}|{names}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def make_item(
    *,
    kind: str,
    source: str,
    subject: str,
    body: str,
    due_date: date | None,
    attachments: list[str] | None = None,
    title: str | None = None,
) -> Item:
    attachments = [a for a in (attachments or []) if a]
    subject = subject or "(matière inconnue)"
    return Item(
        key=content_key(
            kind=kind, subject=subject, body=body, due_date=due_date, attachments=attachments
        ),
        source=source,
        subject=subject,
        title=(title or short_title(body)) or "(sans titre)",
        body=body,
        due_date=due_date,
        kind=kind,
        attachments=attachments,
    )


def subject_name(obj: Any) -> str:
    name = getattr(getattr(obj, "subject", None), "name", None)
    if name:
        return clean_text(name)
    return clean_text(getattr(obj, "subject", "")) or "(matière inconnue)"


def attachment_names(files: Any) -> list[str]:
    out = []
    for f in files or []:
        name = getattr(f, "name", None)
        if name:
            out.append(clean_text(name))
    return out


@dataclass
class RawBundle:
    """Tout ce qu'une synchro a ramené, avant normalisation."""

    homework: list = field(default_factory=list)
    lessons: list = field(default_factory=list)
    contents: dict[str, Any] = field(default_factory=dict)  # slot -> LessonContent
    infos: list = field(default_factory=list)
    discussions: list = field(default_factory=list)  # [(sujet, composer)]
    extracted: dict[str, dict | None] = field(default_factory=dict)  # sujet -> extraction
    today: date | None = None


def lesson_slot(lesson: Any) -> str:
    start = getattr(lesson, "start", None)
    stamp = start.strftime("%Y-%m-%d %H:%M") if start else "?"
    return f"{stamp}|{subject_name(lesson)}"


def _content_fields(content: Any) -> tuple[str, str, list[str]]:
    if content is None:
        return "", "", []
    return (
        clean_text(getattr(content, "title", "")),
        clean_text(getattr(content, "description", "")),
        attachment_names(getattr(content, "files", None)),
    )


def build_items(client: Any, raw: RawBundle) -> list[Item]:
    """Rend les items des cinq sources, dédoublonnés par `key`."""
    base = raw.today or _today()
    out: dict[str, Item] = {}

    def add(item: Item) -> None:
        out.setdefault(item.key, item)

    # 1. Travail à faire
    for hw in raw.homework:
        body = clean_text(getattr(hw, "description", ""))
        if not body:
            continue
        add(
            make_item(
                kind="devoir",
                source="travail_a_faire",
                subject=subject_name(hw),
                body=body,
                due_date=resolve_due(getattr(hw, "date", None), body, None, base),
                attachments=attachment_names(getattr(hw, "files", None)),
            )
        )

    # 2. Cahier de textes : un `cours` toujours, un `controle` en plus
    for lesson in raw.lessons:
        if getattr(lesson, "canceled", False):
            continue
        content = raw.contents.get(lesson_slot(lesson))
        titre, description, files = _content_fields(content)
        body = clean_text(f"{titre} {description}")
        if not body:
            continue
        start = getattr(lesson, "start", None)
        day = start.date() if hasattr(start, "date") else None
        subject = subject_name(lesson)
        attach = attachment_names(getattr(content, "files", None))
        add(
            make_item(
                kind="cours",
                source="cahier_de_textes",
                subject=subject,
                body=body,
                due_date=day,
                attachments=attach,
                title=titre or None,
            )
        )
        probe = f"{titre} {description}"
        if CONTROLE_RE.search(probe):
            add(
                make_item(
                    kind="controle",
                    source="cahier_de_textes",
                    subject=subject,
                    body=body,
                    due_date=resolve_due(None, probe, None, base) or day,
                    attachments=attach,
                    title=titre or None,
                )
            )

    # 3. Informations et sondages — filtrées par DATE, jamais par `read`
    for info in raw.infos:
        title = clean_text(getattr(info, "title", ""))
        try:
            body_raw = info.content() if callable(getattr(info, "content", None)) else ""
        except Exception:
            body_raw = ""
        body = clean_text(body_raw)
        text = clean_text(f"{title}. {body}").strip(". ").strip()
        if not text:
            continue
        category = getattr(info, "category", None)
        subject = clean_text(getattr(category, "name", "") or category) or "Vie du lycée"
        add(
            make_item(
                kind="info",
                source="informations",
                subject=subject,
                body=text,
                due_date=resolve_due(None, text, None, base),
                attachments=attachment_names(getattr(info, "attachments", None)),
                title=title or None,
            )
        )

    # 4. Messagerie : le corps est délégué à l'extraction (§8)
    for subject, composer in raw.discussions:
        extracted = raw.extracted.get(subject)
        detail = clean_text((extracted or {}).get("detail"))
        if detail:
            add(
                make_item(
                    kind="devoir",
                    source="messagerie",
                    subject=clean_text(extracted.get("matiere") or subject) or subject,
                    body=detail,
                    due_date=resolve_due(None, detail, extracted.get("due"), base),
                )
            )
            continue
        composed = clean_text(composer())
        if not composed:
            continue
        add(
            make_item(
                kind="message",
                source="messagerie",
                subject=subject,
                body=composed,
                due_date=resolve_due(None, composed, None, base),
                title=f"Message — {subject}",
            )
        )

    return list(out.values())
