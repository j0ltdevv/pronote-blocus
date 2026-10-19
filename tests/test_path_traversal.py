"""Confinement des chemins de fichiers exposés par `server/`.

Audit : le seul chemin de fichier atteignable depuis le réseau est le montage
statique de `web/` (`server/main.py`, `StaticFiles`). Les autres constructions
de chemins sont internes ou pilotées par l'environnement :
- verrous `locks/<uuid>.lock` (`app/pronote/fetch.py`) — `user_id` est un
  uuid4 généré par le serveur (`db.create_user`), jamais une entrée client ;
- base `DATA_DIR/app.db` et `app_secret` (`app/config.py`) — variables d'env ;
- dossier `uploads/` créé par `Config.ensure_dirs` mais jamais lu ni écrit.

Ces tests verrouillent le comportement du montage statique : les entrées
utilisateur (sauts `..`, encodages `%2e%2e`, chemins absolus, liens
symboliques) doivent rester confinées dans `web/`.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.staticfiles import StaticFiles

from server import deps

SECRET = Fernet.generate_key().decode()
ROOT = Path(__file__).resolve().parents[1]

#: Marqueurs de fichiers situés HORS de `web/` : leur apparition dans une
#: réponse signerait une fuite hors de la racine servie.
_MARQUEUR_PYPROJECT = 'name = "pronote-blocus"'
_MARQUEUR_PASSWD = "root:"

#: Sauts de répertoire, bruts et encodés (%2e=%2E=., %2f=%2F=/, %5c=\)
#: y compris double encodage (aucun décodage répété ne doit avoir lieu).
_CHEMINS_TRAVERSANTS = [
    "../pyproject.toml",
    "../tests/conftest.py",
    "../../pyproject.toml",
    "..%2Fpyproject.toml",
    "%2e%2e%2fpyproject.toml",
    "%2e%2e/pyproject.toml",
    "%2e%2e%2f%2e%2e%2fpyproject.toml",
    "....//pyproject.toml",
    "..%5C..%5Cpyproject.toml",
    "%252e%252e%252fpyproject.toml",
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Serveur isolé : même montage statique que la production (`web/`)."""
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


def test_le_montage_statique_sert_web(client):
    """Preuve que le montage fonctionne : `index.html` à la racine."""
    reponse = client.get("/index.html")
    assert reponse.status_code == 200
    assert "Blocus" in reponse.text


@pytest.mark.parametrize("chemin", _CHEMINS_TRAVERSANTS)
def test_saut_de_repertoire_ne_sort_pas_de_web(client, chemin):
    """Aucun `..` (brut ou encodé) ne doit atteindre `pyproject.toml`."""
    reponse = client.get("/" + chemin)
    assert reponse.status_code != 200 or _MARQUEUR_PYPROJECT not in reponse.text


@pytest.mark.parametrize(
    "chemin",
    [
        "etc/passwd",
        "//etc/passwd",
        "%2e%2e/etc/passwd",
        "%5cetc%5cpasswd",
        "C:/Windows/win.ini",
    ],
)
def test_chemin_absolu_ne_sort_pas_de_web(client, chemin):
    """Aucun chemin absolu (ou racine Windows) ne doit lire `/etc/passwd`."""
    reponse = client.get("/" + chemin)
    assert reponse.status_code != 200 or _MARQUEUR_PASSWD not in reponse.text


def test_symlink_fichier_sortant_bloque_par_le_montage(tmp_path):
    """Un lien vers un fichier extérieur ne doit pas être suivi."""
    racine = tmp_path / "web"
    dehors = tmp_path / "hors"
    (racine / "assets").mkdir(parents=True)
    dehors.mkdir()
    (racine / "assets" / "sans.txt").write_text("interieur")
    (dehors / "secret.txt").write_text("contenu secret")

    (racine / "assets" / "fuite").symlink_to(dehors / "secret.txt")
    with TestClient(_app_static(racine)) as test_client:
        assert test_client.get("/assets/sans.txt").status_code == 200
        assert test_client.get("/assets/fuite").status_code == 404


def test_symlink_dossier_sortant_bloque_par_le_montage(tmp_path):
    """Un lien vers un dossier extérieur ne doit pas être suivi."""
    racine = tmp_path / "web"
    dehors = tmp_path / "hors"
    racine.mkdir()
    (dehors / "secret.txt").mkdir(parents=True)
    (dehors / "secret.txt" / "cible.txt").write_text("contenu secret")

    (racine / "fuite").symlink_to(dehors, target_is_directory=True)
    with TestClient(_app_static(racine)) as test_client:
        assert test_client.get("/fuite/secret.txt/cible.txt").status_code == 404


def _app_static(racine: Path) -> FastAPI:
    """Petit montage identique à `server/main.py` sur un dossier temporaire."""
    app = FastAPI()
    app.mount("/", StaticFiles(directory=str(racine)), name="web")
    return app


def test_web_ne_contient_aucun_lien_symbolique():
    """Aucun lien dans `web/` (déploiement) : rien à suivre hors de la racine."""
    fuites = [p for p in (ROOT / "web").rglob("*") if p.is_symlink()]
    assert fuites == []
