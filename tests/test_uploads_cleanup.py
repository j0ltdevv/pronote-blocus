"""`ensure_dirs()` ne crée plus `uploads/` : ce dossier n'est jamais utilisé.

Avant, `cfg.ensure_dirs()` posait `data/`, `data/uploads/` et `data/locks/`.
Comme aucun code n'écrit dans `uploads/`, sa création a été retirée : seuls
`data/` et `locks/` doivent exister.
"""

from __future__ import annotations

from cryptography.fernet import Fernet

from app import config


def _cfg(tmp_path):
    return config.from_env(
        {
            "TELEGRAM_BOT_TOKEN": "x:y",
            "APP_SECRET": Fernet.generate_key().decode(),
            "DATA_DIR": str(tmp_path / "instance"),
        }
    )


def test_ensure_dirs_ne_cree_pas_uploads(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.ensure_dirs()
    assert not cfg.uploads_dir.exists()
    assert not (tmp_path / "instance" / "uploads").exists()


def test_ensure_dirs_cree_data_et_locks(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.ensure_dirs()
    assert cfg.data_dir.is_dir()
    assert cfg.locks_dir.is_dir()


def test_ensure_dirs_est_idempotent_sans_uploads(tmp_path):
    # Un `uploads/` hérité d'une installation précédente n'empêche pas le
    # démarrage et n'est ni supprimé ni recréé.
    cfg = _cfg(tmp_path)
    cfg.uploads_dir.mkdir(parents=True)
    (cfg.uploads_dir / "ancien.txt").write_text("oubli", encoding="utf-8")

    cfg.ensure_dirs()

    assert cfg.data_dir.is_dir()
    assert cfg.locks_dir.is_dir()
    assert (cfg.uploads_dir / "ancien.txt").read_text(encoding="utf-8") == "oubli"


def test_uploads_dir_reste_derive_de_data_dir(tmp_path):
    # `uploads_dir` existe toujours comme chemin (lectures éventuelles), il
    # n'est juste plus créé sur disque.
    cfg = _cfg(tmp_path)
    assert cfg.uploads_dir == cfg.data_dir / "uploads"
