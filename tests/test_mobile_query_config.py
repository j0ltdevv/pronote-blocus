"""`PRONOTE_MOBILE_QUERY` est appliquée à la reconnexion par jeton.

1. `mobile_url` : query vide → constante par défaut
2. `token_login` : une query personnalisée apparaît dans l'URL servie à
   pronotepy, et l'URL stockée dans les credentials n'est pas touchée
3. `from_env` → `sync_user` → `token_login` : le wiring complet du fix
"""

from cryptography.fernet import Fernet

from app import config, db
from app.pronote import auth, fetch

CREDS = {
    "pronote_url": config.DEFAULT_PRONOTE_URL,
    "username": "demonstration",
    "password": "jeton-exporte",
    "uuid": "uuid-app",
}


class _Client:
    """Double de client pronotepy : `logged_in` vrai, credentals stables."""

    logged_in = True

    def export_credentials(self) -> dict:
        return dict(CREDS)


def _cfg(tmp_path, secret: str, mobile_query: str) -> config.Config:
    """`from_env` réaliste : DATA_DIR et APP_SECRET du fixture, rien d'écrit."""
    return config.from_env(
        {
            "DATA_DIR": str(tmp_path / "data"),
            "APP_SECRET": secret,
            "PRONOTE_MOBILE_QUERY": mobile_query,
        },
        require_token=False,
    )


def _credentials(conn, secret) -> str:
    """Crée un compte avec `CREDS` et rend son `user_id`."""
    user_id = db.create_user(
        conn,
        chat_id=1,
        pronote_url=CREDS["pronote_url"],
        username=CREDS["username"],
        creds=CREDS,
        secret=secret,
    )
    db.save_credentials(conn, user_id, CREDS, secret)
    return user_id


# ── 1. mobile_url ─────────────────────────────────────────────────────────


def test_mobile_url_vide_retombe_sur_la_constante():
    url = auth.mobile_url(config.DEFAULT_PRONOTE_URL, "")

    assert url == (
        "https://0332722e.index-education.net/pronote/mobile.eleve.html"
        + config.DEFAULT_MOBILE_QUERY
    )


def test_mobile_url_blanche_retombe_aussi_sur_la_constante():
    url = auth.mobile_url(config.DEFAULT_PRONOTE_URL, "   ")

    assert url.endswith(config.DEFAULT_MOBILE_QUERY)


# ── 2. token_login ────────────────────────────────────────────────────────


def _install_token_login(monkeypatch) -> dict:
    import pronotepy

    calls: dict = {}

    def fake(
        *, pronote_url, username, password, uuid,
        account_pin=None, client_identifier=None, device_name=None,
    ):  # noqa: ANN001
        calls["pronote_url"] = pronote_url
        return _Client()

    monkeypatch.setattr(pronotepy.Client, "token_login", fake)
    return calls


def test_une_query_personnalisee_apparait_dans_l_url(monkeypatch):
    calls = _install_token_login(monkeypatch)

    auth.token_login(CREDS, mobile_query="?fd=2&bydlg=autre&login=true")

    assert calls["pronote_url"] == (
        "https://0332722e.index-education.net/pronote/mobile.eleve.html"
        "?fd=2&bydlg=autre&login=true"
    )


def test_query_vide_retombe_sur_la_constante_par_defaut(monkeypatch):
    calls = _install_token_login(monkeypatch)

    auth.token_login(CREDS, mobile_query="")

    assert calls["pronote_url"] == (
        "https://0332722e.index-education.net/pronote/mobile.eleve.html"
        + config.DEFAULT_MOBILE_QUERY
    )


def test_une_query_sans_point_interrogation_est_chez_elle(monkeypatch):
    calls = _install_token_login(monkeypatch)

    auth.token_login(CREDS, mobile_query="fd=1&login=true")

    assert calls["pronote_url"].endswith("mobile.eleve.html?fd=1&login=true")


def test_l_url_stockee_n_est_pas_modifiee(monkeypatch):
    _install_token_login(monkeypatch)
    credentials = dict(CREDS)

    auth.token_login(credentials, mobile_query="?fd=2")

    assert credentials["pronote_url"] == config.DEFAULT_PRONOTE_URL


# ── 3. config → sync_user → token_login ───────────────────────────────────


def _sync_vu(monkeypatch, conn, secret, mobile_query: str, tmp_path):
    """Lance une vraie `sync_user` et rend (cfg, query reçue, résultat)."""
    seen: dict = {}

    def fake_login(credentials, *, account_pin=None, mobile_query=""):  # noqa: ANN001
        seen["mobile_query"] = mobile_query
        return _Client()

    monkeypatch.setattr(auth, "token_login", fake_login)
    monkeypatch.setattr(fetch, "collect", lambda *a, **k: fetch.SyncResult(True))
    cfg = _cfg(tmp_path, secret, mobile_query)
    return cfg, seen, fetch.sync_user(conn, _credentials(conn, secret), cfg)


def test_sync_user_transmet_la_query_configuree(monkeypatch, tmp_path, conn, secret):
    """C'est le wiring qui porte le fix : `cfg.mobile_query` → `token_login`."""
    cfg, seen, resultat = _sync_vu(monkeypatch, conn, secret, "?fd=2", tmp_path)

    assert cfg.mobile_query == "?fd=2"
    assert seen["mobile_query"] == "?fd=2"
    assert resultat.ok


def test_sync_user_sans_query_retombe_sur_le_defaut(monkeypatch, tmp_path, conn, secret):
    cfg, seen, resultat = _sync_vu(monkeypatch, conn, secret, "", tmp_path)

    assert cfg.mobile_query == config.DEFAULT_MOBILE_QUERY
    assert seen["mobile_query"] == config.DEFAULT_MOBILE_QUERY
    assert resultat.ok
