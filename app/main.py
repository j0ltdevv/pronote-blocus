"""Bot Telegram PRONOTE Blocus — long polling, aucune surface HTTP.

Un seul processus, SQLite, `python-telegram-bot` en polling : pas de webhook,
pas d'URL publique, pas de HTTPS. La seule dépendance externe est l'API
Telegram (et PRONOTE pour les synchros, et le LLM si configuré).

Parcours d'inscription : `/start` → capture du QR → code à 4 chiffres →
choix des matières → première liste de devoirs.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone

import pronotepy
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from . import config, crypto, db, llm, render
from .pronote import auth, fetch, ranking

log = logging.getLogger(__name__)

# États de la conversation de connexion.
QR, PIN, ACCOUNT_PIN, MATIERES = range(4)

PROMPT_QR = (
    "Envoie une capture d'écran du QR code affiché dans PRONOTE "
    "(Compte → connexion par QR code). Le QR expire vite : génère-le juste "
    "avant de l'envoyer."
)
PROMPT_PIN = (
    "QR lu. Envoie maintenant le code à 4 chiffres que tu as choisi en "
    "générant le QR."
)
PROMPT_MATIERES = (
    "Réponds avec les numéros dans l'ordre de priorité, ex. `3 1 5`."
)

MSG_CRYPTO = (
    "Le QR ou le code à 4 chiffres n'est pas bon. Régénère un QR dans PRONOTE "
    "et renvoie-le."
)
MSG_EXPIRE = (
    "Ce QR a expiré. Régénère-le dans PRONOTE et renvoie-le tout de suite."
)
MSG_MFA = "Ce compte demande un code PIN : envoie-le."
MSG_PAS_PRONOTE = (
    "Ce n'est pas un QR PRONOTE. Prends une capture d'écran du QR affiché par "
    "l'application."
)
MSG_FLOU = "Image trop floue. Refais la capture en incluant toute la bordure du QR."
MSG_CODE = "Ça doit être exactement 4 chiffres."
MSG_INDEX = "ce numéro n'est plus valide, renvoie /retard"
MSG_QUOTA = "tu as posé 10 questions aujourd'hui, reviens demain"
MSG_CONNEXION = "Envoie /start pour connecter ton compte PRONOTE."

_QUATRE_CHIFFRES = re.compile(r"\d{4}")
_ENTIERS = re.compile(r"\d+")


# ─────────────────────────── petits utilitaires ───────────────────────────


def _conn(context: ContextTypes.DEFAULT_TYPE):
    return context.application.bot_data["conn"]


def _cfg(context: ContextTypes.DEFAULT_TYPE) -> config.Config:
    return context.application.bot_data["cfg"]


def _cache(context: ContextTypes.DEFAULT_TYPE, name: str) -> dict:
    return context.application.bot_data.setdefault(name, {})


def _user_of(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is None:
        return None
    return db.get_user_by_chat(_conn(context), chat_id)


def _is_admin(chat_id: int, cfg: config.Config) -> bool:
    return cfg.admin_chat_id is not None and chat_id == cfg.admin_chat_id


async def _reply(bot, chat_id: int, text: str) -> None:
    """Texte simple, découpé par `render.chunk` comme tout le reste."""
    for part in render.chunk(text):
        await bot.send_message(chat_id, part, disable_web_page_preview=True)


async def _send_html(  # noqa: ANN001
    bot, chat_id: int, header: str, lines: list[str], footer: str | None = None
) -> None:
    parts = render.compose(header, lines, footer)
    for part in parts:
        await bot.send_message(
            chat_id, part, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )


async def _reply_html(  # noqa: ANN001
    update: Update, header: str, lines: list[str], footer: str | None = None
) -> None:
    for part in render.compose(header, lines, footer):
        await update.message.reply_text(
            part, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )


async def _note_activity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat:
        _cache(context, "last_seen")[update.effective_chat.id] = datetime.now(
            timezone.utc
        )


def _seen_recently(application: Application, chat_id: int, hours: int = 24) -> bool:
    stamp = application.bot_data.get("last_seen", {}).get(chat_id)
    if stamp is None:
        return False
    return datetime.now(timezone.utc) - stamp < timedelta(hours=hours)


def _stale(last_sync_at: str | None, hours: float) -> bool:
    if not last_sync_at:
        return True
    try:
        stamp = datetime.fromisoformat(last_sync_at)
    except ValueError:
        return True
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - stamp > timedelta(hours=hours)


async def _load_rows(conn, user_id: str, cfg: config.Config):
    """(items, retard, à venir) avec résumés en cache — tout en thread."""
    items = await asyncio.to_thread(db.get_items, conn, user_id)
    summaries = await asyncio.to_thread(llm.ensure_summaries, conn, items, cfg)
    priorities = await asyncio.to_thread(db.get_priorities, conn, user_id)
    done = await asyncio.to_thread(db.done_keys, conn, user_id)
    overdue, upcoming = ranking.split_sections(
        items, summaries, priorities, config.today(), done
    )
    return items, overdue, upcoming


async def _send_rows(bot, conn, user_id: str, title: str, rows, page: int = 1) -> None:  # noqa: ANN001
    """Liste numérotée paginée + réécriture de `list_ref` (ordre global)."""
    if not rows:
        await _reply(bot, _chat_of(conn, user_id), "Rien à afficher pour l'instant.")
        db.write_list_ref(conn, user_id, [])
        return
    db.write_list_ref(conn, user_id, [row.key for row in rows])
    page_rows, page, note = render.paginate(rows, page, 15)
    start = (page - 1) * 15 + 1
    lines = render.numbered_lines(page_rows, config.today(), start=start)
    await _send_html(
        bot,
        _chat_of(conn, user_id),
        f"{title} — {len(rows)} item(s)",
        lines,
        note or None,
    )


def _chat_of(conn, user_id: str) -> int:
    row = db.get_user(conn, user_id)
    if row is None:
        raise KeyError(user_id)
    return int(row["chat_id"])


async def _send_overdue(bot, conn, cfg: config.Config, user_id: str, page: int = 1) -> None:
    _, overdue, _ = await _load_rows(conn, user_id, cfg)
    if not overdue:
        await _reply(
            bot,
            _chat_of(conn, user_id),
            "Rien en retard 🎉 Envoie /refresh pour récupérer les derniers devoirs.",
        )
        db.write_list_ref(conn, user_id, [])
        return
    await _send_rows(
        bot,
        conn,
        user_id,
        "<b>En retard</b> · trié par urgence, puis par tes matières prioritaires",
        overdue,
        page,
    )


# ─────────────────────────── conversation de connexion ───────────────────────────


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    user = _user_of(update, context)
    if user is not None:
        if user["last_error"] == fetch.ERR_TOKEN:
            # Jeton révoqué : sans QR neuf, ce compte ne pourra plus jamais
            # lire PRONOTE. `/start` est le seul geste que l'élève connaît,
            # c'est donc ici qu'on rouvre le parcours de connexion.
            await update.message.reply_text(
                "Ta connexion PRONOTE a expiré, on la refait en 30 secondes.\n\n"
                + PROMPT_QR
            )
            return QR
        await _send_overdue(context.bot, _conn(context), _cfg(context), user["id"])
        return ConversationHandler.END
    await update.message.reply_text(PROMPT_QR)
    return QR


async def _save_qr(
    update: Update, context: ContextTypes.DEFAULT_TYPE, qr: dict
) -> int:
    chat_id = update.effective_chat.id
    _cache(context, "qr_cache")[chat_id] = qr
    # Un QR neuf = un code à 4 chiffres neuf : on repart de zéro.
    _cache(context, "pending_uuid").pop(chat_id, None)
    _cache(context, "mfa_tries").pop(chat_id, None)
    await update.message.reply_text(PROMPT_PIN)
    return PIN


async def qr_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.message
    file = None
    if message.photo:
        file = await message.photo[-1].get_file()
    elif message.document is not None:
        mime = (message.document.mime_type or "").lower()
        if mime in ("image/jpeg", "image/png", "image/jpg"):
            file = await message.document.get_file()
    if file is None:
        await message.reply_text(
            "Envoie une photo ou un fichier image (jpg/png) du QR code."
        )
        return QR
    try:
        data = bytes(await file.download_as_bytearray())
    except Exception:
        log.exception("téléchargement de la photo impossible")
        await message.reply_text(MSG_FLOU)
        return QR
    try:
        qr = await asyncio.to_thread(auth.decode_qr_image, data)
    except auth.QrError as exc:
        await message.reply_text(exc.message)
        return QR
    return await _save_qr(update, context, qr)


async def qr_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        qr = auth.parse_qr_text((update.message.text or "").strip())
    except auth.QrError as exc:
        await update.message.reply_text(exc.message)
        return QR
    return await _save_qr(update, context, qr)


def _new_uuid(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> str:
    uuids = _cache(context, "pending_uuid")
    if chat_id not in uuids:
        uuids[chat_id] = crypto.new_app_uuid()
    return uuids[chat_id]


async def pin_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    chaine = (update.message.text or "").strip()
    if not _QUATRE_CHIFFRES.fullmatch(chaine):
        await update.message.reply_text(MSG_CODE)
        return PIN
    qr = _cache(context, "qr_cache").get(chat_id)
    if not qr:
        await update.message.reply_text(
            "Je n'ai plus le QR en mémoire. Renvoie-le, puis le code."
        )
        return QR
    app_uuid = _new_uuid(context, chat_id)
    _cache(context, "pin_code")[chat_id] = chaine
    await update.message.reply_text("Je me connecte à PRONOTE…")
    try:
        client, creds = await asyncio.to_thread(
            auth.login_with_qr, qr, chaine, app_uuid
        )
    except pronotepy.MFAError:
        tries = _cache(context, "mfa_tries")
        tries[chat_id] = tries.get(chat_id, 0) + 1
        if tries[chat_id] > 1:
            await update.message.reply_text(
                "Le code PIN n'a pas marché non plus. Régénère un QR dans "
                "PRONOTE et renvoie-le avec /start."
            )
            _reset_flow(context, chat_id)
            return ConversationHandler.END
        await update.message.reply_text(MSG_MFA)
        return ACCOUNT_PIN
    except pronotepy.CryptoError:
        await update.message.reply_text(MSG_CRYPTO)
        return QR
    except auth.QrExpired as exc:
        await update.message.reply_text(exc.message)
        return QR
    except Exception:
        log.exception("connexion par QR refusée")
        await update.message.reply_text(MSG_EXPIRE)
        return QR
    return await _finish_login(
        update, context, client, creds, qr["url"], None, qr.get("login", "")
    )


async def account_pin_received(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    chat_id = update.effective_chat.id
    qr = _cache(context, "qr_cache").get(chat_id)
    app_uuid = _cache(context, "pending_uuid").get(chat_id)
    if not qr or not app_uuid:
        await update.message.reply_text("Le QR a disparu. Renvoie-le avec /start.")
        return QR
    code = (update.message.text or "").strip()
    await update.message.reply_text("Je me connecte à PRONOTE…")
    try:
        client, creds = await asyncio.to_thread(
            auth.login_with_qr, qr, _cache(context, "pin_code").get(chat_id, ""), app_uuid, code
        )
    except Exception:
        log.exception("connexion avec code PIN refusée")
        await update.message.reply_text(
            "Le code PIN n'a pas marché. Régénère un QR dans PRONOTE et "
            "renvoie-le avec /start."
        )
        _reset_flow(context, chat_id)
        return ConversationHandler.END
    return await _finish_login(update, context, client, creds, qr["url"], code, qr.get("login", ""))


def _reset_flow(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    for name in ("qr_cache", "pending_uuid", "mfa_tries", "subjects", "pin_code"):
        _cache(context, name).pop(chat_id, None)


async def _finish_login(  # noqa: ANN001
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    client,
    creds: dict,
    pronote_url: str,
    account_pin: str | None,
    username: str = "",
) -> int:
    """Crée le compte, ou le reconnecte s'il existe déjà, puis annonce la classe."""
    conn, cfg = _conn(context), _cfg(context)
    chat_id = update.effective_chat.id
    class_name = await asyncio.to_thread(fetch.class_name_of, client)
    existing = db.get_user_by_chat(conn, chat_id)
    if existing is not None:
        # Ré-appairage : `chat_id` est unique, on met à jour la ligne au lieu
        # d'en créer une seconde (et on garde priorités et « fait »).
        user_id = existing["id"]
        await asyncio.to_thread(
            db.reauth_user,
            conn,
            user_id,
            creds=creds,
            secret=cfg.app_secret,
            pronote_url=pronote_url,
            username=username or creds.get("username", ""),
            class_name=class_name,
            account_pin=account_pin,
        )
    else:
        user_id = await asyncio.to_thread(
            db.create_user,
            conn,
            chat_id=chat_id,
            pronote_url=pronote_url,
            username=username or creds.get("username", ""),
            creds=creds,
            secret=cfg.app_secret,
            class_name=class_name,
            account_pin=account_pin,
        )
    if class_name:
        await asyncio.to_thread(db.set_class_name, conn, user_id, class_name)
    # Première collecte IMMÉDIATE, sur le client déjà connecté : sans elle la
    # première liste serait vide alors que PRONOTE a des devoirs. `collect`
    # (pas `sync_user`) parce que le login vient d'être fait : un second
    # login tournerait le jeton pour rien.
    first = await asyncio.to_thread(fetch.collect, client, conn, user_id, cfg, "items")
    if not first.ok:
        log.warning("première collecte incomplète : %s", first.error)
    subjects = await asyncio.to_thread(fetch.subjects_of, client, cfg)
    _reset_flow(context, chat_id)
    if not subjects:
        await update.message.reply_text(
            f"Connecté, {class_name or 'classe inconnue'}. Je n'ai trouvé "
            "aucune matière : envoie /refresh dans quelques minutes."
        )
        return ConversationHandler.END
    _cache(context, "subjects")[chat_id] = subjects
    lines = [f"{i}. {render.esc(s)}" for i, s in enumerate(subjects, start=1)]
    await _reply_html(
        update,
        f"Connecté, <b>{render.esc(class_name or 'classe inconnue')}</b>. "
        "Voici tes matières :",
        lines,
        PROMPT_MATIERES,
    )
    return MATIERES


async def matieres_received(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    chat_id = update.effective_chat.id
    subjects = _cache(context, "subjects").get(chat_id) or []
    nums = [int(n) for n in _ENTIERS.findall(update.message.text or "")]
    if not nums or any(n < 1 or n > len(subjects) for n in nums):
        await update.message.reply_text(
            "Réponds avec les numéros de la liste ci-dessus, ex. `3 1 5`."
        )
        return MATIERES
    chosen: list[str] = []
    for n in nums:
        if subjects[n - 1] not in chosen:
            chosen.append(subjects[n - 1])
    conn, cfg = _conn(context), _cfg(context)
    user = db.get_user_by_chat(conn, chat_id)
    if user is None:
        await update.message.reply_text(MSG_CONNEXION)
        return ConversationHandler.END
    db.set_priorities(conn, user["id"], chosen)
    _reset_flow(context, chat_id)
    await update.message.reply_text("C'est noté 👍 Voilà ce que tu as à rattraper.")
    try:
        await _send_overdue(context.bot, conn, cfg, user["id"])
    except Exception:
        log.exception("première liste impossible")
        await update.message.reply_text(
            "J'ai enregistré tes priorités, mais je n'ai pas encore de devoirs. "
            "Envoie /refresh dans une minute."
        )
    # Phase `cours` en tâche de fond : jamais sur le chemin de la réponse.
    context.application.create_task(
        _cours_after_sync(context.application, chat_id, user["id"])
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _reset_flow(context, update.effective_chat.id)
    await update.message.reply_text(
        "Annulé. Envoie /start quand tu veux reprendre la connexion."
    )
    return ConversationHandler.END


async def _cours_after_sync(application: Application, chat_id: int, user_id: str) -> None:
    """Phase `cours` hors du chemin d'une réponse de commande."""
    conn = application.bot_data["conn"]
    cfg = application.bot_data["cfg"]
    try:
        result = await asyncio.to_thread(fetch.sync_user, conn, user_id, cfg, "cours")
    except Exception:
        log.exception("phase cours en échec")
        return
    if result.ok and _seen_recently(application, chat_id):
        try:
            await application.bot.send_message(
                chat_id, "Les contenus de cours sont prêts : /cours"
            )
        except Exception:
            log.exception("notification cours impossible")


# ─────────────────────────── commandes ───────────────────────────


async def _require_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = _user_of(update, context)
    if user is None:
        await update.message.reply_text(MSG_CONNEXION)
    return user


async def cmd_retard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _require_user(update, context)
    if user is None:
        return
    page = 1
    if context.args:
        try:
            page = max(1, int(context.args[0]))
        except ValueError:
            page = 1
    await _send_overdue(context.bot, _conn(context), _cfg(context), user["id"], page)


async def cmd_aujourdhui(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _require_user(update, context)
    if user is None:
        return
    conn, cfg = _conn(context), _cfg(context)
    _, overdue, upcoming = await _load_rows(conn, user["id"], cfg)
    today = config.today()
    demain = today + timedelta(days=1)
    rows = list(overdue) + list(upcoming)
    du_jour = [r for r in rows if r.item.due_date == today]
    de_demain = [r for r in rows if r.item.due_date == demain]
    if not du_jour and not de_demain:
        await update.message.reply_text(
            "Rien à rendre aujourd'hui ni demain. /retard pour le reste."
        )
        return
    mixed = du_jour + de_demain
    await _send_rows(
        context.bot,
        conn,
        user["id"],
        "<b>Aujourd'hui et demain</b>",
        mixed,
    )


async def cmd_matiere(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _require_user(update, context)
    if user is None:
        return
    query = " ".join(context.args or []).strip()
    if not query:
        await update.message.reply_text("Envoie /matiere suivi du nom, ex. /matiere maths")
        return
    conn, cfg = _conn(context), _cfg(context)
    _, overdue, upcoming = await _load_rows(conn, user["id"], cfg)
    needle = query.casefold()
    rows = [r for r in list(overdue) + list(upcoming) if needle in r.item.subject.casefold()]
    if not rows:
        await update.message.reply_text(
            f"Rien trouvé en « {query} ». Essaie /retard pour tout voir."
        )
        return
    await _send_rows(
        context.bot, conn, user["id"], f"<b>{render.esc(query)}</b>", rows
    )


async def cmd_cours(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _require_user(update, context)
    if user is None:
        return
    conn, cfg = _conn(context), _cfg(context)
    query = " ".join(context.args or []).strip().casefold()
    items, _, _ = await _load_rows(conn, user["id"], cfg)
    priorities = await asyncio.to_thread(db.get_priorities, conn, user["id"])
    groups = ranking.group_cours(items, priorities)
    if query:
        groups = [g for g in groups if query in g[0].casefold()]
    if not groups:
        await update.message.reply_text(
            "Aucun contenu de cours pour l'instant. La récupération prend "
            "quelques minutes : réessaie plus tard."
        )
        return
    lines, hidden = render.format_cours(groups)
    footer = f"… et {hidden} autres cours" if hidden else None
    await _reply_html(update, render.HEADER_COURS, lines, footer)


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _require_user(update, context)
    if user is None:
        return
    conn, cfg = _conn(context), _cfg(context)
    _, overdue, upcoming = await _load_rows(conn, user["id"], cfg)
    rows = list(overdue) + list(upcoming)
    if not rows:
        await update.message.reply_text("Rien à résumer : tu es à jour 🎉")
        return
    synthesis = await asyncio.to_thread(llm.weekly_synthesis, rows, cfg)
    if synthesis:
        await _reply(context.bot, update.effective_chat.id, synthesis)
        return
    lines = render.numbered_lines(rows[:10], config.today())
    # La liste affichée doit être celle que /fait N résoudra : sans cette
    # réécriture, les numéros de /resume pointeraient sur une liste antérieure.
    db.write_list_ref(conn, user["id"], [row.key for row in rows[:10]])
    await _reply_html(
        update,
        "<b>Ta semaine</b> · synthèse indisponible, voilà la liste brute",
        lines,
        "… et d'autres items, /retard pour tout voir",
    )


async def cmd_fait(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _require_user(update, context)
    if user is None:
        return
    if not context.args or not _ENTIERS.fullmatch(context.args[0].strip()):
        await update.message.reply_text("Envoie /fait suivi d'un numéro, ex. /fait 7")
        return
    idx = int(context.args[0])
    conn = _conn(context)
    key = db.resolve_list_ref(conn, user["id"], idx)
    if key is None:
        await update.message.reply_text(MSG_INDEX)
        return
    db.mark_done(conn, user["id"], key)
    await update.message.reply_text("✅")


async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _require_user(update, context)
    if user is None:
        return
    conn, cfg = _conn(context), _cfg(context)
    if _stale(user["last_sync_at"], hours=1 / 6):
        await update.message.reply_text("Je vais chercher tes devoirs…")
        result = await asyncio.to_thread(fetch.sync_user, conn, user["id"], cfg, "items")
        if not result.ok:
            if result.error == "token invalide":
                await update.message.reply_text(
                    "Ta connexion PRONOTE a expiré. Renvoie un QR avec /start."
                )
                return
            await update.message.reply_text(
                f"PRONOTE ne répond pas pour l'instant ({result.error}). "
                "Réessaie dans 5 minutes."
            )
            return
    await _send_overdue(context.bot, conn, cfg, user["id"])


async def cmd_priorites(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _require_user(update, context)
    if user is None:
        return
    conn = _conn(context)
    subjects = sorted(
        {row["subject"] for row in conn.execute(
            "SELECT DISTINCT subject FROM items WHERE user_id = ?", (user["id"],)
        )}
    )
    if not subjects:
        await update.message.reply_text(
            "Je ne connais pas encore tes matières. Envoie /refresh d'abord."
        )
        return
    _cache(context, "prio_wait")[update.effective_chat.id] = subjects
    _cache(context, "subjects")[update.effective_chat.id] = subjects
    lines = [f"{i}. {render.esc(s)}" for i, s in enumerate(subjects, start=1)]
    await _reply_html(update, "<b>Tes matières</b>", lines, PROMPT_MATIERES)


async def cmd_supprimer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _require_user(update, context)
    if user is None:
        return
    chat_id = update.effective_chat.id
    purges = _cache(context, "purge")
    now = time.monotonic()
    if now - purges.get(chat_id, 0.0) <= 60:
        db.delete_user(_conn(context), user["id"])
        purges.pop(chat_id, None)
        _reset_flow(context, chat_id)
        await update.message.reply_text(
            "Ton compte et toutes tes données sont supprimés."
        )
        return
    purges[chat_id] = now
    await update.message.reply_text(
        "Ça supprime ton compte et toutes tes données PRONOTE. "
        "Renvoie /supprimer dans la minute pour confirmer."
    )


async def cmd_qui(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _cfg(context)
    if not _is_admin(update.effective_chat.id, cfg):
        await update.message.reply_text("Commande réservée à l'admin.")
        return
    conn = _conn(context)
    total = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    en_erreur = conn.execute(
        "SELECT COUNT(*) AS n FROM users WHERE last_error IS NOT NULL"
    ).fetchone()["n"]
    lines = []
    for row in conn.execute(
        "SELECT chat_id, class_name, last_error FROM users"
        " WHERE last_error IS NOT NULL ORDER BY chat_id"
    ):
        lines.append(
            f"<code>{row['chat_id']}</code> "
            f"{render.esc(row['class_name'] or '?')} — {render.esc(row['last_error'])}"
        )
    await _reply_html(
        update,
        f"<b>{total}</b> inscrit(s) · <b>{en_erreur}</b> en erreur",
        lines or ["Aucune erreur 🎉"],
    )


async def cmd_mdp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _cfg(context)
    if not _is_admin(update.effective_chat.id, cfg):
        await update.message.reply_text("Commande réservée à l'admin.")
        return
    if not context.args or not re.fullmatch(r"-?\d+", context.args[0].strip()):
        await update.message.reply_text("Envoie /mdp <chat_id>")
        return
    cible = int(context.args[0])
    conn = _conn(context)
    user = db.get_user_by_chat(conn, cible)
    if user is None:
        await update.message.reply_text("Aucun élève avec ce chat_id.")
        return
    with conn:
        conn.execute("UPDATE users SET allow_password = 1 WHERE id = ?", (user["id"],))
    _cache(context, "pw_flow")[cible] = "user"
    try:
        await context.bot.send_message(
            cible,
            "On t'a autorisé la connexion par mot de passe. Envoie ton "
            "identifiant PRONOTE.",
        )
    except Exception:
        log.exception("message mot de passe impossible")
    await update.message.reply_text(
        f"C'est fait pour {cible}. Il reçoit les instructions."
    )


# ─────────────────────────── mot de passe (dernier recours) ───────────────────────────


async def _password_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE, state: str
) -> None:
    chat_id = update.effective_chat.id
    flows = _cache(context, "pw_flow")
    text = (update.message.text or "").strip()
    if state == "user":
        _cache(context, "pw_user")[chat_id] = text
        flows[chat_id] = "pass"
        await update.message.reply_text(
            "Envoie ton mot de passe. Je supprime ton message juste après."
        )
        return

    # state == "pass" : le mot de passe ne doit rester ni dans le chat ni en base.
    try:
        await update.message.delete()
    except Exception:
        pass
    username = _cache(context, "pw_user").pop(chat_id, "")
    flows.pop(chat_id, None)
    conn, cfg = _conn(context), _cfg(context)
    user = db.get_user_by_chat(conn, chat_id)
    url = user["pronote_url"] if user is not None else cfg.default_pronote_url
    app_uuid = user["app_uuid"] if user is not None else crypto.new_app_uuid()
    try:
        client, creds = await asyncio.to_thread(
            auth.login_with_password, url, username, text, app_uuid
        )
    except Exception:
        log.info("connexion par mot de passe refusée pour chat %s", chat_id)
        await context.bot.send_message(
            chat_id,
            "Identifiant ou mot de passe refusé. Vérifie et renvoie ton "
            "identifiant, ou demande un QR à l'admin.",
        )
        return

    if user is not None:
        db.save_credentials(conn, user["id"], creds, cfg.app_secret)
        user_id = user["id"]
    else:
        class_name = await asyncio.to_thread(fetch.class_name_of, client)
        user_id = await asyncio.to_thread(
            db.create_user,
            conn,
            chat_id=chat_id,
            pronote_url=url,
            username=username,
            creds=creds,
            secret=cfg.app_secret,
            class_name=class_name,
        )
    subjects = await asyncio.to_thread(fetch.subjects_of, client, cfg)
    await context.bot.send_message(chat_id, "Connecté.")
    if subjects:
        _cache(context, "prio_wait")[chat_id] = subjects
        _cache(context, "subjects")[chat_id] = subjects
        lines = [f"{i}. {render.esc(s)}" for i, s in enumerate(subjects, start=1)]
        for part in render.compose("<b>Tes matières</b>", lines, PROMPT_MATIERES):
            await context.bot.send_message(
                chat_id, part, parse_mode=ParseMode.HTML
            )
    await _send_overdue(context.bot, conn, cfg, user_id)


# ─────────────────────────── priorités en texte libre ───────────────────────────


async def _priorities_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    subjects = _cache(context, "prio_wait").get(chat_id) or []
    nums = [int(n) for n in _ENTIERS.findall(update.message.text or "")]
    if not nums or any(n < 1 or n > len(subjects) for n in nums):
        await update.message.reply_text(
            "Réponds avec les numéros de la liste, ex. `3 1 5`."
        )
        return
    chosen: list[str] = []
    for n in nums:
        if subjects[n - 1] not in chosen:
            chosen.append(subjects[n - 1])
    user = _user_of(update, context)
    if user is None:
        await update.message.reply_text(MSG_CONNEXION)
        return
    conn, cfg = _conn(context), _cfg(context)
    db.set_priorities(conn, user["id"], chosen)
    _cache(context, "prio_wait").pop(chat_id, None)
    await update.message.reply_text("C'est noté 👍")
    await _send_overdue(context.bot, conn, cfg, user["id"])


# ─────────────────────────── chat libre ───────────────────────────

#: Formes courtes qu'un élève tape vraiment. Clé = mot de la question,
#: valeur = débuts de matière (sans accents) qu'il désigne. On n'accepte QUE
#: des tokens qui ne sont pas des mots français courants : « ses » est un
#: possessif (« j'ai fini ses exercices »), « sport » et « info » traînent dans
#: des phrases sans rapport — filtrer là-dessus cacherait toutes les autres
#: matières.
_ALIASES_MATIERES: dict[str, tuple[str, ...]] = {
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


def _fold(text: str) -> str:
    """Minuscules sans accents (comparaison de matières)."""
    import unicodedata

    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


#: Noms de matière qui sont AUSSI des mots français courants. « SES » se tape
#: exactement comme le possessif : « j'ai fini ses exercices » ne doit pas
#: filtrer le contexte sur la matière SES. On exige donc une préposition.
_MOTS_AMBIGUS = {"ses", "des", "les", "mes", "tes", "ces", "nos", "vos", "aux"}


def _nomme(folded: str, q: str) -> bool:
    """La matière est-elle nommée, sur MOT entier (pas noyée dans un mot) ?"""
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


def mentioned_subjects(question: str, subjects: list[str]) -> list[str]:
    """Matières réellement nommées dans la question.

    Sert à restreindre le contexte envoyé au LLM : avec vingt-cinq devoirs,
    « j'ai quoi en maths ? » doit être répondu sur les maths, pas sur ce que
    le modèle a envie de piocher. Volontairement conservateur : en cas de
    doute on ne filtre PAS (mieux vaut une réponse large qu'un devoir caché).
    """
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
            alias in words and any(folded.startswith(target) for target in targets)
            for alias, targets in _ALIASES_MATIERES.items()
        ):
            found.append(subject)
    return found


async def _data_block(conn, user_id: str, cfg: config.Config, question: str) -> str:
    items, overdue, upcoming = await _load_rows(conn, user_id, cfg)
    rows = list(overdue) + list(upcoming)
    asked = mentioned_subjects(question, sorted({row.item.subject for row in rows}))
    if asked and len(asked) <= 3:
        rows = [row for row in rows if row.item.subject in asked]
    rows = rows[:40]
    lines = [
        f"[{row.key[:12]}] {row.item.subject} | {row.item.kind} | "
        f"{row.item.due_date.isoformat() if row.item.due_date else '-'} | {row.summary}"
        for row in rows
    ]
    q = question.casefold()
    if any(mot in q for mot in ("cours", "rattraper", "résume", "resume")):
        cours = [i for i in items if i.kind == "cours"]
        cours.sort(key=lambda i: i.due_date or date.min, reverse=True)
        lines.append("COURS:")
        for item in cours[:10]:
            stamp = item.due_date.isoformat() if item.due_date else "?"
            lines.append(f"[{item.key[:12]}] {item.subject} | cours | {stamp} | {item.body[:160]}")
    return "\n".join(lines)


async def _chat_answer(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user
) -> None:
    conn, cfg = _conn(context), _cfg(context)
    chat_id = update.effective_chat.id
    question = (update.message.text or "").strip()
    if not db.bump_llm_count(conn, user["id"], config.today().isoformat(), 10):
        await update.message.reply_text(MSG_QUOTA)
        return
    try:
        data_block = await _data_block(conn, user["id"], cfg, question)
        answer = await asyncio.to_thread(llm.chat_answer, question, data_block, cfg)
    except Exception:
        log.exception("chat libre en échec")
        answer = None
    if answer:
        await _reply(context.bot, chat_id, answer[:700])
        return
    _, overdue, upcoming = await _load_rows(conn, user["id"], cfg)
    rows = list(overdue) + list(upcoming)
    asked = mentioned_subjects(question, sorted({row.item.subject for row in rows}))
    if asked and len(asked) <= 3:
        rows = [row for row in rows if row.item.subject in asked]
    rows = rows[:3]
    if not rows:
        await update.message.reply_text(
            "Je n'ai pas pu réfléchir, et je n'ai aucun devoir en base. "
            "Envoie /refresh."
        )
        return
    lines = render.numbered_lines(rows, config.today())
    await _reply_html(
        update,
        "Je n'ai pas pu réfléchir, voilà ta liste :",
        lines,
        "/retard pour tout voir",
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Texte libre : mot de passe, priorités, sinon question au LLM."""
    chat_id = update.effective_chat.id
    flows = _cache(context, "pw_flow")
    if chat_id in flows:
        await _password_message(update, context, flows[chat_id])
        return
    prio = _cache(context, "prio_wait")
    if chat_id in prio:
        await _priorities_message(update, context)
        return
    user = _user_of(update, context)
    if user is None:
        await update.message.reply_text(MSG_CONNEXION)
        return
    await _chat_answer(update, context, user)


# ─────────────────────────── résumé quotidien ───────────────────────────


async def digest_loop(application: Application, conn, cfg: config.Config) -> None:
    """Réveil toutes les 10 minutes ; aucune exception ne tue la boucle."""
    await asyncio.sleep(20)
    while True:
        try:
            await _digest_pass(application, conn, cfg)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("boucle de résumé : passage avorté")
        await asyncio.sleep(600)


async def _digest_pass(application: Application, conn, cfg: config.Config) -> None:
    users = await asyncio.to_thread(
        lambda: conn.execute("SELECT * FROM users").fetchall()
    )
    today = config.today().isoformat()
    for user in users:
        try:
            if user["digest_day"] == today:
                continue
            if datetime.now().hour < int(user["digest_hour"]):
                continue
            if _stale(user["last_sync_at"], hours=12):
                result = await asyncio.to_thread(
                    fetch.sync_user, conn, user["id"], cfg, "items"
                )
                if not result.ok and result.error == "token invalide":
                    await application.bot.send_message(
                        int(user["chat_id"]),
                        "Ta connexion PRONOTE a expiré. Renvoie un QR avec /start.",
                    )
                    _set_digest_day(conn, user["id"], today)
                    await asyncio.sleep(0.05)
                    continue
            await _send_digest(application, conn, cfg, user)
            _set_digest_day(conn, user["id"], today)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("résumé impossible pour %s", user["id"])
        await asyncio.sleep(0.05)


def _set_digest_day(conn, user_id: str, day: str) -> None:
    with conn:
        conn.execute("UPDATE users SET digest_day = ? WHERE id = ?", (day, user_id))


async def _send_digest(application: Application, conn, cfg: config.Config, user) -> None:
    _, overdue, upcoming = await _load_rows(conn, user["id"], cfg)
    all_rows = list(overdue) + list(upcoming)
    rows = all_rows[:5]
    vus = {row.key for row in rows}
    aujourdhui = [r for r in all_rows if r.item.due_date == config.today() and r.key not in vus]
    today = config.today()
    # On garde chaque ligne AVEC sa clé : le résumé annonce « /fait N », donc
    # la numérotation affichée doit être exactement celle que /fait résoudra —
    # et elle doit survivre à la troncature des lignes trop longues.
    pairs: list[tuple[str, str | None]] = [
        (render.format_row(i, row, today), row.key)
        for i, row in enumerate(rows, start=1)
    ]
    if aujourdhui:
        pairs.append(("<b>Pour aujourd'hui</b>", None))
        pairs.extend(
            (render.format_row(i, row, today), row.key)
            for i, row in enumerate(aujourdhui[:8], start=len(rows) + 1)
        )
    if not pairs:
        return
    header = f"<b>Résumé du jour</b> — {len(overdue)} en retard"
    footer = "/retard pour tout voir · /fait N pour cocher"
    parts = render.compose(header, [line for line, _ in pairs], footer)
    while len(parts) > 1 and len(pairs) > 1:
        pairs = pairs[:-1]
        # Une ligne retirée peut laisser l'en-tête de section tout seul en fin
        # de message : un titre « Pour aujourd'hui » suivi de rien.
        while pairs and pairs[-1][1] is None:
            pairs = pairs[:-1]
        parts = render.compose(header, [line for line, _ in pairs], footer)
    if parts:
        db.write_list_ref(conn, user["id"], [key for _, key in pairs if key])
        await application.bot.send_message(
            int(user["chat_id"]),
            parts[0],
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


# ─────────────────────────── assemblage ───────────────────────────


def build_application(cfg: config.Config, conn, request=None) -> Application:
    """Construit l'Application et enregistre TOUS les handlers (sans réseau).

    `request` permet de brancher un transport de test (aucun appel Telegram) ;
    en production il reste `None` et c'est le transport HTTP par défaut.
    """

    async def post_init(application: Application) -> None:
        application.bot_data["conn"] = conn
        application.bot_data["cfg"] = cfg
        application.create_task(digest_loop(application, conn, cfg))

    builder = Application.builder().token(cfg.telegram_token).post_init(post_init)
    if request is not None:
        builder = builder.request(request)
    application = builder.build()
    application.bot_data["conn"] = conn
    application.bot_data["cfg"] = cfg
    for name in ("qr_cache", "pending_uuid", "mfa_tries", "subjects", "prio_wait",
                 "pw_flow", "pw_user", "purge", "last_seen"):
        application.bot_data.setdefault(name, {})

    conversation = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            QR: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, qr_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, qr_text),
            ],
            PIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, pin_received)],
            ACCOUNT_PIN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, account_pin_received)
            ],
            MATIERES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, matieres_received)
            ],
        },
        fallbacks=[CommandHandler("annuler", cancel)],
        per_chat=True,
        allow_reentry=True,
    )

    handlers = [
        conversation,
        CommandHandler("retard", cmd_retard),
        CommandHandler("aujourdhui", cmd_aujourdhui),
        CommandHandler("matiere", cmd_matiere),
        CommandHandler("cours", cmd_cours),
        CommandHandler("resume", cmd_resume),
        CommandHandler("fait", cmd_fait),
        CommandHandler("refresh", cmd_refresh),
        CommandHandler("priorites", cmd_priorites),
        CommandHandler("supprimer", cmd_supprimer),
        CommandHandler("qui", cmd_qui),
        CommandHandler("mdp", cmd_mdp),
    ]
    for handler in handlers:
        application.add_handler(handler, group=0)
    application.add_handler(TypeHandler(Update, _note_activity), group=-1)
    # Le texte libre reste dans le GROUPE 0, en dernier : `python-telegram-bot`
    # évalue TOUS les groupes (un seul handler par groupe), donc un
    # `MessageHandler` en groupe 1 retraiterait « 1234 » juste après l'avoir
    # consommé comme code PIN — quota de chat brûlé à chaque inscription.
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, on_text), group=0
    )
    return application


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s : %(message)s",
    )
    try:
        cfg = config.from_env()
    except RuntimeError as exc:
        raise SystemExit(f"Configuration incomplète : {exc}") from None
    cfg.ensure_dirs()
    conn = db.connect(cfg.db_path)
    db.init_schema(conn)
    db.purge_old_summaries(conn)
    application = build_application(cfg, conn)
    log.info("pronote-blocus démarre en long polling")
    application.run_polling()


if __name__ == "__main__":
    main()
