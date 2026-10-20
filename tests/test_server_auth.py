"""Tests de l'API web : jeton d'accès, isolation des comptes, CORS.

Aucun réseau : la base vit dans `tmp_path`, les jetons dans un magasin neuf,
et les seuls appels PRONOTE (`login_with_qr`, `collect`) sont doublés.
"""

from __future__ import annotations

import importlib
from datetime import timedelta
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import db
from app.config import today as _today
from app.pronote import auth as pronote_auth
from app.pronote import fetch
from app.pronote.normalize import Item
from server import deps

SECRET = Fernet.generate_key().decode()
PRONOTE_URL = "https://demo.index-education.net/pronote/eleve.html"

#: Toutes les routes qui lisent ou écrivent les données d'un élève.
ROUTES_PROTEGEES = [
    ("GET", "/api/items", None),
    ("POST", "/api/items/done", {"item_id": "devoir:x"}),
    ("POST", "/api/items/refresh", {}),
    ("POST", "/api/chat", {"question": "quoi de neuf ?"}),
    ("POST", "/api/auth/logout", {}),
]


# ── fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Serveur isolé : config et base dans `tmp_path`, magasins mémoire vidés."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_SECRET", SECRET)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    # LLM désactivé : le chat répond par le repli local, sans appel sortant.
    for variable in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(deps, "_conn", None)
    monkeypatch.setattr(deps, "_cfg", None)
    monkeypatch.setattr(deps, "_MEMORY", {})
    monkeypatch.setattr(deps, "_SESSIONS", {})

    # Import paresseux : `server.main` ouvre la base au chargement, il doit donc
    # voir le DATA_DIR du test (jamais celui de l'instance de production).
    main = importlib.import_module("server.main")
    with TestClient(main.app) as test_client:
        yield test_client

    conn = deps._conn
    if conn is not None:
        conn.close()


@pytest.fixture
def conn(client):
    """Connexion sqlite du serveur de test."""
    return deps.get_conn()


def _devoir(subject: str, title: str) -> Item:
    due = _today() + timedelta(days=2)
    return Item(
        key=f"devoir:{subject}:{title}",
        source="devoir",
        subject=subject,
        title=title,
        body=title,
        due_date=due,
        kind="devoir",
    )


def _compte(conn, *, chat_id: int, subject: str, titre: str) -> tuple[str, str]:
    """Crée un compte avec un devoir et rend `(user_id, jeton)`."""
    user_id = db.create_user(
        conn,
        chat_id=chat_id,
        pronote_url=PRONOTE_URL,
        username=f"eleve{chat_id}",
        creds={
            "password": "jeton-initial",
            "client_identifier": "ident-1",
            "uuid": "b4f1c2a8-6d3e-4f5a-9c7b-2e8d1a0f6c33",
        },
        secret=SECRET,
    )
    db.replace_items(conn, user_id, [_devoir(subject, titre)])
    return user_id, deps.issue_token(user_id)


def dep_cookie(client):
    """Valeur du cookie de session posé par `/api/auth/pin`, ou None."""
    return client.cookies.get(deps.COOKIE_NAME)


def _entete(jeton: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {jeton}"}


# ── routes publiques ───────────────────────────────────────────────────


def test_sante_et_index_restent_publics(client):
    """`/health` et la PWA ne demandent aucun jeton."""
    sante = client.get("/health")
    assert sante.status_code == 200
    assert sante.json() == {"status": "ok"}

    index = client.get("/")
    assert index.status_code == 200
    assert "Blocus" in index.text


def test_qr_et_pin_sont_ouverts_mais_ne_lisent_rien(client):
    """Le flow de connexion est public : il sert justement à obtenir un jeton."""
    qr = client.post("/api/auth/qr", json={"pronote_url": PRONOTE_URL})
    assert qr.status_code == 200
    assert qr.json()["session_id"]

    # Sans session valide, aucun jeton n'est délivré.
    sans_session = client.post(
        "/api/auth/pin", json={"session_id": "inconnue", "pin": "1234"}
    )
    assert sans_session.status_code == 404


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/pronote/eleve.html",
        "https://127.0.0.1/pronote/eleve.html",
        "https://169.254.169.254/latest/meta-data/",
        "http://localhost/pronote/eleve.html",
    ],
)
def test_qr_refuse_les_url_internes(client, url):
    """`_qr_from_url` valide l'URL : un SSRF ne peut pas construire de QR."""
    reponse = client.post("/api/auth/qr", json={"pronote_url": url})
    assert reponse.status_code == 400
    assert "QR PRONOTE" in reponse.json()["detail"]


# ── jeton obligatoire ──────────────────────────────────────────────────


@pytest.mark.parametrize(("methode", "path", "payload"), ROUTES_PROTEGEES)
def test_route_protegee_refuse_sans_jeton(client, methode, path, payload):
    """Sans en-tête `Authorization`, toutes les routes de données sont fermées."""
    reponse = client.request(methode, path, json=payload)
    assert reponse.status_code == 401
    assert reponse.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(("methode", "path", "payload"), ROUTES_PROTEGEES)
def test_route_protegee_refuse_un_jeton_inconnu(client, methode, path, payload):
    reponse = client.request(
        methode, path, json=payload, headers=_entete("jeton-qui-n-existe-pas")
    )
    assert reponse.status_code == 401


@pytest.mark.parametrize(
    "entete",
    [
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": "Basic aWFtOm5vdA=="},
        {"Authorization": "jeton-sans-schema"},
    ],
)
def test_en_tete_mal_forme_refuse(client, entete):
    reponse = client.get("/api/items", headers=entete)
    assert reponse.status_code == 401


def test_jeton_perime_refuse(client, conn, monkeypatch):
    """Au-delà du TTL, le jeton ne vaut plus rien (et un jeton frais si)."""
    user_id, _ = _compte(conn, chat_id=1, subject="Mathématiques", titre="DM 1")

    monkeypatch.setattr(deps, "TOKEN_TTL_SECONDS", -1)
    perime = deps.issue_token(user_id)
    assert client.get("/api/items", headers=_entete(perime)).status_code == 401

    monkeypatch.setattr(deps, "TOKEN_TTL_SECONDS", 24 * 60 * 60)
    frais = deps.issue_token(user_id)
    assert client.get("/api/items", headers=_entete(frais)).status_code == 200


def test_jeton_revoque_refuse(client, conn):
    """Un jeton oublié côté serveur ne rouvre plus rien."""
    _, jeton = _compte(conn, chat_id=1, subject="Mathématiques", titre="DM 1")
    assert client.get("/api/items", headers=_entete(jeton)).status_code == 200
    deps.revoke_token(jeton)
    assert client.get("/api/items", headers=_entete(jeton)).status_code == 401


# ── isolation des comptes ──────────────────────────────────────────────


def test_chaque_jeton_ne_voit_que_son_compte(client, conn):
    """Les devoirs rendus sont ceux du compte du jeton, jamais d'un autre."""
    _, jeton_a = _compte(conn, chat_id=1, subject="Mathématiques", titre="DM 1")
    user_b, jeton_b = _compte(conn, chat_id=2, subject="SVT", titre="Schéma")

    liste_a = client.get("/api/items", headers=_entete(jeton_a)).json()
    assert [item["subject"] for item in liste_a] == ["Mathématiques"]

    liste_b = client.get("/api/items", headers=_entete(jeton_b)).json()
    assert [item["subject"] for item in liste_b] == ["SVT"]

    # `user_id` n'est plus un paramètre lu par le serveur : le passer ne change rien.
    ignore = client.get(
        "/api/items", params={"user_id": user_b}, headers=_entete(jeton_a)
    )
    assert [item["subject"] for item in ignore.json()] == ["Mathématiques"]


def test_cocher_un_devoir_n_ecrit_que_dans_son_compte(client, conn):
    """Un jeton ne peut pas cocher (ni toucher) le devoir d'un autre compte."""
    user_a, jeton_a = _compte(conn, chat_id=1, subject="Mathématiques", titre="DM 1")
    user_b, jeton_b = _compte(conn, chat_id=2, subject="SVT", titre="Schéma")
    devoir_b = client.get("/api/items", headers=_entete(jeton_b)).json()[0]["id"]

    # L'appelant A tente de cocher le devoir de B, en glissant un `user_id` au passage.
    reponse = client.post(
        "/api/items/done",
        json={"item_id": devoir_b, "user_id": "autre-compte"},
        headers=_entete(jeton_a),
    )
    assert reponse.status_code == 200

    # B n'a rien coché. Côté A, la clé atterrit dans SON propre cochage : elle
    # ne touche aucune ligne de B, et A n'hérite pas de son devoir.
    assert db.done_keys(conn, user_b) == set()
    assert db.done_keys(conn, user_a) == {devoir_b}
    assert [item["subject"] for item in client.get("/api/items", headers=_entete(jeton_a)).json()] == [
        "Mathématiques"
    ]

    # A coche SON devoir : le cochage part sous son compte. Depuis le fix
    # `include_done`, le devoir coché RESTE visible (marqué fait) pour
    # alimenter l'UI « Faits ».
    devoir_a = client.get("/api/items", headers=_entete(jeton_a)).json()[0]["id"]
    client.post("/api/items/done", json={"item_id": devoir_a}, headers=_entete(jeton_a))
    assert db.done_keys(conn, user_a) == {devoir_a, devoir_b}
    liste = client.get("/api/items", headers=_entete(jeton_a)).json()
    assert [i["id"] for i in liste] == [devoir_a]
    assert liste[0]["done"] is True
    assert db.done_keys(conn, user_b) == set()


def test_chat_ne_voit_que_les_devoirs_de_son_compte(client, conn):
    """La question libre est ancrée aux données du jeton, sans `user_id` client."""
    _, jeton_a = _compte(conn, chat_id=1, subject="Mathématiques", titre="DM 1")
    _, jeton_b = _compte(conn, chat_id=2, subject="SVT", titre="Schéma")

    reponse = client.post("/api/chat", json={"question": ""}, headers=_entete(jeton_a))
    assert reponse.status_code == 200
    assert "Mathématiques" in reponse.json()["answer"]
    assert "SVT" not in reponse.json()["answer"]

    autre = client.post("/api/chat", json={"question": ""}, headers=_entete(jeton_b))
    assert "SVT" in autre.json()["answer"]


# ── déconnexion ────────────────────────────────────────────────────────


def test_logout_revoque_le_jeton_et_supprime_le_compte_appelant(client, conn):
    user_a, jeton_a = _compte(conn, chat_id=1, subject="Mathématiques", titre="DM 1")
    _, jeton_b = _compte(conn, chat_id=2, subject="SVT", titre="Schéma")

    assert client.post("/api/auth/logout", headers=_entete(jeton_a)).json() == {"ok": True}

    # Le compte visé est celui du jeton : l'autre élève est intact.
    assert db.get_user(conn, user_a) is None
    assert client.get("/api/items", headers=_entete(jeton_b)).status_code == 200

    # Jeton révoqué : plus rien n'est accessible, même pas un rafraîchissement.
    assert client.get("/api/items", headers=_entete(jeton_a)).status_code == 401
    assert client.post("/api/items/refresh", headers=_entete(jeton_a)).status_code == 401
    assert client.post("/api/auth/logout", headers=_entete(jeton_a)).status_code == 401


def test_logout_ne_prend_pas_de_user_id(client, conn):
    """Un `user_id` glissé dans le corps est ignoré (aucun autre compte touché)."""
    user_a, jeton_a = _compte(conn, chat_id=1, subject="Mathématiques", titre="DM 1")
    user_b, jeton_b = _compte(conn, chat_id=2, subject="SVT", titre="Schéma")

    reponse = client.post(
        "/api/auth/logout", json={"user_id": user_b}, headers=_entete(jeton_a)
    )
    assert reponse.status_code == 200
    assert db.get_user(conn, user_a) is None
    assert db.get_user(conn, user_b) is not None
    assert client.get("/api/items", headers=_entete(jeton_b)).status_code == 200


# ── connexion PIN : délivrance du jeton ────────────────────────────────


@pytest.fixture
def pronote_hors_ligne(monkeypatch):
    """Double `login_with_qr` / `collect` : la connexion PIN sans réseau."""
    appels: dict[str, object] = {}

    def fake_login(qr, pin, app_uuid, account_pin=None):  # noqa: ANN001
        appels["pin"] = pin
        client_pronote = SimpleNamespace(logged_in=True)
        creds = {
            "password": "jeton-pronote",
            "client_identifier": "ident-1",
            "uuid": app_uuid,
            "username": qr.get("login", ""),
        }
        return client_pronote, creds

    def fake_collect(client_pronote, conn, user_id, cfg, phase="items"):  # noqa: ANN001
        appels["collecte"] = (user_id, phase)
        return SimpleNamespace(ok=True, items=[])

    monkeypatch.setattr(pronote_auth, "login_with_qr", fake_login)
    monkeypatch.setattr(fetch, "class_name_of", lambda client_pronote: "Terminale")
    monkeypatch.setattr(fetch, "collect", fake_collect)
    return appels


def test_pin_delivre_un_jeton_exploitable(client, conn, pronote_hors_ligne):
    """`/pin` rend un jeton opaque, et ce jeton ouvre les routes de données."""
    qr = client.post("/api/auth/qr", json={"pronote_url": PRONOTE_URL}).json()
    reponse = client.post(
        "/api/auth/pin", json={"session_id": qr["session_id"], "pin": "1234"}
    )
    assert reponse.status_code == 200
    corps = reponse.json()
    assert pronote_hors_ligne["pin"] == "1234"

    jeton = corps["token"]
    assert jeton and len(jeton) >= 32
    assert jeton != corps["user_id"]
    assert corps["subjects"] == []
    assert pronote_hors_ligne["collecte"] == (corps["user_id"], "items")

    liste = client.get("/api/items", headers=_entete(jeton))
    assert liste.status_code == 200
    assert liste.json() == []
    assert db.get_user(conn, corps["user_id"]) is not None

    # Le cookie httpOnly est posé par /pin : il vaut jeton à lui seul, c'est
    # ce qui permet à l'élève de rester connecté sans refaire un QR.
    assert dep_cookie(client) is not None
    assert client.get("/api/items").status_code == 200

    # Sans en-tête NI cookie, rien n'est accessible.
    client.cookies.clear()
    assert client.get("/api/items").status_code == 401


def test_deux_logins_donnent_deux_jetons_independants(client, conn, pronote_hors_ligne):
    """Deux connexions au même compte : deux jetons, révoqués séparément."""
    user_id, jeton_un = _compte(conn, chat_id=1, subject="Mathématiques", titre="DM 1")
    jeton_deux = deps.issue_token(user_id)

    assert client.get("/api/items", headers=_entete(jeton_un)).status_code == 200
    assert client.get("/api/items", headers=_entete(jeton_deux)).status_code == 200

    client.post("/api/auth/logout", headers=_entete(jeton_un))
    # Le compte est supprimé : les deux jetons tombent d'un coup.
    assert client.get("/api/items", headers=_entete(jeton_un)).status_code == 401
    assert client.get("/api/items", headers=_entete(jeton_deux)).status_code == 401


def test_la_session_survit_a_un_redemarrage(client, conn, pronote_hors_ligne):
    """Le jeton est en base : vider la mémoire ne déconnecte pas l'élève.

    Sans cette persistance, chaque redémarrage du serveur — et chaque
    expiration à 24 h — obligeait à régénérer un QR, donc à retrouver un
    ordinateur. Une fois le QR fait, le cookie suffit.
    """
    qr = client.post("/api/auth/qr", json={"pronote_url": PRONOTE_URL}).json()
    reponse = client.post(
        "/api/auth/pin", json={"session_id": qr["session_id"], "pin": "1234"}
    )
    assert reponse.status_code == 200
    jeton = reponse.json()["token"]

    # Redémarrage simulé : tout ce qui vivait en mémoire disparaît.
    deps._MEMORY.clear()
    client.cookies.clear()

    assert deps.resolve_token(jeton, conn) is not None
    assert client.get("/api/items", headers=_entete(jeton)).status_code == 200


def test_un_jeton_revoque_ne_ressuscite_pas_par_le_cookie(client, conn, pronote_hors_ligne):
    """L'en-tête fait foi : un jeton mort ne redevient pas valide via le cookie."""
    qr = client.post("/api/auth/qr", json={"pronote_url": PRONOTE_URL}).json()
    corps = client.post(
        "/api/auth/pin", json={"session_id": qr["session_id"], "pin": "1234"}
    ).json()
    jeton = corps["token"]

    # Le cookie est toujours là, l'en-tête porte un jeton inconnu.
    assert client.cookies.get(deps.COOKIE_NAME) is not None
    assert client.get("/api/items", headers=_entete("jeton-mort")).status_code == 401

    # Et le jeton révoqué, présenté en en-tête, est refusé même avec le cookie.
    client.post("/api/auth/logout", headers=_entete(jeton))
    assert client.get("/api/items", headers=_entete(jeton)).status_code == 401


def test_le_cookie_seul_authentifie(client, conn, pronote_hors_ligne):
    """Sans en-tête, le cookie httpOnly suffit — c'est le cas « navigateur relancé »."""
    qr = client.post("/api/auth/qr", json={"pronote_url": PRONOTE_URL}).json()
    client.post("/api/auth/pin", json={"session_id": qr["session_id"], "pin": "1234"})
    assert client.cookies.get(deps.COOKIE_NAME) is not None
    assert client.get("/api/items").status_code == 200


@pytest.fixture
def pronote_mot_de_passe(monkeypatch):
    """Double `login_with_password` : aucune requête réseau dans les tests."""
    appels: dict[str, object] = {}

    def fake_login(url, username, password, app_uuid, ent=None):  # noqa: ANN001
        appels["ent"] = ent
        appels["username"] = username
        return (
            SimpleNamespace(logged_in=True),
            {
                "password": password,
                "client_identifier": "ident-1",
                "uuid": app_uuid,
                "username": username,
            },
        )

    monkeypatch.setattr(pronote_auth, "login_with_password", fake_login)
    monkeypatch.setattr(fetch, "class_name_of", lambda client_pronote: "Terminale")
    monkeypatch.setattr(
        fetch, "collect", lambda c, conn, uid, cfg, phase="items": SimpleNamespace(ok=True)
    )
    return appels


def test_le_mot_de_passe_ouvre_une_session(client, conn, pronote_mot_de_passe):
    """Sans ordinateur : identifiant + mot de passe → jeton et cookie."""
    reponse = client.post(
        "/api/auth/password",
        json={
            "pronote_url": PRONOTE_URL,
            "username": "eleve",
            "password": "secret",
        },
    )
    assert reponse.status_code == 200, reponse.text
    corps = reponse.json()
    assert corps["token"]
    assert pronote_mot_de_passe["username"] == "eleve"
    assert client.cookies.get(deps.COOKIE_NAME) is not None
    assert client.get("/api/items").status_code == 200

    # Le mot de passe n'est pas stocké en clair : seule la valeur exportée
    # par PRONOTE est chiffrée en base.
    brut = conn.execute("select token_enc from users").fetchone()[0]
    assert b"secret" not in bytes(brut)


def test_le_mot_de_passe_peut_passer_par_un_ent(client, pronote_mot_de_passe):
    """Quand le lycée est derrière un ENT, le handler correspondant est résolu."""
    reponse = client.post(
        "/api/auth/password",
        json={
            "pronote_url": PRONOTE_URL,
            "username": "eleve",
            "password": "secret",
            "ent": "lyceeconnecte_aquitaine",
        },
    )
    assert reponse.status_code == 200, reponse.text
    assert callable(pronote_mot_de_passe["ent"])


def test_un_ent_inconnu_est_refuse(client, pronote_mot_de_passe):
    reponse = client.post(
        "/api/auth/password",
        json={
            "pronote_url": PRONOTE_URL,
            "username": "eleve",
            "password": "secret",
            "ent": "pas-un-ent",
        },
    )
    assert reponse.status_code == 400


def test_le_mot_de_passe_peut_etre_desactive(client, conn, monkeypatch, pronote_mot_de_passe):
    """`ALLOW_PASSWORD_LOGIN=0` ferme la route, sans toucher au reste."""
    from app.config import from_env

    cfg = from_env(require_token=False)
    object.__setattr__(cfg, "allow_password_login", False)
    # `_bootstrap` reconstruit la config dès que `_conn` est absent : on fige
    # les deux pour que la requête voie bien le drapeau désactivé.
    monkeypatch.setattr(deps, "_conn", conn)
    monkeypatch.setattr(deps, "_cfg", cfg)

    reponse = client.post(
        "/api/auth/password",
        json={"pronote_url": PRONOTE_URL, "username": "e", "password": "p"},
    )
    assert reponse.status_code == 403


# ── CORS ───────────────────────────────────────────────────────────────


def test_cors_autorise_l_origine_web_et_pas_les_autres(client):
    """L'origine du frontend est reflétée ; les autres n'obtiennent rien."""
    preflight = client.options(
        "/api/items",
        headers={
            "Origin": "http://localhost:8000",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://localhost:8000"
    assert preflight.headers["access-control-allow-credentials"] == "true"

    inconnue = client.get("/api/items", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in inconnue.headers

    # Jamais de joker avec des identifiants.
    joker = client.options(
        "/api/items",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert joker.headers.get("access-control-allow-origin") != "*"


def test_origines_lues_dans_l_environnement(monkeypatch):
    """`WEB_ORIGIN` pilote la liste blanche, avec un défaut local."""
    main = importlib.import_module("server.main")

    monkeypatch.delenv("WEB_ORIGIN", raising=False)
    assert main.web_origins() == ["http://localhost:8000"]

    monkeypatch.setenv("WEB_ORIGIN", "https://blocus.example")
    assert main.web_origins() == ["https://blocus.example"]

    monkeypatch.setenv("WEB_ORIGIN", "https://blocus.example, http://localhost:5173")
    assert main.web_origins() == ["https://blocus.example", "http://localhost:5173"]

    monkeypatch.setenv("WEB_ORIGIN", "   ")
    assert main.web_origins() == ["http://localhost:8000"]
