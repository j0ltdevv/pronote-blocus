"""Chiffrement au repos du jeton PRONOTE : `app/crypto.py`.

Ce module est la frontière de sécurité du bot : si `encrypt` laisse passer du
clair, ou si `decrypt` avale silencieusement une erreur, le jeton d'accès est
perdu ou exposé. Les tests vérifient donc le contrat observable : aller-retour
exact, refus bruyant de toute clé absente ou mal formée, refus de toute donnée
corrompue, format Fernet stable et secret absent des logs.
"""

from __future__ import annotations

import base64
import logging
import uuid

import pytest

from app import crypto

# ───────────────────────────── 1. aller-retour ─────────────────────────────


def test_aller_retour_conserve_le_jeton(secret):
    blob = crypto.encrypt("jeton-secret", secret)

    assert isinstance(blob, bytes), "le chiffré doit être rangé en base tel quel"
    assert crypto.decrypt(blob, secret) == "jeton-secret"


@pytest.mark.parametrize(
    "plain",
    [
        "",
        "a",
        "motdepasse-avec-accents-éàüñ-ç",
        "ligne1\nligne2\t\ttabulation",
        "🔐" * 500,
        "x" * 5000,
    ],
    ids=["vide", "un-caractere", "accents", "multiligne", "emoji", "cinq-kilo"],
)
def test_aller_retour_preserve_toute_chaine_y_compris_vide_ou_longue(plain, secret):
    """Un mot de passe PRONOTE peut être vide ou dépasser un bloc Fernet."""
    blob = crypto.encrypt(plain, secret)

    assert crypto.decrypt(blob, secret) == plain


def test_deux_chiffrements_du_meme_texte_sont_differents(secret):
    """Deux blob distincts : un attaquant qui lit la base ne voit pas le motif."""
    premier = crypto.encrypt("jeton-secret", secret)
    second = crypto.encrypt("jeton-secret", secret)

    assert premier != second, "Fernet doit être non déterministe (IV aléatoire)"
    assert crypto.decrypt(premier, secret) == crypto.decrypt(second, secret)


def test_le_chiffre_ne_contient_pas_le_texte_en_clair(secret):
    plain = "motdepasse-tres-long-et-unique-0123456789"
    blob = crypto.encrypt(plain, secret)

    assert plain.encode() not in blob
    assert b"motdepasse" not in blob


def test_dechiffrement_accepte_les_types_acceptes_par_sqlite(secret):
    """SQLite rend un `memoryview` : le déchiffrement doit s'en accommoder."""
    blob = crypto.encrypt("jeton", secret)

    for variante in (bytearray(blob), memoryview(blob), bytes(blob)):
        assert crypto.decrypt(variante, secret) == "jeton"


# ───────────────────── 2. clé absente ou mal formée ─────────────────────


@pytest.mark.parametrize("fonction", [crypto.encrypt, crypto.decrypt])
def test_cle_absente_refusee_par_les_deux_fonctions(fonction, secret):
    """`""` est la valeur par défaut d'un APP_SECRET oublié dans l'env."""
    argument = b"jeton-chiffre" if fonction is crypto.decrypt else "jeton"

    with pytest.raises(crypto.SecretError, match="APP_SECRET vide"):
        fonction(argument, "")


@pytest.mark.parametrize(
    "mauvaise",
    [
        "pas-une-cle",
        "a" * 44,
        "clé-secrète-à-accent",
        base64.urlsafe_b64encode(b"trop-court").decode(),
        "=" * 44,
    ],
    ids=["texte", "quarante-quatre-a", "accentue", "trop-court", "signes-egal"],
)
def test_cle_malformee_refusee_avec_message_actionnable(mauvaise, secret):
    """Une clé hors format doit échouer, pas produire un chiffré illisible."""
    with pytest.raises(crypto.SecretError) as echec:
        crypto.encrypt("jeton", mauvaise)

    message = str(echec.value)
    assert "APP_SECRET" in message
    assert "Fernet.generate_key" in message, "le message doit dire comment générer la clé"


@pytest.mark.parametrize(
    "mauvaise",
    ["pas-une-cle", "a" * 44, "clé-secrète-à-accent", "=" * 44],
    ids=["texte", "quarante-quatre-a", "accentue", "signes-egal"],
)
def test_cle_malformee_refusee_autant_pour_dechiffrer(mauvaise):
    with pytest.raises(crypto.SecretError, match="APP_SECRET"):
        crypto.decrypt(b"jeton-chiffre", mauvaise)


def test_secret_error_est_un_runtime_error():
    """Les appelants attrapent `RuntimeError` : la hiérarchie doit rester stable."""
    assert issubclass(crypto.SecretError, RuntimeError)


# ─────────────────────── 3. données corrompues / autre clé ───────────────────────


@pytest.mark.parametrize(
    "blob",
    [
        b"",
        b"jeton-en-clair-corrompu",
        b"\x80",
        b"\x00" * 100,
        b"gAAAAABmY2V4YW1wbGUtdG9rZW4tdGVzdA==",
    ],
    ids=["vide", "texte-clair", "entete-seul", "zeros", "jeton-ancien"],
)
def test_donnee_corrompue_refusee_bruyamment(blob, secret):
    with pytest.raises(crypto.SecretError, match="corrompue"):
        crypto.decrypt(blob, secret)


def test_blob_tronque_d_un_chiffre_valide_est_refuse(secret):
    blob = crypto.encrypt("jeton-secret", secret)

    with pytest.raises(crypto.SecretError, match="corrompue"):
        crypto.decrypt(blob[:-5], secret)


def test_une_octet_altere_dans_le_chiffre_est_refuse(secret):
    blob = bytearray(crypto.encrypt("jeton-secret", secret))
    blob[-1] ^= 0x01

    with pytest.raises(crypto.SecretError, match="corrompue"):
        crypto.decrypt(bytes(blob), secret)


def test_octet_altere_dans_le_texte_chiffre_est_refuse(secret):
    """Le MAC doit couvrir le texte : une altération du corps ne passe pas."""
    jeton = bytearray(base64.urlsafe_b64decode(crypto.encrypt("jeton-secret", secret)))
    jeton[25] ^= 0x01  # corps chiffré : version(1) + horodatage(8) + IV(16)

    with pytest.raises(crypto.SecretError, match="corrompue"):
        crypto.decrypt(base64.urlsafe_b64encode(bytes(jeton)), secret)


def test_autre_cle_ne_dechiffre_pas(secret, other_secret):
    """Changer APP_SECRET doit être un échec franc, pas du charabia."""
    blob = crypto.encrypt("jeton-secret", secret)

    with pytest.raises(crypto.SecretError, match="APP_SECRET a chang"):
        crypto.decrypt(blob, other_secret)


def test_deux_cles_ne_donnent_pas_le_meme_chiffre(secret, other_secret):
    assert crypto.encrypt("jeton", secret) != crypto.encrypt("jeton", other_secret)


def test_chaine_illegible_passee_a_decrypt_est_refusee(secret):
    """`bytes(str)` lève TypeError : le contrat doit rester un SecretError."""
    with pytest.raises(crypto.SecretError, match="corrompue"):
        crypto.decrypt("pas-des-octets", secret)


# ─────────────────────── 4. non-régression du format ───────────────────────


def test_format_du_blob_fernet_stable(secret):
    """Le blob reste un jeton Fernet : version 0x80 + base64 url-safe."""
    blob = crypto.encrypt("jeton", secret)
    jeton = base64.urlsafe_b64decode(blob)

    assert blob.startswith(b"gAAAA"), "jeton Fernet version 0x80 encodé en base64"
    assert jeton[0:1] == b"\x80", "premier octet de la charge utile"
    assert len(blob) >= 76, "version(1) + horodatage(8) + IV(16) + charge + HMAC(32)"
    assert b"\x80" not in blob[1:-32], "le corps ne doit contenir que du base64"


def test_longueur_de_la_cle_valide_est_acceptee(secret):
    """44 caractères base64 url-safe : le format documenté doit rester valable."""
    assert len(secret) == 44

    assert crypto.decrypt(crypto.encrypt("jeton", secret), secret) == "jeton"


# ─────────────────────── 5. le secret ne fuite pas ───────────────────────


def test_echec_de_chiffrement_ne_logue_ni_cle_ni_texte(caplog, secret, other_secret):
    """Aucun log, et surtout pas de trace du secret ou du jeton en clair."""
    blob = crypto.encrypt("jeton-secret-a-ne-pas-fuir", secret)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(crypto.SecretError) as echec:
            crypto.decrypt(blob, other_secret)
        with pytest.raises(crypto.SecretError) as echec_vide:
            crypto.encrypt("jeton-secret-a-ne-pas-fuir", "pas-une-cle")

    assert [r for r in caplog.records if r.name.startswith("app.crypto")] == []
    assert "jeton-secret-a-ne-pas-fuir" not in caplog.text
    assert secret not in caplog.text
    for message in (str(echec.value), str(echec_vide.value)):
        assert "jeton-secret-a-ne-pas-fuir" not in message
        assert secret not in message
        assert other_secret not in message


def test_aller_retour_n_emmit_aucun_log(caplog, secret):
    with caplog.at_level(logging.DEBUG):
        assert crypto.decrypt(crypto.encrypt("jeton", secret), secret) == "jeton"

    assert caplog.records == [], "le chemin nominal ne doit rien logger"


# ─────────────────────── 6. identifiant d'appareil ───────────────────────


def test_new_app_uuid_est_un_uuid4_valide():
    identifiant = crypto.new_app_uuid()

    assert isinstance(identifiant, str)
    assert uuid.UUID(identifiant).version == 4


def test_new_app_uuid_varie_dun_appel_a_l_autre():
    """Un seul appel par compte : deux appels doivent rendre deux valeurs."""
    assert crypto.new_app_uuid() != crypto.new_app_uuid()