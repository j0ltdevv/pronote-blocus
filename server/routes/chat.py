"""Route de chat : question libre sur les devoirs, réponse ancrée aux données."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import db, llm
from app.config import Config, today as _today
from app.pronote import ranking

from ..deps import CfgDep, DBDep, UserDep, require_csrf

router = APIRouter(
    prefix="/api/chat",
    tags=["chat"],
    dependencies=[Depends(require_csrf)],
)

#: Même plafond que le bot Telegram : 10 questions par compte et par jour.
QUOTA_QUESTIONS_PAR_JOUR = 10
MSG_QUOTA = (
    f"Quota de {QUOTA_QUESTIONS_PAR_JOUR} questions par jour atteint, reviens demain."
)


# ── models ─────────────────────────────────────────────────────────────


class ChatIn(BaseModel):
    question: str


class ChatOut(BaseModel):
    answer: str


# ── helpers ────────────────────────────────────────────────────────────


def _data_block(conn, cfg: Config, user_id: str, question: str) -> str:
    """Construit le bloc de données envoyé au LLM, comme `_data_block` du bot."""
    items = db.get_items(conn, user_id)
    summaries = llm.ensure_summaries(conn, items, cfg)
    priorities = db.get_priorities(conn, user_id)
    done = db.done_keys(conn, user_id)
    overdue, upcoming = ranking.split_sections(
        items, summaries, priorities, _today(), done
    )

    rows = list(overdue) + list(upcoming)

    # Filtre sur les matières mentionnées (conservé depuis le bot).
    asked = _mentioned_subjects(question, sorted({row.item.subject for row in rows}))
    if asked and len(asked) <= 3:
        rows = [r for r in rows if r.item.subject in asked]
    rows = rows[:40]

    lines = [
        f"[{row.key[:12]}] {row.item.subject} | {row.item.kind} | "
        f"{row.item.due_date.isoformat() if row.item.due_date else '-'} | {row.summary}"
        for row in rows
    ]

    q = (question or "").casefold()
    if any(mot in q for mot in ("cours", "rattraper", "résume", "resume")):
        cours = [i for i in items if i.kind == "cours"]
        cours.sort(key=lambda i: i.due_date or date.min, reverse=True)
        lines.append("COURS:")
        for item in cours[:10]:
            stamp = item.due_date.isoformat() if item.due_date else "?"
            lines.append(
                f"[{item.key[:12]}] {item.subject} | cours | {stamp} | {item.body[:160]}"
            )
    return "\n".join(lines)


_ALIAS_MATIERES = {
    "math": ("mathematiques",),
    "maths": ("mathematiques",),
    "svt": ("sciences de la vie",),
    "hg": ("histoire",),
    "hggsp": ("histoire",),
    "eps": ("education physique",),
    "philo": ("philosophie",),
    "techno": ("technologie",),
    "francais": ("francais",),
    "anglais": ("anglais",),
    "espagnol": ("espagnol",),
    "allemand": ("allemand",),
    "latin": ("latin",),
    "physique": ("physique",),
    "chimie": ("chimie",),
}
_MOTS_AMBIGUS = {"ses", "des", "les", "mes", "tes", "ces", "nos", "vos", "aux"}


def _fold(text: str) -> str:
    import unicodedata

    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _nomme(folded: str, q: str) -> bool:
    import re

    if not folded:
        return False
    if folded in _MOTS_AMBIGUS:
        return bool(
            re.search(
                rf"(?:^|[^a-z0-9])(?:en|de|d'|pour)\s+{re.escape(folded)}(?![a-z0-9])",
                q,
            )
        )
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(folded)}(?![a-z0-9])", q))


def _mentioned_subjects(question: str, subjects: list[str]) -> list[str]:
    import re

    q = _fold(question or "")
    if not q:
        return []
    words = {w for w in re.split(r"[^a-z0-9]+", q) if w}
    found: list[str] = []
    for subject in subjects:
        folded = _fold(subject)
        if _nomme(folded, q):
            found.append(subject)
            continue
        head = folded.split()[0] if folded.split() else folded
        if any(len(w) >= 5 and head.startswith(w) for w in words):
            found.append(subject)
            continue
        if any(
            alias in words
            and any(folded.startswith(target) for target in targets)
            for alias, targets in _ALIAS_MATIERES.items()
        ):
            found.append(subject)
    return found


# ── route ──────────────────────────────────────────────────────────────


@router.post("", response_model=ChatOut)
def chat(
    body: ChatIn,
    user: UserDep,
    conn: DBDep,
    cfg: CfgDep,
) -> ChatOut:
    """Question libre : réponse ancrée aux devoirs de l'élève."""
    user_id = user.user_id

    # Sans LLM, la réponse est une liste locale : aucune question consommée
    # (même règle que le bot Telegram).
    if not cfg.llm_enabled:
        return ChatOut(answer=_fallback_answer(conn, cfg, user_id, body.question))

    # Quota de 10 questions/jour, comme le bot Telegram.
    today_str = _today().isoformat()
    if not db.bump_llm_count(conn, user_id, today_str, QUOTA_QUESTIONS_PAR_JOUR):
        raise HTTPException(429, MSG_QUOTA)

    block = _data_block(conn, cfg, user_id, body.question)
    answer = llm.chat_answer(body.question, block, cfg)

    if answer:
        return ChatOut(answer=answer[:700])

    # Relai : le LLM a échoué, on renvoie la liste brutaile en repli.
    return ChatOut(answer=_fallback_answer(conn, cfg, user_id, body.question))


def _fallback_answer(conn, cfg: Config, user_id: str, question: str) -> str:
    """Replique le comportement du bot : liste les 3 premiers items."""
    items = db.get_items(conn, user_id)
    summaries = llm.ensure_summaries(conn, items, cfg)
    priorities = db.get_priorities(conn, user_id)
    done = db.done_keys(conn, user_id)
    overdue, upcoming = ranking.split_sections(
        items, summaries, priorities, _today(), done
    )
    rows = list(overdue) + list(upcoming)

    asked = _mentioned_subjects(question, sorted({row.item.subject for row in rows}))
    if asked and len(asked) <= 3:
        rows = [r for r in rows if r.item.subject in asked]
    rows = rows[:3]
    if not rows:
        return "Je n'ai pas pu réfléchir, et je n'ai aucun devoir en base. Rafraîchis d'abord."

    lines = []
    for i, row in enumerate(rows, start=1):
        du = row.item.due_date.isoformat() if row.item.due_date else "sans date"
        lines.append(f"{i}. {row.item.subject} — {row.action} · {du}")
    return "Je n'ai pas pu réfléchir, voilà ta liste :\n" + "\n".join(lines)
