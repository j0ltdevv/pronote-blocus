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
import threading
import time
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

# Circuit ouvert anti-panne : un LLM injoignable mémorise l'échec `DOWN_SECONDS`
# secondes. Pendant ce laps, `_chat` rend `None` sans appeler le transport — ce
# qui évite de refaire des timeouts de 15 s à chaque affichage (`/retard`,
# synthèse, chat…) quand le serveur LLM est mort. Le repli local garantit
# l'affichage ; rien n'est écrit dans `summaries` tant que le LLM est down.
DOWN_SECONDS = 600
_LLM_DOWN_UNTIL: float = 0.0

# Le fournisseur compte ses requêtes *simultanées* par compte, pas les threads
# du serveur. Sans borne, N élèves sur un cache froid envoient leurs lots au
# même instant et se font refuser en 429 — ce qui, à cause du circuit ouvert,
# privait tout le monde du LLM. Quatre slots couvrent le débit utile (les lots
# de résumés partent en série). Mesuré le 18/10/2026 : 4 appels simultanés
# acceptés, refus au-delà.
MAX_CONCURRENT_CALLS = 4
_CALL_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_CALLS)

# Attente maximale d'un slot avant de rendre la main au repli. Les endpoints
# synchrones partagent le pool de threads du serveur (~40) avec les requêtes
# qui n'appellent PAS le LLM (liste des devoirs, cocher « fait »). Attendre
# sans borne derrière quatre appels de 15 s garerait tout le pool et gèlerait
# aussi ces requêtes-là : mieux vaut un repli déterministe immédiat.
SLOT_WAIT_SECONDS = 2.0

# « Ralentis », ce n'est pas « en panne ». Un 429/413/422 veut dire : ta requête
# est arrivée, le fournisseur la refuse pour l'instant (quota, trop d'appels,
# JSON non conforme). Punir ça dix minutes couperait le service pour un pic d'un
# instant ; on observe donc une pause courte, partagée, puis on retente.
PAUSE_SECONDS = 3.0
_PAUSE_UNTIL: float = 0.0

# Codes qui demandent une pause, pas un circuit ouvert. 400 y figure pour Groq :
# un JSON non conforme rend `json_validate_failed` (mesuré avec gpt-oss-20b), et
# ce n'est pas le fournisseur qui est tombé.
PAUSE_CODES = frozenset({400, 413, 422, 429, 503})


def _is_down() -> bool:
    """Vrai tant que le circuit ouvert n'a pas expiré."""
    return time.monotonic() < _LLM_DOWN_UNTIL


def _mark_down() -> None:
    """Ouvre le circuit `DOWN_SECONDS` secondes après un échec du fournisseur."""
    global _LLM_DOWN_UNTIL
    _LLM_DOWN_UNTIL = time.monotonic() + DOWN_SECONDS


def _is_paused() -> bool:
    """Vrai pendant la pause qui suit un refus temporaire du fournisseur."""
    return time.monotonic() < _PAUSE_UNTIL


def _mark_paused() -> None:
    """Impose `PAUSE_SECONDS` de pause partagée après un refus temporaire."""
    global _PAUSE_UNTIL
    _PAUSE_UNTIL = time.monotonic() + PAUSE_SECONDS


def _reset_down() -> None:
    """Referme le circuit (tests : l'état est un global de module)."""
    global _LLM_DOWN_UNTIL, _PAUSE_UNTIL
    _LLM_DOWN_UNTIL = 0.0
    _PAUSE_UNTIL = 0.0

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
    """Un appel OpenAI-compatible. TOUTE erreur ⇒ None, jamais d'exception.

    Circuit ouvert : si le LLM a été marqué down (`_LLM_DOWN_UNTIL`), on sort
    tôt sans appeler le transport. L'échec franc (timeout, connexion, 5xx,
    JSON cassé) ouvre le circuit `DOWN_SECONDS` secondes : on ne refait plus de
    requête pendant ce laps, ce qui évite les 15 s d'attente répétée à chaque
    `/retard` tant que le serveur LLM est mort.

    Un refus temporaire (429, 413, 422, 503) suit un autre chemin : pause
    partagée de `PAUSE_SECONDS`, sans circuit ouvert. Voir `_chat_locked`.

    Les appels sortants sont bornés par `_CALL_SLOTS` : le fournisseur limite
    les requêtes simultanées de notre compte, pas celles de nos threads. Le
    slot est attendu au plus `SLOT_WAIT_SECONDS` ; au-delà on rend la main au
    repli sans bloquer le thread du serveur.
    """
    if not cfg.llm_enabled:
        return None
    # Circuit ouvert : le LLM est connu comme injoignable, on ne tente pas.
    if _is_down():
        return None
    # Pause en cours : le fournisseur vient de demander de ralentir, on n'ajoute
    # pas de charge et on rend la main au repli déterministe.
    if _is_paused():
        return None
    payload: dict[str, Any] = {
        "model": cfg.llm_model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if not _CALL_SLOTS.acquire(timeout=SLOT_WAIT_SECONDS):
        # Tous les slots sont occupés : d'autres appels tiennent le fournisseur.
        # On rend la main au repli au lieu de parquer un thread du serveur.
        log.warning("aucun slot LLM libre après %.1fs : repli local", SLOT_WAIT_SECONDS)
        return None
    try:
        return _chat_locked(cfg, payload, timeout)
    finally:
        _CALL_SLOTS.release()


def _chat_locked(cfg: Config, payload: dict[str, Any], timeout: float) -> str | None:
    """Le transport, une fois un slot d'appel obtenu. Voir `_chat`."""
    try:
        response = httpx.post(
            f"{cfg.llm_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {cfg.llm_api_key}"},
            json=payload,
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        msg = data["choices"][0]["message"]
        content = msg.get("content") or msg.get("reasoning_content")
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in PAUSE_CODES:
            # Le fournisseur répond, il refuse seulement pour l'instant : quota
            # atteint (429), corps trop gros (413), JSON refusé (400/422),
            # surcharge passagère (503). On lui laisse un instant et on repart
            # au lot suivant. Couper dix minutes ici punirait tous les élèves
            # pour le pic d'un seul.
            log.warning("LLM à ralentir (%s) : pause de %.0fs", status, PAUSE_SECONDS)
            _mark_paused()
            return None
        log.warning("appel LLM échoué : %s", type(exc).__name__)
        _mark_down()
        return None
    except Exception as exc:  # timeout, connexion, JSON cassé, forme inattendue...
        log.warning("appel LLM échoué : %s", type(exc).__name__)
        # On mémorise l'échec : les clés en échec repartent dans le lot à chaque
        # affichage tant qu'on ne circuit pas. 10 minutes suffisent au redémarrage
        # d'un backend sans pénaliser l'affichage immédiat.
        _mark_down()
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
            # `_chat` a déjà classé l'échec : circuit ouvert si le fournisseur
            # est réellement tombé, simple pause s'il a demandé de ralentir.
            # Marquer « down » ici transformerait un 429 en panne de dix
            # minutes pour tout le monde, sur le chemin qui 429 le plus.
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
        # LLM_MESSAGES=0 : le corps d'un message de prof ne sort JAMAIS de la
        # machine, pas même pour un résumé. Le repli local suffit à l'afficher.
        if not cfg.llm_messages and getattr(item, "source", "") == "messagerie":
            continue
        todo.append((item.key, item.body))

    if not todo or not cfg.llm_enabled or _is_down():
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
    if not cfg.llm_messages:
        # LLM_MESSAGES=0 : le corps d'un message de prof ne sort jamais de la
        # machine, y compris via `row.summary` (qui vaut body[:140]).
        rows = [r for r in rows if getattr(r.item, "source", "") != "messagerie"]
    if not rows or not cfg.llm_enabled:
        return None
    # LLM_MESSAGES=0 : le corps d'un message de prof ne sort JAMAIS du serveur.
    # `_row_line` lit `row.summary`, qui sans résumé vaut `body[:140]` : un
    # message non filtré enverrait le corps du prof au LLM. On l'interdit donc,
    # comme `ensure_summaries` le fait pour la phase de résumé.
    if not cfg.llm_messages:
        rows = [r for r in rows if getattr(getattr(r, "item", r), "source", "") != "messagerie"]
        if not rows:
            return None
    # Circuit ouvert : si le LLM est marqué down, on ne tente même pas.
    if _is_down():
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
