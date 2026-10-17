# Pronote Blocus

Bot Telegram qui répond à un lycéen revenu après une absence : **« qu'est-ce que
j'ai à rattraper ? »**, trié par urgence, à partir des cinq onglets PRONOTE
(travail à faire, cahier de textes, informations et sondages, messagerie,
pièces jointes).

Aucun serveur web : `python-telegram-bot` en **long polling**. Pas d'URL
publique, pas de HTTPS, pas de domaine, ça tourne derrière un NAT.

## Démarrage

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .
export TELEGRAM_BOT_TOKEN=...        # @BotFather
export APP_SECRET=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
export ADMIN_CHAT_ID=...             # ton chat_id, pour /qui et /mdp
python -m app.main
```

`APP_SECRET` chiffre les jetons PRONOTE. **Perdre cette clé = tous les comptes
doivent refaire un QR** : sauvegarde-la hors du dépôt, et garde-la stable entre
redémarrages.

## Parcours élève (trois messages)

1. `/start` → « envoie une capture d'écran du QR de PRONOTE ».
2. photo du QR → le bot demande le code à 4 chiffres choisi en générant le QR.
3. le code → « Connecté, 3B » + la liste numérotée de ses matières ; il répond
   `3 1 5` pour dire ce qui compte le plus, et reçoit tout de suite sa liste.

Le parcours identifiant / mot de passe existe en secours, activé par l'admin
avec `/mdp <chat_id>` (le mot de passe n'est jamais journalisé).

## Commandes

| Commande | Effet |
|---|---|
| `/retard [page]` | ce qui est en retard, trié par urgence puis par matières prioritaires |
| `/aujourdhui` | ce qui tombe aujourd'hui et demain |
| `/matiere <texte>` | filtre par matière |
| `/cours [matière]` | les contenus de cours à rattraper |
| `/resume` | synthèse de la semaine |
| `/fait <n>` | coche l'item `n` de la dernière liste |
| `/refresh` | resynchronise (au plus toutes les 10 minutes) |
| `/priorites` | refait la liste des matières prioritaires |
| `/supprimer` | efface le compte et toutes ses données (deux fois de suite) |
| `/annuler` | annule le parcours de connexion en cours |
| `/qui`, `/mdp <chat_id>` | admin seulement |

Un texte qui n'est pas une commande est une question libre, répondue
uniquement à partir des devoirs de l'élève, 10 questions par jour.

## Réglages (environnement)

| Variable | Défaut | Rôle |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | obligatoire |
| `APP_SECRET` | — | obligatoire, clé Fernet |
| `ADMIN_CHAT_ID` | vide | `/qui`, `/mdp` |
| `WINDOW_START` | `2026-10-01` | début de la fenêtre de rattrapage |
| `DEFAULT_PRONOTE_URL` | établissement | si le QR ne porte pas d'URL exploitable |
| `PRONOTE_MOBILE_QUERY` | `?fd=1&bydlg=A6ABB224-…&login=true` | paramètres de l'endpoint mobile |
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | vide | endpoint OpenAI-compatible ; **sans lui, tout fonctionne en dégradé** |
| `LLM_MESSAGES` | `1` | `0` = le corps des messages de profs ne sort jamais du serveur |
| `MAX_CONCURRENT_SYNCS` | `4` | plafond de logins PRONOTE simultanés |
| `DATA_DIR` | `data` | `app.db`, `uploads/`, `locks/` |

## Ce que le bot ne fait jamais

Il est **strictement en lecture** : il n'appelle ni `Homework.set_done`, ni le
marquage « lu » des informations ni celui de la messagerie. Lire ne modifie
rien chez l'élève. Le seul état écrit est local (`done_local`, priorités).

## Développement

```bash
python -m pytest tests -q          # suite hors réseau
python -m app.dev_demo             # pipeline réel sur le compte de démo public
```

`tests/fake_telegram.py` branche un transport Telegram simulé :
`build_application(cfg, conn, request=FakeRequest())` rejoue tout le parcours
sans réseau ni token.
