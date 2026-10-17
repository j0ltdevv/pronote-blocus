"""Dates françaises tirées du texte, AVANT le modèle.

Un devoir caché dans un message n'a souvent aucune date structurée : elle est
dans le texte (« pour le 21 octobre », « vendredi »). Le LLM n'est que le
dernier recours, et ses dates passent par le même garde-fou.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from ..config import JOURS_FR, MOIS_FR

PAST = 15
FUTURE = 120

_MOIS_RE = "|".join(sorted(MOIS_FR, key=len, reverse=True))
_JOURS_RE = "|".join(JOURS_FR)


def _in_window(cand: date, base: date) -> bool:
    return base - timedelta(days=PAST) <= cand <= base + timedelta(days=FUTURE)


def date_explicite(text: str, base: date | None = None) -> str | None:
    """« pour le 21 octobre » → '2026-10-21' ; hors fenêtre → None."""
    if not text:
        return None
    base = base or date.today()
    m = re.search(rf"\b(\d{{1,2}})\s+({_MOIS_RE})\b", text, re.I)
    if not m:
        return None
    try:
        cand = date(base.year, MOIS_FR[m.group(2).lower()], int(m.group(1)))
    except (ValueError, KeyError):
        return None
    if _in_window(cand, base):
        return cand.isoformat()
    # « 5 janvier » en décembre = l'an prochain
    try:
        cand = date(base.year + 1, MOIS_FR[m.group(2).lower()], int(m.group(1)))
    except (ValueError, KeyError):
        return None
    return cand.isoformat() if _in_window(cand, base) else None


def prochain_jour(text: str, base: date | None = None) -> str | None:
    """« vendredi » → la prochaine occurrence, au plus tôt demain."""
    if not text:
        return None
    base = base or date.today()
    m = re.search(rf"\b({_JOURS_RE})\b", text, re.I)
    if not m:
        return None
    delta = (JOURS_FR[m.group(1).lower()] - base.weekday()) % 7 or 7
    return (base + timedelta(days=delta)).isoformat()


def check_llm_date(value: str | None, base: date | None = None) -> str | None:
    """Une date hallucinée (année passée, trop loin) vaut « pas de date »."""
    if not value:
        return None
    base = base or date.today()
    try:
        cand = date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
    return cand.isoformat() if _in_window(cand, base) else None


def resolve_due(
    structured: date | None,
    text: str,
    llm_due: str | None = None,
    base: date | None = None,
) -> date | None:
    """Hiérarchie : date PRONOTE > date explicite > jour de semaine > LLM."""
    base = base or date.today()
    if structured is not None:
        return structured
    for finder in (date_explicite, prochain_jour):
        found = finder(text or "", base)
        if found:
            return date.fromisoformat(found)
    checked = check_llm_date(llm_due, base)
    return date.fromisoformat(checked) if checked else None
