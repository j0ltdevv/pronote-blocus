"""Synchro PRONOTE : lecture seule des cinq sources, en deux phases.

Règles qui protègent 800 comptes :

* un verrou `flock` par élève, pris AVANT le login et rendu APRÈS
  l'écriture des credentials (deux synchros concurrentes du même élève
  invalideraient mutuellement le jeton) ;
* un sémaphore global, sinon 200 élèves qui écrivent en même temps lancent
  200 logins et l'établissement bloque l'IP ;
* la phase `cours` (lente) ne relit jamais un cours déjà traité, mais
  retente à chaque passage ceux dont le contenu est encore vide.

Ce module n'écrit JAMAIS chez PRONOTE : ni « fait », ni « lu », ni message.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Callable, Iterator, Literal

from .. import db
from ..config import Config
from ..config import today as _today
from . import auth
from .normalize import Item, RawBundle, build_items, clean_text, lesson_slot

log = logging.getLogger(__name__)

PHASE_ITEMS = "items"
PHASE_COURS = "cours"

#: Une lecture de `lesson.content` = une requête réseau.
MAX_CONTENT_READS = 30
#: Pause entre deux lectures de contenu (le serveur PRONOTE n'aime pas les rafales).
CONTENT_DELAY = 1.2
#: Nombre de jours de devoirs regardés devant nous.
FORWARD_DAYS = 45
#: Attente avant l'unique nouvel essai sur un 429/403.
BACKOFF_SECONDS = 3
#: Délai (connexion, lecture) imposé aux requêtes de pronotepy, en secondes.
#: pronotepy n'en pose aucun : sans lui, une socket figée bloque la synchro
#: et le verrou du compte indéfiniment.
HTTP_TIMEOUT = (10, 30)

ERR_TOKEN = "token invalide"
ERR_PIN = "PIN requis"
ERR_UNREACHABLE = "PRONOTE injoignable"
ERR_TIMEOUT = "timeout"
ERR_RATE = "PRONOTE nous ralentit, réessaie dans 5 minutes"
ERR_UNKNOWN = "PRONOTE n'a pas répondu correctement"

#: Modèle factice sous lequel le cache d'extraction des messages est rangé
#: dans la table `summaries` (voir `_extract`).
EXTRACT_MODEL = "extract-v1"

STATUS_TRAITE = "traite"
STATUS_VIDE = "vide"


@dataclass
class SyncResult:
    ok: bool
    error: str | None = None
    items: list[Item] = field(default_factory=list)
    class_name: str | None = None
    phase: str = PHASE_ITEMS
    contents_read: int = 0


class RateLimited(Exception):
    """429/403 confirmé après un nouvel essai."""


# ─────────────────────────── verrous ───────────────────────────


@contextlib.contextmanager
def account_lock(cfg: Config, user_id: str) -> Iterator[str]:
    """Verrou exclusif par compte (`flock`), réutilisable hors de `sync_user`.

    Deux threads qui passent ici pour le même élève se sérialisent ; deux
    élèves différents ne s'attendent jamais.
    """
    cfg.locks_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.locks_dir / f"{user_id}.lock"
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield str(path)
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


_SEMAPHORES: dict[int, threading.Semaphore] = {}
_SEMAPHORES_GUARD = threading.Lock()


def _semaphore(limit: int) -> threading.Semaphore:
    with _SEMAPHORES_GUARD:
        sem = _SEMAPHORES.get(limit)
        if sem is None:
            sem = _SEMAPHORES[limit] = threading.Semaphore(limit)
        return sem


# ─────────────────────────── cache contenu ───────────────────────────

#: (user_id, lesson.start, matière) → LessonContent déjà lu dans ce process.
_CONTENT_CACHE: dict[tuple[str, str, str], Any] = {}
_CONTENT_CACHE_GUARD = threading.Lock()
_CONTENT_CACHE_MAX = 4000


def _cache_key(user_id: str, lesson: Any) -> tuple[str, str, str]:
    start = getattr(lesson, "start", None)
    stamp = start.isoformat() if hasattr(start, "isoformat") else str(start or "")
    subject = clean_text(getattr(getattr(lesson, "subject", None), "name", ""))
    return (user_id, stamp, subject)


def _cache_get(key: tuple[str, str, str]) -> tuple[bool, Any]:
    with _CONTENT_CACHE_GUARD:
        if key in _CONTENT_CACHE:
            return True, _CONTENT_CACHE[key]
    return False, None


def _cache_put(key: tuple[str, str, str], content: Any) -> None:
    with _CONTENT_CACHE_GUARD:
        if len(_CONTENT_CACHE) >= _CONTENT_CACHE_MAX:
            _CONTENT_CACHE.clear()
        _CONTENT_CACHE[key] = content


# ─────────────────────────── erreurs ───────────────────────────


#: pronotepy signale un statut HTTP refusé SANS code structuré :
#: `PronoteAPIError("Bad request (http status: 429)")`.
_HTTP_STATUS_RE = re.compile(r"http status[:\s]*(\d{3})", re.I)


def _http_status(exc: BaseException) -> int | None:
    """Code HTTP porté par l'exception, quelle que soit sa forme."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    code = getattr(exc, "pronote_error_code", None)
    if isinstance(code, int):
        return code
    match = _HTTP_STATUS_RE.search(str(exc))
    if match:
        return int(match.group(1))
    return None


def _error_of(exc: BaseException) -> str:
    """Exception → texte affichable à l'élève (jamais de stack trace)."""
    import requests

    import pronotepy

    if isinstance(exc, auth.TokenRefused):
        return ERR_TOKEN
    if isinstance(exc, RateLimited):
        return ERR_RATE
    if isinstance(exc, pronotepy.MFAError):
        return ERR_PIN
    if isinstance(exc, getattr(pronotepy, "QRCodeDecryptError", ())):
        return ERR_TOKEN
    if isinstance(exc, pronotepy.CryptoError):
        return ERR_TOKEN
    if isinstance(exc, requests.Timeout):
        return ERR_TIMEOUT
    if isinstance(exc, pronotepy.PronoteAPIError):
        code = getattr(exc, "pronote_error_code", None)
        if code in (401, 403):
            return ERR_TOKEN if code == 401 else ERR_RATE
        # Mesuré en production : un jeton révoqué ressort en erreur PRONOTE
        # 23 (« Unknown error from pronote: 23 »), sans code structuré.
        if code == 23 or "pronote: 23" in str(exc):
            return ERR_TOKEN
        # Un statut HTTP refusé arrive en texte, sans `pronote_error_code`.
        status = _http_status(exc)
        if status == 401:
            return ERR_TOKEN
        if status in (403, 429):
            return ERR_RATE
        return ERR_UNKNOWN
    if isinstance(exc, requests.RequestException):
        status = _http_status(exc)
        if status == 429:
            return ERR_RATE
        if status in (401, 403):
            return ERR_TOKEN if status == 401 else ERR_RATE
        return ERR_UNREACHABLE
    return ERR_UNKNOWN


def _call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Appel réseau : sur 429/403, une pause puis UN seul nouvel essai.

    Ne réessaie QUE les `requests.HTTPError`. Les erreurs de pronotepy
    (`PronoteAPIError("Bad request (http status: 429)")`) ne le sont pas :
    `ClientBase.post` a déjà fait un `refresh()` (login complet) et rejoué
    la requête avant de la propager ; insister relancerait un login de plus
    pendant que le serveur throttle. `_error_of` les classe en `ERR_RATE`
    (« réessaie dans 5 minutes ») et la synchro suivante reprend.
    """
    import requests

    try:
        return fn(*args, **kwargs)
    except requests.HTTPError as exc:
        status = _http_status(exc)
        if status not in (429, 403):
            raise
        time.sleep(BACKOFF_SECONDS)
        try:
            return fn(*args, **kwargs)
        except requests.HTTPError as again:
            if _http_status(again) in (429, 403):
                raise RateLimited() from again
            raise


# ─────────────────────────── helpers PRONOTE ───────────────────────────


def _arm_timeout(client: Any, timeout: Any = HTTP_TIMEOUT) -> None:
    """Impose un délai à chaque requête du client pronotepy.

    pronotepy ne passe aucun `timeout` à `requests` : une socket figée
    bloquerait la synchro (et le verrou du compte) sans fin. On patche le
    `Session` à l'instance — `Session.get`/`post` passent par `self.request`.
    Sans client réel (tests, objets factices), ne fait rien.
    """
    session = getattr(getattr(client, "communication", None), "session", None)
    if session is None or getattr(session, "_timeout_arme", False):
        return
    request = session.request

    def request_with_timeout(method: str, url: str, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", timeout)
        return request(method, url, **kwargs)

    session.request = request_with_timeout
    session._timeout_arme = True


def _client_call(
    client: Any, fn: Callable[..., Any], *args: Any, **kwargs: Any
) -> Any:
    """`_call` sur un client pronotepy, timeout réarmé à chaque requête.

    Un `refresh()` de pronotepy remplace `communication` par un `Session`
    neuf : le délai posé sur l'ancien disparaît avec lui.
    """
    _arm_timeout(client)
    return _call(fn, *args, **kwargs)


def _window_start(cfg: Config) -> date:
    """Début de fenêtre courant : `WINDOW_START` relu à chaque synchro.

    La valeur validée au démarrage (`cfg.window_start`) n'est qu'un repli :
    si la variable change pendant que le process tourne, le cycle suivant
    l'applique sans redémarrer. Une valeur invalide à chaud est ignorée
    (log + repli) plutôt que de faire échouer la synchro.
    """
    raw = (os.environ.get("WINDOW_START") or "").strip()
    if not raw:
        return cfg.window_start_date()
    try:
        return date.fromisoformat(raw)
    except ValueError:
        log.warning(
            "WINDOW_START invalide en cours de route %r : repli sur %s",
            raw,
            cfg.window_start,
        )
        return cfg.window_start_date()


def _window(cfg: Config) -> tuple[date, date, date]:
    today = _today()
    return _window_start(cfg), today, today + timedelta(days=FORWARD_DAYS)


def class_name_of(client: Any) -> str | None:
    """« 2nde 3 » lu sur le client, ou None si PRONOTE ne le donne pas."""
    try:
        info = getattr(client, "info", None)
        name = clean_text(getattr(info, "class_name", "") if info is not None else "")
    except Exception:
        return None
    return name or None


def subjects_of(client: Any, cfg: Config) -> list[str]:
    """Matières réelles de l'élève sur la fenêtre de rattrapage, triées.

    Sert la liste numérotée montrée pendant l'inscription.
    """
    date_from, today, _ = _window(cfg)
    try:
        lessons = _client_call(client, client.lessons, date_from, today)
    except Exception:
        lessons = []
    names: set[str] = set()
    for lesson in lessons or []:
        name = clean_text(getattr(getattr(lesson, "subject", None), "name", ""))
        if name:
            names.add(name)
    if not names:
        # Filet : certains comptes n'ont aucun cours sur la fenêtre.
        try:
            for lesson in _client_call(client, client.lessons, today, today) or []:
                name = clean_text(getattr(getattr(lesson, "subject", None), "name", ""))
                if name:
                    names.add(name)
        except Exception:
            pass
    return sorted(names)


def _content_is_empty(content: Any) -> bool:
    if content is None:
        return True
    title = clean_text(getattr(content, "title", ""))
    description = clean_text(getattr(content, "description", ""))
    return not title and not description


def _read_content(client: Any, lesson: Any) -> Any:
    return _client_call(client, lambda: lesson.content)


def _discussion_body(discussion: Any) -> str:
    parts: list[str] = []
    for message in list(getattr(discussion, "messages", []) or []):
        text = clean_text(getattr(message, "content", ""))
        if text:
            parts.append(text)
    return "\n".join(parts)


def _extract(conn: Any, subject: str, body: str, cfg: Config) -> dict | None:
    """Extraction LLM d'un devoir caché dans un message, AVEC cache.

    Règle §7 appliquée ici aussi : le coût du modèle ne doit pas dépendre du
    nombre d'élèves. Deux cent élèves de la même classe reçoivent le même
    message de prof : un seul appel, la réponse est rangée dans `summaries`
    sous le modèle `extract-v1` et servie aux suivants. La clé est un hash du
    couple (sujet, corps) : un message modifié repart au modèle.
    """
    if not (cfg.llm_messages and cfg.llm_enabled and body.strip()):
        return None

    cache_key = hashlib.sha256(
        json.dumps([subject, body], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    cached = db.get_summaries(conn, [cache_key], EXTRACT_MODEL).get(cache_key)
    if cached is not None:
        try:
            stored = json.loads(cached["summary"] or "null")
        except ValueError:
            return None
        return stored if isinstance(stored, dict) else None

    try:
        from .. import llm  # import tardif : évite un cycle

        found, sure = llm.extract_devoir_checked(subject, body, cfg)
    except Exception:
        return None
    if not sure:
        # Panne du modèle : rien en cache, on retentera à la synchro suivante.
        return None
    db.put_summaries(
        conn,
        EXTRACT_MODEL,
        {
            cache_key: {
                "summary": json.dumps(found, ensure_ascii=False) if found else "null",
                "action": "",
                "effort": "",
            }
        },
    )
    return found


# ─────────────────────────── collecte ───────────────────────────


def _fetch_items(client: Any, cfg: Config, conn: Any) -> RawBundle:
    """Phase `items` : devoirs, informations, messagerie. Aucun `.content`."""
    date_from, today, date_to = _window(cfg)
    homework = _client_call(client, client.homework, date_from, date_to) or []
    infos = (
        _client_call(
            client,
            client.information_and_surveys,
            datetime.combine(date_from, dtime.min),
            datetime.combine(today, dtime.max),
            only_unread=False,
        )
        or []
    )

    discussions: list[tuple[str, Callable[[], str]]] = []
    extracted: dict[str, dict | None] = {}
    seen: set[str] = set()
    for discussion in _client_call(client, client.discussions) or []:
        # On ne lit QUE les discussions ouvertes, et on ne les marque jamais lues.
        if getattr(discussion, "closed", False):
            continue
        subject = clean_text(getattr(discussion, "subject", "")) or "(sans objet)"
        if subject in seen:
            continue
        seen.add(subject)
        # Une discussion illisible fait échouer la phase : la traiter comme
        # vide la ferait disparaître, et `replace_items` purgerait les items
        # `messagerie` déjà connus de l'élève.
        body = _discussion_body(discussion)  # déclenche les messages
        if not body:
            continue
        discussion_extraction = _extract(conn, subject, body, cfg)
        extracted[subject] = discussion_extraction
        discussions.append((subject, (lambda text=body: text)))

    lessons = _client_call(client, client.lessons, date_from, today) or []

    return RawBundle(
        homework=list(homework),
        lessons=list(lessons),
        contents={},
        infos=list(infos),
        discussions=discussions,
        extracted=extracted,
        today=today,
    )


def _merge_items(existing: list[Item], fresh: list[Item]) -> list[Item]:
    merged: dict[str, Item] = {item.key: item for item in existing}
    for item in fresh:
        merged[item.key] = item
    return list(merged.values())


def _collect_cours(
    client: Any, conn: Any, user_id: str, cfg: Config, bundle: RawBundle
) -> tuple[list[Item], int, dict[str, str]]:
    """Phase `cours` : lit les contenus non encore traités, dans la limite.

    Ne reconstruit que les items `cours`/`controle`, puis les unit aux items
    déjà en base : la phase `items` n'est jamais écrasée.

    Les statuts `lesson_seen` ne sont PAS écrits ici : `collect` ne les pose
    qu'après un `replace_items` réussi. Sinon une panne en fin de passe
    (429, disque, build) marquerait « traité » un cours dont l'item n'a
    jamais été persisté, et la passe suivante le sauterait pour toujours.
    """
    statuses = db.lesson_status(conn, user_id)
    contents: dict[str, Any] = {}
    pending: dict[str, str] = {}
    first_failure: Exception | None = None
    obtained = 0
    reads = 0
    skipped = 0

    for lesson in bundle.lessons:
        if getattr(lesson, "canceled", False):
            continue
        slot = lesson_slot(lesson)
        status = statuses.get(slot)
        if status == STATUS_TRAITE:
            continue

        cached, content = _cache_get(_cache_key(user_id, lesson))
        if not cached:
            if reads >= MAX_CONTENT_READS:
                skipped += 1
                continue
            if reads:
                time.sleep(CONTENT_DELAY)
            reads += 1
            try:
                content = _read_content(client, lesson)
            except RateLimited:
                raise
            except Exception as exc:
                # Une lecture ratée ne jette pas les autres : le cours garde
                # son statut, la synchro suivante le retentera. Les items
                # déjà en base pour ce cours restent (voir `_merge_items`).
                if first_failure is None:
                    first_failure = exc
                skipped += 1
                continue
            # Contenu vide : ne PAS mettre en cache. Le docstring de la phase
            # cours promet de « retenter à chaque passage ceux dont le contenu
            # est encore vide ». Un cours vide mis en cache serait jamais
            # relu avant le prochain raz de 4000 entrées, ce qui annulerait
            # cette garantie. On garde par contre la protection anti-boucle :
            # STATUS_VIDE évite de relire le même cours indéfiniment dans
            # une même passe.
            if not _content_is_empty(content):
                _cache_put(_cache_key(user_id, lesson), content)

        obtained += 1
        contents[slot] = content
        pending[slot] = STATUS_VIDE if _content_is_empty(content) else STATUS_TRAITE

    if first_failure is not None and obtained == 0:
        # Aucune lecture n'a abouti (session expirée, serveur mort) : ce
        # n'est pas une panne partielle. Échouer fait rejouer la synchro
        # sans attendre le délai de fraîcheur, et rien n'est purgé.
        raise first_failure

    bundle.contents = contents
    _arm_timeout(client)  # `build_items` lit `info.content()`
    fresh = build_items(client, bundle)
    existing = db.get_items(conn, user_id)
    return _merge_items(existing, fresh), reads, pending


def collect(
    client: Any,
    conn: Any,
    user_id: str,
    cfg: Config,
    phase: Literal["items", "cours"] = PHASE_ITEMS,
) -> SyncResult:
    """Synchro sans login : réutilisable juste après une connexion par QR."""
    cfg.ensure_dirs()
    _arm_timeout(client)
    phase = PHASE_COURS if phase == PHASE_COURS else PHASE_ITEMS
    try:
        db.set_sync_state(conn, user_id, phase)
        class_name = class_name_of(client)
        lesson_statuses: dict[str, str] = {}
        if phase == PHASE_ITEMS:
            bundle = _fetch_items(client, cfg, conn)
            _arm_timeout(client)  # `build_items` lit `info.content()`
            items = build_items(client, bundle)
            reads = 0
        else:
            date_from, today, _ = _window(cfg)
            bundle = RawBundle(
                lessons=list(
                    _client_call(client, client.lessons, date_from, today) or []
                ),
                today=today,
            )
            items, reads, lesson_statuses = _collect_cours(
                client, conn, user_id, cfg, bundle
            )
    except Exception as exc:
        error = _error_of(exc)
        db.set_sync_state(conn, user_id, "idle")
        db.set_last_error(conn, user_id, error)
        return SyncResult(False, error, [], class_name_of(client), phase, 0)

    try:
        db.replace_items(conn, user_id, items)
        # Statuts posés SEULEMENT une fois les items persistés : un cours
        # marqué « traité » dont l'item manque ne serait plus jamais relu.
        for slot, status in lesson_statuses.items():
            db.set_lesson_status(conn, user_id, slot, status)
        if class_name:
            db.set_class_name(conn, user_id, class_name)
        db.mark_synced(conn, user_id)
    finally:
        db.set_sync_state(conn, user_id, "idle")

    return SyncResult(True, None, items, class_name, phase, reads)


def sync_user(
    conn: Any,
    user_id: str,
    cfg: Config,
    phase: Literal["items", "cours"] = PHASE_ITEMS,
) -> SyncResult:
    """Verrou + login + rotation du jeton + collecte."""
    cfg.ensure_dirs()
    phase = PHASE_COURS if phase == PHASE_COURS else PHASE_ITEMS
    with _semaphore(cfg.max_concurrent_syncs), account_lock(cfg, user_id):
        try:
            creds = db.credentials_for(conn, user_id, cfg.app_secret)
        except KeyError:
            db.set_sync_state(conn, user_id, "idle")
            return SyncResult(False, ERR_TOKEN, [], None, phase, 0)

        try:
            client = auth.token_login(
                creds,
                account_pin=creds.get("account_pin"),
                mobile_query=cfg.mobile_query,
            )
            # Le login réussit a invalidé le jeton précédent : on écrit le
            # nouveau TOUT DE SUITE, dans la même section critique.
            db.save_credentials(
                conn, user_id, client.export_credentials(), cfg.app_secret
            )
        except Exception as exc:
            error = _error_of(exc)
            db.set_sync_state(conn, user_id, "idle")
            db.set_last_error(conn, user_id, error)
            return SyncResult(False, error, [], None, phase, 0)

        result = collect(client, conn, user_id, cfg, phase)

        # La session a pu tourner pendant la collecte : on repose le jeton
        # avant de relâcher le verrou.
        with contextlib.suppress(Exception):
            db.save_credentials(
                conn, user_id, client.export_credentials(), cfg.app_secret
            )
        return result
