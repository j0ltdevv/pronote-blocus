"""`app.llm` : transport OpenAI-compatible, replis, quota et confidentialité.

Aucun réseau : `httpx.post` est TOUJOURS remplacé par un faux qui enregistre
chaque requête. Le quota et le filtrage `LLM_MESSAGES` sont observés à travers
le vrai handler `_chat_answer` (le seul consommateur du quota).
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from types import SimpleNamespace

import httpx
import pytest

from app import config, db, llm
from app import main as bot_main
from app.pronote import ranking
from app.pronote.normalize import make_item

CHAT = 4242


# ─────────────────────────── montage ───────────────────────────


def _env(tmp_path, secret: str, **overrides: str) -> dict[str, str]:
    env = {
        "TELEGRAM_BOT_TOKEN": "123456:TEST-TOKEN",
        "APP_SECRET": secret,
        "DATA_DIR": str(tmp_path / "data"),
        "WINDOW_START": "2026-10-01",
        "LLM_BASE_URL": "http://127.0.0.1:9/v1",
        "LLM_API_KEY": "cle-de-test",
        "LLM_MODEL": "modele-de-test",
    }
    env.update(overrides)
    return env


@pytest.fixture
def cfg(tmp_path, secret) -> config.Config:
    return config.from_env(_env(tmp_path, secret))


@pytest.fixture
def cfg_sans_llm(tmp_path, secret) -> config.Config:
    env = _env(tmp_path, secret)
    for key in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        env.pop(key)
    return config.from_env(env)


class _Response:
    """Réponse httpx minimale : ni socket, ni encodage réel."""

    def __init__(
        self,
        payload=None,  # noqa: ANN001
        *,
        json_error: Exception | None = None,
        status_error: Exception | None = None,
    ) -> None:
        self._payload = payload
        self._json_error = json_error
        self._status_error = status_error

    def raise_for_status(self) -> None:
        if self._status_error is not None:
            raise self._status_error

    def json(self):  # noqa: ANN201
        if self._json_error is not None:
            raise self._json_error
        return self._payload


@pytest.fixture
def http(monkeypatch):
    """Remplace l'unique sortie réseau de `app.llm` et rend le journal."""
    state = SimpleNamespace(calls=[], response=None, error=None)

    def post(url: str, **kwargs):  # noqa: ANN201
        state.calls.append((url, kwargs))
        if state.error is not None:
            raise state.error
        assert state.response is not None, "aucune réponse programmée"
        return state.response

    monkeypatch.setattr(llm.httpx, "post", post)
    return state


def _message_payload(content=None, reasoning=None) -> dict:  # noqa: ANN001
    message: dict[str, str] = {}
    if content is not None:
        message["content"] = content
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return {"choices": [{"message": message}]}


def _item(subject: str, body: str, *, kind: str, source: str):  # noqa: ANN202
    return make_item(
        kind=kind,
        source=source,
        subject=subject,
        body=body,
        due_date=date(2026, 10, 20),
    )


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:  # noqa: ANN003
        self.sent.append(text)


class _FakeMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text: str, **kwargs) -> None:  # noqa: ANN003
        self.replies.append(text)


def _update(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=CHAT),
        message=_FakeMessage(text),
    )


def _context(conn, cfg_: config.Config) -> tuple[SimpleNamespace, _FakeBot]:  # noqa: ANN001
    bot = _FakeBot()
    application = SimpleNamespace(bot_data={"conn": conn, "cfg": cfg_})
    return SimpleNamespace(application=application, bot=bot), bot


def _no_rows(monkeypatch) -> None:  # noqa: ANN001
    async def rows(conn, user_id, cfg_):  # noqa: ANN001, ARG001
        return [], [], []

    monkeypatch.setattr(bot_main, "_load_rows", rows)


def _chat_once(cfg_: config.Config, text: str = "question") -> str | None:
    return llm._chat(cfg_, [{"role": "user", "content": text}], max_tokens=32)


# ─────────────────────────── 1. mode dégradé ───────────────────────────


def test_sans_llm_le_bot_repond_quand_meme(conn, make_user, cfg_sans_llm, monkeypatch):
    """Aucune clé LLM : le message de repli part, aucune exception ne remonte."""
    uid = make_user(chat_id=CHAT)
    _no_rows(monkeypatch)

    def interdit(*args):  # noqa: ANN002
        raise AssertionError("le LLM a été appelé alors que cfg.llm_enabled est False")

    monkeypatch.setattr(llm, "chat_answer", interdit)
    monkeypatch.setattr(llm.httpx, "post", interdit)

    update = _update("j'ai quoi en maths ?")
    ctx, _bot = _context(conn, cfg_sans_llm)
    asyncio.run(bot_main._chat_answer(update, ctx, {"id": uid}))

    assert cfg_sans_llm.llm_enabled is False
    assert update.message.replies == [
        "Je n'ai pas pu réfléchir, et je n'ai aucun devoir en base. Envoie /refresh."
    ]


def test_sans_llm_les_replis_sont_deterministes_et_sans_reseau(
    conn, cfg_sans_llm, http
):
    item = _item("Mathématiques", "Exercices 12 à 15 pour vendredi", kind="devoir", source="travail_a_faire")

    assert llm.chat_answer("question", "DONNÉES", cfg_sans_llm) is None
    assert llm.weekly_synthesis([], cfg_sans_llm) is None
    assert llm.extract_devoir_checked("Sujet", "Message de prof", cfg_sans_llm) == (None, False)

    fallback = llm.summarize_many([("cle-1", item.body)], cfg_sans_llm)
    assert fallback["cle-1"] == {
        "summary": item.body,
        "action": item.body[:90],
        "effort": "moyen",
    }

    cached = llm.ensure_summaries(conn, [item], cfg_sans_llm)
    assert cached[item.key]["summary"] == item.body
    assert http.calls == []
    assert conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 0


def test_un_resume_deja_en_cache_ne_repart_pas_au_modele(conn, cfg, http):
    item = _item("SVT", "Lire le chapitre 2", kind="devoir", source="travail_a_faire")
    db.put_summaries(
        conn,
        cfg.llm_model,
        {item.key: {"summary": "Déjà résumé", "action": "relire", "effort": "court"}},
    )

    got = llm.ensure_summaries(conn, [item], cfg)

    assert got[item.key]["summary"] == "Déjà résumé"
    assert http.calls == []


def test_une_reponse_partielle_garde_le_repli_des_cles_absentes(conn, cfg, http):
    premier = _item("SVT", "Lire le chapitre 2", kind="devoir", source="travail_a_faire")
    second = _item("Anglais", "Apprendre les verbes irréguliers", kind="devoir", source="travail_a_faire")
    http.response = _Response(
        _message_payload(
            content=json.dumps(
                {
                    "items": [
                        {
                            "key": premier.key,
                            "summary": "Lire le chapitre 2",
                            "action": "lire le chapitre 2",
                            "effort": "court",
                        }
                    ]
                }
            )
        )
    )

    got = llm.ensure_summaries(conn, [premier, second], cfg)

    assert got[premier.key]["summary"] == "Lire le chapitre 2"
    assert got[second.key]["summary"] == second.body


# ─────────────────────────── 2. reasoning_content ───────────────────────────


def test_reasoning_content_sert_quand_content_est_vide(cfg, http):
    http.response = _Response(_message_payload(content="", reasoning="Réponse réfléchie"))

    assert _chat_once(cfg) == "Réponse réfléchie"

    url, kwargs = http.calls[0]
    assert url == "http://127.0.0.1:9/v1/chat/completions"
    assert kwargs["headers"]["Authorization"] == "Bearer cle-de-test"
    assert kwargs["json"]["model"] == "modele-de-test"


def test_reasoning_content_sert_quand_content_est_absent(cfg, http):
    http.response = _Response(_message_payload(reasoning="Réponse réfléchie"))

    assert _chat_once(cfg) == "Réponse réfléchie"


def test_content_prioritaire_quand_les_deux_existent(cfg, http):
    http.response = _Response(
        _message_payload(content="Réponse finale", reasoning="Brouillon interne")
    )

    assert _chat_once(cfg) == "Réponse finale"


def test_une_reponse_sans_aucun_texte_vaut_none(cfg, http):
    http.response = _Response(_message_payload(content="", reasoning=None))

    assert _chat_once(cfg) is None


def test_json_mode_est_transmis_dans_le_corps(cfg, http):
    http.response = _Response(_message_payload(content="{}"))

    llm._chat(cfg, [{"role": "user", "content": "x"}], max_tokens=32, json_mode=True)

    assert http.calls[0][1]["json"]["response_format"] == {"type": "json_object"}


# ─────────────────────────── 3. quota quotidien ───────────────────────────


def test_quota_dix_questions_par_jour_puis_remise_a_zero(
    conn, make_user, cfg, monkeypatch
):
    uid = make_user(chat_id=CHAT)
    _no_rows(monkeypatch)
    monkeypatch.setattr(llm, "chat_answer", lambda question, block, cfg_: "Réponse simulée.")
    ctx, bot = _context(conn, cfg)
    user = {"id": uid}

    monkeypatch.setattr(config, "today", lambda: date(2026, 10, 17))
    for numero in range(10):
        update = _update(f"question {numero}")
        asyncio.run(bot_main._chat_answer(update, ctx, user))
        assert bot.sent == ["Réponse simulée."], f"question {numero} non servie"
        bot.sent.clear()

    refusee = _update("question 11")
    asyncio.run(bot_main._chat_answer(refusee, ctx, user))
    assert refusee.message.replies == [bot_main.MSG_QUOTA]
    assert bot.sent == [], "le 11e appel a tout de même interrogé le modèle"
    assert db.get_user(conn, uid)["llm_count"] == 10

    monkeypatch.setattr(config, "today", lambda: date(2026, 10, 18))
    lendemain = _update("nouveau jour")
    asyncio.run(bot_main._chat_answer(lendemain, ctx, user))
    assert bot.sent == ["Réponse simulée."]

    row = db.get_user(conn, uid)
    assert (row["llm_day"], row["llm_count"]) == ("2026-10-18", 1)


def test_sans_llm_aucun_quota_consomme(conn, make_user, cfg_sans_llm, monkeypatch):
    uid = make_user(chat_id=CHAT)
    _no_rows(monkeypatch)
    ctx, _bot = _context(conn, cfg_sans_llm)

    asyncio.run(bot_main._chat_answer(_update("une question"), ctx, {"id": uid}))

    row = db.get_user(conn, uid)
    assert (row["llm_day"], row["llm_count"]) == (None, 0)


# ─────────────────────────── 4. LLM_MESSAGES=0 ───────────────────────────


def test_llm_messages_0_n_envoie_jamais_le_corps_d_un_message(
    conn, tmp_path, secret, http
):
    cfg_ = config.from_env(_env(tmp_path, secret, LLM_MESSAGES="0"))
    item = _item(
        "Mathématiques",
        "CORPS CONFIDENTIEL DU PROF",
        kind="message",
        source="messagerie",
    )

    summaries = llm.ensure_summaries(conn, [item], cfg_)

    assert http.calls == [], "le corps d'un message de prof est parti au modèle"
    assert "CORPS CONFIDENTIEL DU PROF" in summaries[item.key]["summary"]
    assert conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 0


def test_llm_messages_1_envoie_le_message_pour_le_resume(
    conn, tmp_path, secret, http
):
    """Témoin : sans le réglage, le même corps part bien (le test sait le voir)."""
    cfg_ = config.from_env(_env(tmp_path, secret, LLM_MESSAGES="1"))
    item = _item(
        "Mathématiques",
        "CORPS CONFIDENTIEL DU PROF",
        kind="message",
        source="messagerie",
    )
    http.response = _Response(
        _message_payload(
            content=json.dumps(
                {
                    "items": [
                        {
                            "key": item.key,
                            "summary": "Relire la leçon",
                            "action": "relire la leçon",
                            "effort": "court",
                        }
                    ]
                }
            )
        )
    )

    summaries = llm.ensure_summaries(conn, [item], cfg_)

    assert len(http.calls) == 1
    envoye = json.dumps(http.calls[0][1]["json"], ensure_ascii=False)
    assert "CORPS CONFIDENTIEL DU PROF" in envoye
    assert summaries[item.key]["summary"] == "Relire la leçon"


def _data_block_rows():
    message = _item("Mathématiques", "CORPS DU MESSAGE DU PROF", kind="message", source="messagerie")
    devoir = _item("SVT", "Lire le chapitre 2", kind="devoir", source="travail_a_faire")
    cours = _item("Histoire", "CONTENU DÉTAILLÉ DU COURS", kind="cours", source="cahier_de_textes")
    rows = [ranking.to_row(message, None), ranking.to_row(devoir, None)]
    return [message, devoir, cours], rows, []


def test_data_block_sans_llm_messages_filtre_messages_et_cours(
    tmp_path, secret, monkeypatch
):
    cfg_ = config.from_env(_env(tmp_path, secret, LLM_MESSAGES="0"))

    async def rows(conn, user_id, cfg):  # noqa: ANN001, ARG001
        return _data_block_rows()

    monkeypatch.setattr(bot_main, "_load_rows", rows)

    block = asyncio.run(
        bot_main._data_block(None, "eleve", cfg_, "résume mes cours")
    )

    assert "CORPS DU MESSAGE DU PROF" not in block
    assert "CONTENU DÉTAILLÉ DU COURS" not in block
    assert "Lire le chapitre 2" in block


def test_data_block_avec_llm_messages_inclut_les_cours(tmp_path, secret, monkeypatch):
    """Témoin : réglage actif, le cours demandé est bien transmis."""
    cfg_ = config.from_env(_env(tmp_path, secret, LLM_MESSAGES="1"))

    async def rows(conn, user_id, cfg):  # noqa: ANN001, ARG001
        return _data_block_rows()

    monkeypatch.setattr(bot_main, "_load_rows", rows)

    block = asyncio.run(
        bot_main._data_block(None, "eleve", cfg_, "résume mes cours")
    )

    assert "CONTENU DÉTAILLÉ DU COURS" in block


# ─────────────────────────── extraction d'un devoir ───────────────────────────


def test_extraction_lit_un_json_entoure_de_markdown(cfg, http):
    due = (date.today() + timedelta(days=3)).isoformat()
    http.response = _Response(
        _message_payload(
            content=(
                "```json\n"
                '{"matiere":"Mathématiques","detail":"Exercices 12 à 15",'
                f'"due":"{due}"}}\n'
                "```"
            )
        )
    )

    found, interpretable = llm.extract_devoir_checked(
        "Devoirs", "Exercices 12 à 15 pour la semaine prochaine", cfg
    )

    assert interpretable is True
    assert found == {"matiere": "Mathématiques", "detail": "Exercices 12 à 15", "due": due}
    assert "Exercices 12 à 15 pour la semaine prochaine" in http.calls[0][1]["json"]["messages"][1]["content"]


def test_un_null_est_retente_une_fois(cfg, http):
    http.response = _Response(_message_payload(content="null"))

    assert llm.extract_devoir("Sujet", "Rien à faire", cfg) is None
    assert len(http.calls) == 2, "un null interprétable est retenté une seule fois"


def test_extraction_avec_une_reponse_cassee_ne_remonte_jamais(cfg, http):
    http.response = _Response(_message_payload(content="pas de json ici"))

    assert llm.extract_devoir_checked("Sujet", "Message", cfg) == (None, False)
    assert len(http.calls) == 1


# ─────────────────────────── synthèse hebdomadaire ───────────────────────────


def test_synthese_hebdo_garde_cinq_lignes(cfg, http):
    item = _item("SVT", "Lire le chapitre 2", kind="devoir", source="travail_a_faire")
    row = ranking.to_row(item, None)
    http.response = _Response(
        _message_payload(
            content="ligne 1\nligne 2\n\nligne 3\nligne 4\nligne 5\nligne 6"
        )
    )

    resultat = llm.weekly_synthesis([row], cfg)

    assert resultat == "ligne 1\nligne 2\nligne 3\nligne 4\nligne 5"
    assert "Lire le chapitre 2" in http.calls[0][1]["json"]["messages"][1]["content"]


# ─────────────────────────── 5. pannes ───────────────────────────


@pytest.mark.parametrize(
    "factory",
    [
        pytest.param(lambda: httpx.ConnectError("réseau coupé"), id="connexion"),
        pytest.param(lambda: httpx.ReadTimeout("trop lent"), id="timeout"),
        pytest.param(
            lambda: httpx.HTTPStatusError(
                "500",
                request=httpx.Request("POST", "http://127.0.0.1:9/v1/chat/completions"),
                response=httpx.Response(500),
            ),
            id="http-500",
        ),
    ],
)
def test_erreur_de_transport_ne_remonte_jamais(cfg, http, factory):
    http.error = factory()

    assert _chat_once(cfg) is None


def test_reponse_non_json_ne_remonte_jamais(cfg, http):
    http.response = _Response(json_error=json.JSONDecodeError("cassé", "<html>", 0))

    assert _chat_once(cfg) is None


def test_reponse_de_forme_inattendue_ne_remonte_jamais(cfg, http):
    http.response = _Response({"choices": []})

    assert _chat_once(cfg) is None


def _status_error(status: int) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(
        str(status),
        request=httpx.Request("POST", "http://127.0.0.1:9/v1/chat/completions"),
        response=httpx.Response(status),
    )


@pytest.mark.parametrize("status", [400, 413, 422, 429, 503])
def test_un_refus_temporaire_pause_sans_couper_le_service(cfg, http, status):
    """429/413/422/400/503 : « ralentis », pas « en panne ».

    Couper le service dix minutes sur un 429 privait tous les élèves d'IA pour
    le pic d'un seul instant — et sur le chemin des résumés, qui 429 le plus.
    """
    http.error = _status_error(status)

    assert _chat_once(cfg) is None
    assert not llm._is_down()
    assert llm._is_paused()


def test_la_pause_ne_ressert_pas_le_fournisseur(cfg, http):
    """Pendant la pause, aucune requête ne part : c'est tout l'intérêt."""
    http.error = _status_error(429)
    assert _chat_once(cfg) is None

    http.calls.clear()
    http.response = _Response(_message_payload(content="trop tard"))

    assert _chat_once(cfg) is None
    assert http.calls == []


def test_une_panne_franche_ouvre_le_circuit(cfg, http):
    """500 et coupure réseau restent des pannes : le circuit s'ouvre."""
    http.error = _status_error(500)

    assert _chat_once(cfg) is None
    assert llm._is_down()
    assert not llm._is_paused()


def test_un_lot_de_resumes_rate_limite_ne_coupe_pas_le_service(cfg, http, conn, make_user):
    """Le chemin des résumés ne doit pas transformer un 429 en panne de 10 min."""
    user = make_user()
    item = _item("Maths", "Exercices 12 à 18 page 47", kind="devoir", source="travail_a_faire")
    http.error = _status_error(429)

    resultat = llm.ensure_summaries(conn, [item], cfg)

    assert resultat[item.key]["summary"] == item.body
    assert not llm._is_down()


def test_les_appels_simultanes_sont_bornees(cfg, monkeypatch):
    """Le fournisseur compte nos requêtes simultanées, pas nos threads."""
    import threading
    import time as _time

    en_vol = 0
    maximum = 0
    verrou = threading.Lock()

    def post(url, **kwargs):  # noqa: ANN001, ANN202
        nonlocal en_vol, maximum
        with verrou:
            en_vol += 1
            maximum = max(maximum, en_vol)
        _time.sleep(0.05)
        with verrou:
            en_vol -= 1
        return _Response(_message_payload(content="ok"))

    monkeypatch.setattr(llm.httpx, "post", post)

    threads = [threading.Thread(target=_chat_once, args=(cfg,)) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert maximum <= llm.MAX_CONCURRENT_CALLS
    assert maximum == llm.MAX_CONCURRENT_CALLS


def test_slots_occupees_rendent_la_main_au_lieu_de_bloquer(cfg, monkeypatch):
    """Sans attente bornée, quatre appels de 15 s gèreraient tout le pool.

    `ensure_summaries` est appelé depuis des endpoints synchrones : le thread
    qui attend un slot est un thread qui ne sert plus la liste des devoirs.
    """
    import threading
    import time as _time

    debut = threading.Event()

    def post(url, **kwargs):  # noqa: ANN001, ANN202
        debut.wait(5)
        return _Response(_message_payload(content="ok"))

    monkeypatch.setattr(llm.httpx, "post", post)

    # On sature les slots avec des appels qui ne rendent pas la main.
    for _ in range(llm.MAX_CONCURRENT_CALLS):
        threading.Thread(target=_chat_once, args=(cfg,), daemon=True).start()
    _time.sleep(0.2)

    t0 = _time.monotonic()
    assert _chat_once(cfg) is None
    ecoule = _time.monotonic() - t0

    debut.set()
    assert ecoule < llm.SLOT_WAIT_SECONDS + 1.5, f"attente trop longue : {ecoule:.1f}s"


def test_une_panne_donne_le_repli_sans_exception(cfg, http, conn):
    http.error = httpx.ConnectError("réseau coupé")
    item = _item("SVT", "Lire le chapitre 2", kind="devoir", source="travail_a_faire")
    row = ranking.to_row(item, None)

    assert llm.chat_answer("question", "DONNÉES", cfg) is None
    assert llm.weekly_synthesis([row], cfg) is None
    assert llm.extract_devoir_checked("Sujet", "Message", cfg) == (None, False)

    fallback = llm.summarize_many([(item.key, item.body)], cfg)
    assert fallback[item.key]["summary"] == item.body

    assert llm.ensure_summaries(conn, [item], cfg)[item.key]["summary"] == item.body
    assert conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 0
