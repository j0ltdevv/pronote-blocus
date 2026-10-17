"""Résumés et réponses LLM — jamais bloquants.

Règle absolue : aucune réponse du bot ne dépend du LLM. Chaque fonction
publique a un repli déterministe, et aucune exception ne traverse cette API.
Les valeurs de repli ne sont JAMAIS écrites dans `summaries`, sinon le cache
se figerait sur du dégradé.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable, Sequence

log = logging.getLogger(__name__)

import httpx

from . import db
from .config import Config
from .config import today as _today
from .pronote import dates

BATCH_SIZE = 8
EFFORTS = ("court", "moyen", "long")
_FENCE = re.compile(r"^```(?:json)?|```$", re.M)
_OBJET = re.compile(r"\{.*\}", re.S)

# Prompt d'extraction repris TEL QUEL du bot en production sur cet établissement.
_EXTRACT_PROMPT = (
    "Nous sommes le {today}. Tu extrais les devoirs des messages de profs Pronote. "
    'Réponds UNIQUEMENT ce JSON : {{"matiere": "...", "detail": "...", "due": "AAAA-MM-JJ" ou null}}. '
    "matiere = la matière déduite du message ; detail = le travail à faire à la maison, court et "
    "fidèle ; due = l'échéance SI le message en donne une. Si le message ne contient AUCUN travail "
    "à faire à la maison, réponds exactement : null"
)

_SUMMARY_SYSTEM = (
    "Tu résumes des devoirs de lycée pour un élève français. Réponds UNIQUEMENT un objet JSON "
    'de la forme {"items":[{"key":"…","summary":"≤120 caractères","action":"verbe à '
    'l\'infinitif + objet","effort":"court|moyen|long"}]}. summary fait 120 caractères maximum ; '
    "action commence par un verbe à l'infinitif suivi de son objet ; effort vaut court, moyen ou "
    "long. Recopie exactement la valeur de key fournie. N'invente AUCUNE information absente du "
    "texte source."
)

_CHAT_SYSTEM = (
    "Tu réponds à un lycéen en français, en 3 phrases maximum. Tu ne cites que les devoirs "
    "présents dans les DONNÉES. Si la réponse n'y est pas, dis-le. Ne donne jamais de conseil "
    "sur une matière à abandonner."
)

_WEEKLY_SYSTEM = (
    "Tu résumes une semaine de devoirs pour un lycéen français, en 5 lignes maximum, une ligne "
    "par priorité. Reste strictement fidèle aux éléments fournis, tutoie l'élève, n'invente rien."
)


# ─────────────────────────── transport ───────────────────────────


def _chat(
    cfg: Config,
    messages: list[dict[str, str]],
    *,
    max_tokens: int,
    timeout: float = 15,
    json_mode: bool = False,
) -> str | None:
    """Un appel OpenAI-compatible. TOUTE erreur ⇒ None, jamais d'exception."""
    if not cfg.llm_enabled:
        return None
    payload: dict[str, Any] = {
        "model": cfg.llm_model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    try:
        response = httpx.post(
            f"{cfg.llm_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {cfg.llm_api_key}"},
            json=payload,
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]
    except Exception as exc:  # httpx.HTTPError, timeout, JSON cassé, forme inattendue…
        log.warning("appel LLM échoué : %s", exc)
        return None
    if not isinstance(content, str) or not content.strip():
        return None
    return content


def _unfence(text: str) -> str:
    """Retire un éventuel bloc Markdown ```json … ``` avant json.loads."""
    return _FENCE.sub("", text.strip()).strip()


def _load_json(text: str) -> Any:
    try:
        return json.loads(_unfence(text))
    except (ValueError, TypeError):
        return None


# ─────────────────────────── repli ───────────────────────────


def _fallback(body: str, title: str | None = None) -> dict[str, str]:
    body = (body or "").strip()
    label = (title or "").strip()
    if not label:
        label = body.split("\n", 1)[0][:90].strip() or body[:90]
    return {
        "summary": body[:140],
        "action": label[:90],
        "effort": "moyen",
    }


# ─────────────────────────── résumés ───────────────────────────


def _summary_messages(pairs: Sequence[tuple[str, str]]) -> list[dict[str, str]]:
    lines = []
    for key, body in pairs:
        lines.append(f"- key: {key}\n  texte: {(body or '')[:1200]}")
    return [
        {"role": "system", "content": _SUMMARY_SYSTEM},
        {"role": "user", "content": "Devoirs à résumer :\n" + "\n".join(lines)},
    ]


def _clean_entry(entry: Any, wanted: set[str]) -> tuple[str, dict[str, str]] | None:
    if not isinstance(entry, dict):
        return None
    key = entry.get("key")
    if not isinstance(key, str) or key not in wanted:
        return None
    summary = entry.get("summary")
    action = entry.get("action")
    effort = entry.get("effort")
    if not isinstance(summary, str) or not summary.strip():
        return None
    if not isinstance(action, str) or not action.strip():
        return None
    if not isinstance(effort, str) or effort.strip().lower() not in EFFORTS:
        effort = "moyen"
    else:
        effort = effort.strip().lower()
    return key, {
        "summary": summary.strip()[:140],
        "action": action.strip()[:140],
        "effort": effort,
    }


def _summarize_real(pairs: Sequence[tuple[str, str]], cfg: Config) -> dict[str, dict[str, str]]:
    """Uniquement les résumés réellement produits par le modèle (jamais de repli)."""
    out: dict[str, dict[str, str]] = {}
    for start in range(0, len(pairs), BATCH_SIZE):
        batch = list(pairs[start : start + BATCH_SIZE])
        wanted = {key for key, _ in batch}
        text = _chat(
            cfg,
            _summary_messages(batch),
            max_tokens=900,
            timeout=15,
            json_mode=True,
        )
        if text is None:
            continue
        data = _load_json(text)
        if isinstance(data, dict):
            entries = data.get("items")
        else:
            entries = data
        if not isinstance(entries, list):
            continue
        for entry in entries:
            cleaned = _clean_entry(entry, wanted)
            if cleaned is not None:
                out[cleaned[0]] = cleaned[1]
    return out


def summarize_many(pairs: list[tuple[str, str]], cfg: Config) -> dict[str, dict]:
    """[(key, body)] → {key: {"summary","action","effort"}}. Toute clé demandée sort."""
    bodies: dict[str, str] = {}
    for key, body in pairs:
        bodies.setdefault(key, body or "")
    out = {key: _fallback(body) for key, body in bodies.items()}
    if not bodies:
        return out
    try:
        out.update(_summarize_real(list(bodies.items()), cfg))
    except Exception:
        pass
    return out


def ensure_summaries(conn, items: Iterable, cfg: Config) -> dict[str, dict]:
    """Cache partagé par contenu : seul ce qui manque part au modèle."""
    items = list(items)
    merged: dict[str, dict] = {}
    for item in items:
        merged.setdefault(item.key, _fallback(item.body, item.title))
    if not merged:
        return merged

    try:
        cached = db.get_summaries(conn, list(merged), cfg.llm_model)
    except Exception:
        cached = {}
    for key, value in cached.items():
        if key in merged:
            merged[key] = value

    todo: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in items:
        if item.key in cached or item.key in seen:
            continue
        seen.add(item.key)
        todo.append((item.key, item.body))

    if not todo or not cfg.llm_enabled:
        return merged

    try:
        real = _summarize_real(todo, cfg)
    except Exception:
        return merged
    if not real:
        return merged
    try:
        db.put_summaries(conn, cfg.llm_model, real)
    except Exception:
        pass  # le cache ne doit jamais empêcher d'afficher la liste
    merged.update(real)
    return merged


# ─────────────────────────── devoir caché dans un message ───────────────────────────


def _extract_once(subject: str, content: str, cfg: Config) -> tuple[dict | None, bool]:
    """→ (devoir, réponse_interprétable). bool=False = panne, pas un « null »."""
    prompt = _EXTRACT_PROMPT.format(today=_today().isoformat())
    text = _chat(
        cfg,
        [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"Sujet de la discussion : {subject}\n\nMessage :\n{(content or '')[:800]}"},
        ],
        max_tokens=200,
        timeout=15,
    )
    if text is None:
        return None, False
    stripped = _unfence(text)
    if stripped.strip().lower().startswith("null"):
        return None, True
    match = _OBJET.search(stripped)
    if not match:
        return None, False
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None, False
    if not isinstance(data, dict):
        return None, True
    detail = data.get("detail")
    if not isinstance(detail, str) or not detail.strip():
        return None, True
    matiere = data.get("matiere")
    due = dates.check_llm_date(data.get("due"))
    return {
        "matiere": matiere.strip() if isinstance(matiere, str) else "",
        "detail": detail.strip(),
        "due": due,
    }, True


def extract_devoir_checked(subject: str, content: str, cfg: Config) -> tuple[dict | None, bool]:
    """→ (devoir, réponse_interprétable).

    `bool=False` = le modèle n'a pas pu être lu (panne, JSON cassé, timeout) :
    l'appelant ne doit RIEN mettre en cache, sinon une panne passagère fige
    « pas de devoir » pour la semaine. `bool=True` + `None` = vrai « null ».
    """
    if not cfg.llm_enabled or not (content or "").strip():
        return None, False
    for _ in range(2):
        try:
            found, interpretable = _extract_once(subject, content, cfg)
        except Exception:
            return None, False
        if found is not None:
            return found, True
        if not interpretable:
            return None, False
    return None, True


def extract_devoir(subject: str, content: str, cfg: Config) -> dict | None:
    """Extrait un devoir d'un message de prof ; un « null » est retenté une fois."""
    found, _interpretable = extract_devoir_checked(subject, content, cfg)
    return found


# ─────────────────────────── chat ancré ───────────────────────────


def chat_answer(question: str, data_block: str, cfg: Config) -> str | None:
    """Réponse ancrée sur les données de l'élève, 700 caractères maximum."""
    text = _chat(
        cfg,
        [
            {"role": "system", "content": _CHAT_SYSTEM},
            {"role": "user", "content": f"DONNÉES:\n{data_block}\n\nQUESTION: {question}"},
        ],
        max_tokens=400,
        timeout=15,
    )
    if text is None:
        return None
    answer = text.strip()
    return answer[:700] if answer else None


# ─────────────────────────── synthèse hebdomadaire ───────────────────────────


def _row_line(row) -> str:
    item = row.item
    due = item.due_date.isoformat() if item.due_date else "sans date"
    return f"- {item.subject} | {item.kind} | {due} | {row.summary} | action: {row.action}"


def weekly_synthesis(rows: Sequence, cfg: Config) -> str | None:
    """5 lignes maximum ; None si le LLM est indisponible (le parent replie)."""
    rows = list(rows)
    if not rows or not cfg.llm_enabled:
        return None
    text = _chat(
        cfg,
        [
            {"role": "system", "content": _WEEKLY_SYSTEM},
            {"role": "user", "content": "Éléments de la semaine :\n" + "\n".join(_row_line(r) for r in rows[:40])},
        ],
        max_tokens=350,
        timeout=15,
    )
    if text is None:
        return None
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    return "\n".join(lines[:5]) or None
