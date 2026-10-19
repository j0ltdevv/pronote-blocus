"""Tests des en-têtes de sécurité HTTP (CSP, nosniff, référent, cadre).

Les assets de l'app shell et de la page hors ligne sont lus depuis `web/` :
chaque directive de la CSP doit couvrir ce qui est réellement servi.
"""

from __future__ import annotations

import importlib
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from server import deps
from server.security import (
    CONTENT_SECURITY_POLICY,
    PERMISSIONS_POLICY,
    SECURITY_HEADERS,
)

WEB_DIR = Path(__file__).resolve().parents[1] / "web"


def _directives(csp: str = CONTENT_SECURITY_POLICY) -> dict[str, list[str]]:
    """Découpe une CSP en `{directive: [sources]}`."""
    parsed: dict[str, list[str]] = {}
    for part in csp.split(";"):
        bits = part.split()
        if bits:
            parsed[bits[0]] = bits[1:]
    return parsed


class _Assets(HTMLParser):
    """Relève les ressources externes et les éventuels script/style en ligne."""

    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[str] = []
        self.styles: list[str] = []
        self.fonts: list[str] = []
        self.images: list[str] = []
        self.manifests: list[str] = []
        self.inline_scripts = 0
        self.inline_styles = 0
        self.inline_handlers = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        if any(name.startswith("on") for name in values):
            self.inline_handlers += 1
        if "style" in values:
            self.inline_styles += 1

        if tag == "script":
            if values.get("src"):
                self.scripts.append(values["src"])
            else:
                self.inline_scripts += 1
        elif tag == "link":
            rel = values.get("rel", "")
            if rel == "stylesheet" and values.get("href"):
                self.styles.append(values["href"])
            elif rel == "preload" and values.get("as") == "font" and values.get("href"):
                self.fonts.append(values["href"])
            elif rel == "manifest" and values.get("href"):
                self.manifests.append(values["href"])
        elif tag == "img" and values.get("src"):
            self.images.append(values["src"])


def _assets(page: str) -> _Assets:
    parser = _Assets()
    parser.feed((WEB_DIR / page).read_text(encoding="utf-8"))
    return parser


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Serveur isolé, sans base de production ni appel sortant."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_SECRET", Fernet.generate_key().decode())
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    for variable in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(deps, "_conn", None)
    monkeypatch.setattr(deps, "_cfg", None)
    monkeypatch.setattr(deps, "_MEMORY", {})
    monkeypatch.setattr(deps, "_SESSIONS", {})

    main = importlib.import_module("server.main")
    with TestClient(main.app) as test_client:
        yield test_client

    conn = deps._conn
    if conn is not None:
        conn.close()


# ── présence des en-têtes ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/index.html",
        "/app.js",
        "/styles.css",
        "/offline.html",
        "/offline.css",
        "/offline.js",
        "/manifest.json",
        "/sw.js",
        "/fonts/bricolage.woff2",
        "/icons/icon.svg",
        "/health",
    ],
)
def test_chaque_reponse_porte_les_en_tetes(client, path):
    """Y compris les fichiers statiques et la page hors ligne."""
    reponse = client.get(path)
    assert reponse.status_code == 200
    for name, value in SECURITY_HEADERS.items():
        assert reponse.headers[name] == value


def test_les_erreurs_portent_aussi_les_en_tetes(client):
    """Un 401 ou un 404 ne doit pas être une porte dérobée sans CSP."""
    for reponse in (client.get("/api/items"), client.get("/introuvable")):
        assert reponse.status_code in (401, 404)
        for name, value in SECURITY_HEADERS.items():
            assert reponse.headers[name] == value


def test_cors_reste_compatible(client):
    """Le middleware de sécurité n'efface pas les en-têtes CORS."""
    reponse = client.options(
        "/api/items",
        headers={
            "Origin": "http://localhost:8000",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert reponse.status_code == 200
    assert reponse.headers["access-control-allow-origin"] == "http://localhost:8000"
    assert reponse.headers["content-security-policy"] == CONTENT_SECURITY_POLICY


# ── directives de la CSP ───────────────────────────────────────────────


def test_directives_par_defaut_et_scripts():
    directives = _directives()
    assert directives["default-src"] == ["'self'"]
    assert directives["script-src"] == ["'self'"]
    assert directives["style-src"] == ["'self'"]
    assert directives["object-src"] == ["'none'"]
    assert directives["base-uri"] == ["'self'"]
    assert directives["form-action"] == ["'self'"]
    assert directives["frame-ancestors"] == ["'none'"]
    assert directives["frame-src"] == ["'none'"]


def test_pas_de_contournement_inline_ou_eval():
    """Aucune directive ne doit rouvrir inline/eval, qui annulerait la CSP."""
    sources = {source for values in _directives().values() for source in values}
    assert "'unsafe-inline'" not in sources
    assert "'unsafe-eval'" not in sources
    assert "data:" not in _directives()["script-src"]
    assert "blob:" not in _directives()["script-src"]


def test_permissions_policy_minimale():
    assert PERMISSIONS_POLICY == (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    )


# ── les assets servis sont autorisés ───────────────────────────────────


@pytest.mark.parametrize("page", ["index.html", "offline.html"])
def test_pages_sans_script_ni_style_en_ligne(page):
    """La CSP stricte casserait une page qui embarque du code en ligne."""
    assets = _assets(page)
    assert assets.inline_scripts == 0
    assert assets.inline_styles == 0
    assert assets.inline_handlers == 0
    assert assets.scripts, f"{page} doit charger app.js ou offline.js"


@pytest.mark.parametrize("page", ["index.html", "offline.html"])
def test_assets_references_sont_servis_et_autorises(client, page):
    """Chaque ressource de la page répond 200 et tombe dans une directive."""
    assets = _assets(page)
    directives = _directives()

    for path, directive in (
        *((src, "script-src") for src in assets.scripts),
        *((href, "style-src") for href in assets.styles),
        *((href, "font-src") for href in assets.fonts),
        *((src, "img-src") for src in assets.images),
        *((href, "manifest-src") for href in assets.manifests),
    ):
        assert path.startswith("/"), f"ressource externe interdite : {path}"
        reponse = client.get(path)
        assert reponse.status_code == 200, path
        # Même origine : `'self'` couvre le chemin relatif.
        assert "'self'" in directives[directive], (path, directive)


def test_apercu_qr_blob_est_autorise():
    """`app.js` crée un `blob:` pour l'aperçu : `img-src` doit l'accepter."""
    directives = _directives()
    assert "'self'" in directives["worker-src"]
    assert "blob:" in directives["img-src"]

    script = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    assert "URL.createObjectURL" in script


@pytest.mark.parametrize(
    ("path", "attendu"),
    [
        ("/app.js", "text/javascript"),
        ("/offline.js", "text/javascript"),
        ("/sw.js", "text/javascript"),
        ("/styles.css", "text/css"),
        ("/offline.css", "text/css"),
    ],
)
def test_types_mimes_compatibles_avec_nosniff(client, path, attendu):
    """Avec `nosniff`, un type MIME erroné ferait bloquer script, worker ou CSS."""
    content_type = client.get(path).headers["content-type"].split(";")[0]
    assert content_type == attendu


@pytest.mark.parametrize("feuille", ["styles.css", "offline.css"])
def test_css_ne_charge_que_l_origine(feuille):
    """Aucune police ou image distante : `font-src`/`img-src` restent locaux."""
    css = (WEB_DIR / feuille).read_text(encoding="utf-8")
    urls = re.findall(r"""url\(\s*['"]?([^'")]+)""", css)
    assert urls
    for url in urls:
        assert url.startswith("/"), f"URL externe dans {feuille} : {url}"
