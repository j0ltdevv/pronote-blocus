"""Validation anti-SSRF de l'URL PRONOTE (`auth.validate_pronote_url`).

Un QR est une entrée non fiable, et l'URL qu'il contient est requêtée DEPUIS
le serveur : métadonnées cloud, loopback, IPv6 locales, formes d'IP déguisées
et HTTP en clair doivent tous être refusés. Un test par vecteur de l'audit, et
la non-régression sur une URL PRONOTE légitime.

Le résolveur est doublé (`auth._resolved_addresses`) : aucun test ne sort du
réseau, et les vecteurs « rebinding » peuvent mentir à volonté.
"""

from __future__ import annotations

import json
import socket

import pytest

from app.config import DEFAULT_PRONOTE_URL
from app.pronote import auth

PUBLIQUE = "93.184.216.34"


def _qr(url: str) -> str:
    """Texte de QR minimal, forme de `parse_qr_text`."""
    return json.dumps({"login": "demonstration", "jeton": "0123456789", "url": url})


class _Resolver:
    """Double de résolveur : note les noms demandés, rend les adresses voulues."""

    def __init__(self, *adresses: str) -> None:
        self.adresses = list(adresses) or [PUBLIQUE]
        self.demandes: list[tuple[str, int]] = []

    def __call__(self, host: str, port: int) -> list[str]:
        self.demandes.append((host, port))
        return list(self.adresses)


@pytest.fixture
def resolveur(monkeypatch):
    """`resolveur("1.2.3.4")` → installé comme résolveur et renvoyé."""

    def _installer(*adresses: str) -> _Resolver:
        double = _Resolver(*adresses)
        monkeypatch.setattr(auth, "_resolved_addresses", double)
        return double

    return _installer


@pytest.fixture
def resolveur_injoignable(monkeypatch):
    """Résolveur qui échoue, comme hors réseau."""

    def _echec(host: str, port: int):
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(auth, "_resolved_addresses", _echec)


# ─────────────── vecteur 1 : métadonnées cloud (169.254.169.254) ───────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://169.254.169.254/latest/meta-data/",
        "https://169.254.169.254/pronote/eleve.html",
        "https://[::ffff:169.254.169.254]/latest/meta-data/",
    ],
)
def test_vecteur_1_metadonnees_cloud_refusees(url, resolveur):
    double = resolveur()

    with pytest.raises(auth.QrNotPronote):
        auth.parse_qr_text(_qr(url))

    assert double.demandes == []


# ─────────────── vecteur 2 : loopback et 0.0.0.0 ───────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.2/pronote/eleve.html",
        "https://127.0.0.1/pronote/eleve.html",
        "https://127.1/pronote/eleve.html",
        "https://0.0.0.0/pronote/eleve.html",
        "https://[::1]/pronote/eleve.html",
    ],
)
def test_vecteur_2_loopback_et_zero_refuses(url, resolveur):
    double = resolveur()

    with pytest.raises(auth.QrNotPronote):
        auth.parse_qr_text(_qr(url))

    assert double.demandes == []


# ─────────────── vecteur 3 : IPv6 locales et IPv4-mappées ───────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://[fe80::1]/pronote/eleve.html",
        "https://[fc00::1]/pronote/eleve.html",
        "https://[fd12:3456::1]/pronote/eleve.html",
        "https://[::ffff:127.0.0.1]/pronote/eleve.html",
        "https://[::ffff:10.0.0.5]/pronote/eleve.html",
    ],
)
def test_vecteur_3_ipv6_locales_refusees(url, resolveur):
    double = resolveur()

    with pytest.raises(auth.QrNotPronote):
        auth.parse_qr_text(_qr(url))

    assert double.demandes == []


# ─────────────── vecteur 4 : entier, octal, hexadécimal ───────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://2130706433/pronote/eleve.html",
        "https://0177.0.0.1/pronote/eleve.html",
        "https://0x7f000001/pronote/eleve.html",
        "https://0x7f.0.0.1/pronote/eleve.html",
        "https://0xdeadbeef/pronote/eleve.html",
    ],
)
def test_vecteur_4_ip_deguisees_refusees_sans_dns(url, resolveur):
    """Formes non canoniques : refusées AVANT toute résolution."""
    double = resolveur()

    with pytest.raises(auth.QrNotPronote):
        auth.parse_qr_text(_qr(url))

    assert double.demandes == []


# ─────────────── vecteur 5 : rebinding DNS ───────────────


def test_vecteur_5_rebinding_vers_loopback_refuse(resolveur):
    """Le nom est légitime, la réponse du DNS ne l'est pas."""
    double = resolveur("127.0.0.1")

    with pytest.raises(auth.QrNotPronote):
        auth.parse_qr_text(_qr("https://rebind.index-education.net/pronote/eleve.html"))

    assert double.demandes == [("rebind.index-education.net", 443)]


@pytest.mark.parametrize(
    "adresses",
    [
        ("93.184.216.34", "169.254.169.254"),
        ("93.184.216.34", "10.0.0.5"),
        ("93.184.216.34", "fc00::1"),
    ],
)
def test_vecteur_5_reponse_mixte_refusee(resolveur, adresses):
    """UNE adresse non publique suffit à refuser : `requests` peut choisir l'autre."""
    resolveur(*adresses)

    with pytest.raises(auth.QrNotPronote):
        auth.parse_qr_text(_qr("https://demo.index-education.net/pronote/eleve.html"))


@pytest.mark.parametrize(
    "url",
    [
        "https://10.0.0.5/pronote/eleve.html",
        "https://192.168.1.10/pronote/eleve.html",
        "https://172.16.0.5/pronote/eleve.html",
        "https://100.64.0.1/pronote/eleve.html",
    ],
)
def test_vecteur_5_plages_privees_refusees(url, resolveur):
    double = resolveur()

    with pytest.raises(auth.QrNotPronote):
        auth.parse_qr_text(_qr(url))

    assert double.demandes == []


def test_vecteur_5_nom_injoignable_refuse(resolveur_injoignable):
    with pytest.raises(auth.QrNotPronote):
        auth.parse_qr_text(_qr("https://inconnu.index-education.net/pronote/eleve.html"))


def test_vecteur_5_resolution_vide_refusee(resolveur):
    double = resolveur()
    double.adresses = []

    with pytest.raises(auth.QrNotPronote):
        auth.parse_qr_text(_qr("https://vide.index-education.net/pronote/eleve.html"))


# ─────────────── vecteur 6 : HTTP en clair ───────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://ETABLISSEMENT.index-education.net/pronote/eleve.html",
        "http://demo.index-education.net:8443/pronote/eleve.html",
        "http://127.0.0.1/pronote/eleve.html",
    ],
)
def test_vecteur_6_http_refuse_sans_dns(url, resolveur):
    """En clair, le jeton mobile circulerait en clair : HTTPS obligatoire."""
    double = resolveur()

    with pytest.raises(auth.QrNotPronote):
        auth.parse_qr_text(_qr(url))

    assert double.demandes == []


# ─────────────── formes détournées d'URL ───────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://user:motdepasse@demo.index-education.net/pronote/eleve.html",
        "https://demo.index-education.net@127.0.0.1/pronote/eleve.html",
        "https://:8443/pronote/eleve.html",
        "https:///pronote/eleve.html",
        "https://demo.index-education.net:99999/pronote/eleve.html",
        "https://demo.index-education.net:abc/pronote/eleve.html",
        "https://[fe80::1/pronote/eleve.html",
        "ftp://demo.index-education.net/pronote/eleve.html",
        "file:///pronote/eleve.html",
    ],
)
def test_url_detournee_refusee_sans_dns(url, resolveur):
    double = resolveur()

    with pytest.raises(auth.QrNotPronote):
        auth.validate_pronote_url(url)

    assert double.demandes == []


# ─────────────── non-régression : URL PRONOTE légitimes ───────────────


def test_non_regression_sous_domaine_et_chemin_conserves(resolveur):
    double = resolveur(PUBLIQUE, "2606:2800:220:1:248:1893:25c8:1946")

    assert auth.parse_qr_text(_qr(DEFAULT_PRONOTE_URL)) == {
        "login": "demonstration",
        "jeton": "0123456789",
        "url": DEFAULT_PRONOTE_URL,
    }
    assert double.demandes == [("etablissement.index-education.net", 443)]


def test_non_regression_port_personnalise_resolu_et_conserve(resolveur):
    double = resolveur(PUBLIQUE)
    url = "https://ETABLISSEMENT.index-education.net:8443/pronote/parent.html"

    assert auth.validate_pronote_url(url) == url
    assert double.demandes == [("etablissement.index-education.net", 8443)]
    assert auth.mobile_url(url).startswith(
        "https://ETABLISSEMENT.index-education.net:8443/pronote/mobile.parent.html"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://etab.index-education.net/pronote/eleve.html",
        "https://demo.index-education.net/pronote/eleve.html",
        "https://ETABLISSEMENT.index-education.net/pronote/vie-scolaire.html#ancre",
        "https://93.184.216.34/pronote/eleve.html",
        "https://[2606:2800:220:1:248:1893:25c8:1946]/pronote/eleve.html",
    ],
)
def test_non_regression_url_publiques_acceptees(url, resolveur):
    resolveur(PUBLIQUE)

    assert auth.validate_pronote_url(url) == url


def test_non_regression_ip_litterale_publique_sans_dns(resolveur):
    """Une IP publique canonique n'a pas besoin du résolveur."""
    double = resolveur()

    assert auth.validate_pronote_url("https://93.184.216.34/pronote/eleve.html")
    assert double.demandes == []
