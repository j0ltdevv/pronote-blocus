"""Rendu Telegram : une seule porte de sortie, toujours échappée.

Tout le HTML envoyé à Telegram passe par `esc` puis par `chunk`. Telegram
plafonne un message à 4096 caractères : on vise 3500 et on découpe sur les
sauts de ligne pour ne jamais couper une ligne d'item en deux quand c'est
évitable. Un décalage ici rendrait `/fait N` faux, donc la numérotation et le
découpage vivent au même endroit.
"""

from __future__ import annotations

import html
from datetime import date

from .pronote.normalize import Item
from .pronote.ranking import Row

# Telegram : 4096 caractères maximum par message. On garde une marge.
MESSAGE_LIMIT = 3500
SUITE = "\n… (suite)"
COURS_PER_SUBJECT = 5

HEADER_RETARD = (
    "⏰ <b>En retard</b> — trié par urgence, puis par tes matières prioritaires"
)
HEADER_AUJOURDHUI = "📅 <b>Aujourd'hui et demain</b> — trié par urgence"
HEADER_MATIERE = "📚 <b>Par matière</b> — en retard d'abord"
HEADER_COURS = "📖 <b>Cours à rattraper</b> — les plus récents d'abord"


def esc(text: object) -> str:
    """Échappe pour le parse_mode HTML de Telegram (jamais les guillemets)."""
    return html.escape(str(text), quote=False)


def chunk(html: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Découpe un message en morceaux ≤ `limit`.

    Coupe sur les `\\n` quand c'est possible ; une ligne seule plus longue que
    la largeur utile est coupée en tranches dures (borne garantie, aucune
    boucle possible). Chaque morceau sauf le dernier finit par « … (suite) ».
    """
    text = str(html or "")
    if not text.strip():
        return []

    limit = max(1, int(limit))
    # La marque de suite compte dans la limite : elle est réservée d'avance.
    suffix = SUITE if limit > len(SUITE) + 40 else ""
    width = max(1, limit - len(suffix))

    blocks: list[str] = []
    for line in text.split("\n"):
        if len(line) <= width:
            blocks.append(line)
        else:
            blocks.extend(line[i : i + width] for i in range(0, len(line), width))

    parts: list[str] = []
    current: list[str] = []
    size = 0
    for block in blocks:
        extra = len(block) + (1 if current else 0)
        if current and size + extra > width:
            parts.append("\n".join(current))
            current = [block]
            size = len(block)
        else:
            current.append(block)
            size += extra
    if current:
        parts.append("\n".join(current))

    last = len(parts) - 1
    return [p if i == last else p + suffix for i, p in enumerate(parts)]


def due_label(due: date | None, today: date) -> str:
    """« 9 j de retard », « aujourd'hui », « demain », « dans 5 j », « »."""
    if due is None:
        return ""
    days = (due - today).days
    if days < 0:
        return f"{-days} j de retard"
    if days == 0:
        return "aujourd'hui"
    if days == 1:
        return "demain"
    return f"dans {days} j"


def format_row(idx: int, row: Row, today: date) -> str:
    """Une ligne d'item, sans saut de ligne final (l'appelant joint)."""
    item = row.item
    line = f"{idx}. <b>{esc(item.subject)}</b> — {esc(row.action)}"
    label = due_label(item.due_date, today)
    if label:
        line += f" · {label}"
    if item.kind == "controle":
        line += " ⚠️"
    count = len(item.attachments or [])
    if count:
        line += f" 📎{count}"
    return line


def numbered_lines(rows: list[Row], today: date, start: int = 1) -> list[str]:
    return [format_row(start + i, row, today) for i, row in enumerate(rows)]


def paginate(
    rows: list[Row], page: int, per_page: int = 15
) -> tuple[list[Row], int, str]:
    """(items de la page, page bornée, note de bas de message)."""
    per_page = max(1, int(per_page))
    total = len(rows)
    nb_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(int(page or 1), nb_pages))
    start = (page - 1) * per_page
    visible = rows[start : start + per_page]
    rest = total - start - len(visible)
    if rest > 0:
        note = f"… et {rest} autres, /retard {page + 1}"
    elif nb_pages > 1:
        note = f"Page {page}/{nb_pages}"
    else:
        note = ""
    return visible, page, note


def compose(
    header: str,
    lines: list[str],
    footer: str | None = None,
    limit: int = MESSAGE_LIMIT,
) -> list[str]:
    """Assemble en-tête + lignes + pied, puis découpe. Aucun cas vide géré."""
    parts: list[str] = []
    if header:
        parts.append(header)
    parts.extend(line for line in lines if line)
    if footer:
        parts.append(footer)
    return chunk("\n".join(parts), limit)


def _cours_label(item: Item) -> str:
    text = item.title or item.body or ""
    if len(text) > 100:
        text = text[:100].rstrip() + "…"
    return text


def format_cours(groups: list[tuple[str, list[Item]]]) -> tuple[list[str], int]:
    """5 cours par matière, récents d'abord. Rend (lignes, nb masqués)."""
    lines: list[str] = []
    hidden = 0
    for subject, lessons in groups:
        ordered = sorted(
            lessons, key=lambda i: i.due_date or date.min, reverse=True
        )
        hidden += max(0, len(ordered) - COURS_PER_SUBJECT)
        for item in ordered[:COURS_PER_SUBJECT]:
            stamp = item.due_date.isoformat() if item.due_date else "sans date"
            lines.append(
                f"<b>{esc(subject)}</b> — {esc(_cours_label(item))}\n    {stamp}"
            )
    return lines, hidden
