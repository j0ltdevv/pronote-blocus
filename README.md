# Pronote Blocus

Application web qui répond à un lycéen revenu après une absence : **« qu'est-ce
que j'ai à rattraper ? »**, trié par urgence, à partir des cinq onglets PRONOTE
(travail à faire, cahier de textes, informations et sondages, messagerie,
pièces jointes).

Le produit est la **PWA** (`web/` + `server/`, FastAPI) : elle s'ouvre depuis un
téléphone, s'installe sur l'écran d'accueil. La connexion se fait par QR code,
ou par identifiant + mot de passe (voir « Se connecter »). Un **bot Telegram** répond au
même moteur ; il reste disponible en option, mais les lycéens ne l'utilisent pas.

Rien n'est jamais écrit dans PRONOTE : l'application est **strictement en
lecture**.

## Démarrage (web)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
export APP_SECRET=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
export DATA_DIR=./data
uvicorn server.main:app --host 127.0.0.1 --port 8000
```

Ouvre `http://localhost:8000/`. `TELEGRAM_BOT_TOKEN` n'est **pas** nécessaire
pour l'interface web.

`APP_SECRET` chiffre les jetons PRONOTE. **Perdre cette clé = tous les comptes
doivent refaire un QR** : sauvegarde-la hors du dépôt, et garde-la stable entre
redémarrages.

## Parcours élève (web)

1. L'écran de connexion explique comment récupérer le QR (Espace Élève PRONOTE →
   bouton QR → code à 4 chiffres → capture).
2. L'élève colle la capture (ou la choisit) ; le serveur décode le QR.
3. Il saisit son code à 4 chiffres → la liste s'affiche, triée : **en retard**,
   **à venir**, **faits**.
4. Il coche ce qu'il a fait — dans Blocus uniquement, jamais dans PRONOTE.

## Bot Telegram (option)

```bash
export TELEGRAM_BOT_TOKEN=...        # @BotFather
export ADMIN_CHAT_ID=...             # ton chat_id, pour /qui et /mdp
python -m app.main
```

### Parcours du bot (trois messages)

1. `/start` → « envoie une capture d'écran du QR de PRONOTE ».
2. photo du QR → le bot demande le code à 4 chiffres choisi en générant le QR.
3. le code → « Connecté, 3B » + la liste numérotée de ses matières ; il répond
   `3 1 5` pour dire ce qui compte le plus, et reçoit tout de suite sa liste.

Le parcours identifiant / mot de passe existe en secours, activé par l'admin
avec `/mdp <chat_id>` (le mot de passe n'est jamais journalisé).

## Commandes du bot

| Commande | Effet |
|---|---|
| `/retard [page]` | ce qui est en retard, trié par urgence puis par matières prioritaires |
| `/aujourdhui` | ce qui tombe aujourd'hui et demain |
| `/matiere <texte>` | filtre par matière |
| `/cours [matière]` | les contenus de cours à rattraper |
| `/resume` | synthèse de la semaine |
| `/fait <n>` | coche l'item `n` de la dernière liste envoyée (local, invisible dans PRONOTE) |
| `/refresh` | resynchronise si la dernière synchro a plus de 10 minutes, puis renvoie la liste |
| `/priorites` | refait la liste des matières prioritaires |
| `/supprimer` | efface le compte et toutes ses données (à confirmer dans la minute) |
| `/annuler` | annule le parcours de connexion en cours |
| `/qui`, `/mdp <chat_id>` | admin seulement |

Un texte qui n'est pas une commande est une question libre : elle part au LLM
avec les seuls devoirs de l'élève (40 items max, restreints aux matières
nommées dans la question quand elle en cite 3 ou moins), 10 questions par jour.

Le quota de 10 questions ne compte que si un LLM est configuré : sans LLM,
chaque message renvoie directement la liste filtrée, sans consommation.

Le résumé quotidien part tout seul une fois par jour, après `digest_hour`
(18 h par défaut, colonne `users.digest_hour` en base) : 5 items les plus
urgents, les devoirs du jour, et la numérotation annoncée par `/fait N`.
Une donnée de plus de 12 h déclenche une relecture PRONOTE avant l'envoi.

`/retard` est paginé (`/retard 2`). `/fait <n>` vise la **dernière liste
envoyée** (retard, résumé du jour, synthèse de semaine) : la numérotation est
réécrite à chaque envoi, un numéro périmé répond « renvoie /retard ».

## Se connecter

### Par QR code (ordinateur, appareil photo)

1. Depuis l'ordinateur, ouvrez l'Espace Élève PRONOTE.
2. Cliquez sur le bouton QR → notez le code à 4 chiffres.
3. Sur le téléphone, ouvrez Blocus → collez la capture du QR.
4. Saisissez le code à 4 chiffres → la liste s'affiche.

### Par identifiant + mot de passe (sans ordinateur)

Le QR n'existe que sur la version bureau de PRONOTE : un élève sans ordinateur
passe par ce second chemin. Il faut `ALLOW_PASSWORD_LOGIN=1` (défaut).

1. Sur le téléphone, ouvrez Blocus → « Pas d'ordinateur sous la main ? ».
2. L'adresse PRONOTE et l'ENT (`PASSWORD_ENT`) sont **préremplis** : l'élève ne
   saisit que son identifiant et son mot de passe.
3. Dans ce mode, PRONOTE ne délivre aucun jeton d'appareil : le mot de passe est
   donc **conservé chiffré** (Fernet) dans `users.token_enc`, jamais en clair ni
   dans un journal. La déconnexion supprime le compte et ce mot de passe.

### Durée de la session

Une fois connecté, le jeton est **haché en base** (`web_sessions`) et posé dans
un cookie `httpOnly` : l'élève reste connecté **90 jours**, y compris après un
redémarrage du serveur. Le QR — ou les identifiants — ne servent donc qu'une
fois par trimestre. `Authorization: Bearer` reste prioritaire sur le cookie ; le
cookie ne sert que de repli quand aucun en-tête n'est présent.

## Configuration (environnement, web et bot)

| Variable | Défaut | Rôle |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | obligatoire |
| `APP_SECRET` | — | obligatoire, clé Fernet |
| `ADMIN_CHAT_ID` | vide | `/qui`, `/mdp` |
| `ALLOW_PASSWORD_LOGIN` | `1` | activer (`1`/`true`) ou désactiver (`0`/`false`) la connexion par mot de passe |
| `PASSWORD_ENT` | `lyceeconnecte_aquitaine` | ENT par défaut pour la connexion par mot de passe |
| `WINDOW_START` | `2026-10-01` | début de la fenêtre de rattrapage ; elle court jusqu'à aujourd'hui + 45 jours (`FORWARD_DAYS`) |
| `DEFAULT_PRONOTE_URL` | `https://0332722e.index-education.net/pronote/eleve.html` | si le QR ne porte pas d'URL exploitable ; **à remplacer** par ton établissement |
| `PRONOTE_MOBILE_QUERY` | `?fd=1&bydlg=A6ABB224-…&login=true` | paramètres de l'endpoint mobile appliqués à la reconnexion par jeton ; vide → constante par défaut |
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | vide | endpoint OpenAI-compatible ; **sans lui, tout fonctionne en dégradé** |
| `LLM_MESSAGES` | `1` | `0` = le corps des messages de profs ne sort jamais du serveur |
| `MAX_CONCURRENT_SYNCS` | `4` | plafond de logins PRONOTE simultanés |
| `DATA_DIR` | `data` | `app.db`, `locks/` |

## Ce que Blocus ne fait jamais

Il est **strictement en lecture** : il n'appelle ni `Homework.set_done`, ni le
marquage « lu » des informations ni celui de la messagerie. Lire ne modifie
rien chez l'élève. Le seul état écrit est local (`done_local`, priorités,
`list_ref`).

## Limites connues

- **Phase `cours` plafonnée** : 30 contenus de cours lus par synchro, 1,2 s
  entre deux (`MAX_CONTENT_READS`, `CONTENT_DELAY`). Au-delà, `/cours` reste
  partiel jusqu'à la synchro suivante : chaque lecture de contenu est une
  requête réseau, et le serveur PRONOTE n'aime pas les rafales.
- **`DATA_DIR/uploads/` n'est plus créé** : aucun fichier n'y est jamais écrit,
  `ensure_dirs()` ne crée donc plus que `DATA_DIR` lui-même et `locks/`. Un
  éventuel `uploads/` issu d'une installation précédente est inerte et peut
  être supprimé à la main.
- **Pièces jointes jamais téléchargées** : seul le nombre de fichiers d'un
  devoir est lu et affiché (📎2), aucun binaire n'est stocké.
- **Une seule instance** : le service ne prend pas de verrou global. Deux
  processus qui pointent le même `DATA_DIR` se marchent dessus sur SQLite.
- **Fenêtre de rattrapage figée** : `WINDOW_START` n'est relu qu'au démarrage ;
  changer la variable demande un `systemctl restart`.
- **`digest_hour` n'est pas exposed** : c'est une colonne SQLite, à modifier
  en base si besoin.

## Déploiement du bot (option)

Cible : un VPS ou une machine toujours allumée, derrière un NAT, sans URL
publique. Le bot tourne en long polling, un seul processus suffit.

**1. Utilisateur et code**

```bash
sudo useradd --system --home /opt/pronote-blocus --shell /usr/sbin/nologin pronote-blocus
sudo git clone <dépôt> /opt/pronote-blocus
sudo chown -R pronote-blocus:pronote-blocus /opt/pronote-blocus
cd /opt/pronote-blocus
sudo -u pronote-blocus python3 -m venv .venv
sudo -u pronote-blocus .venv/bin/pip install -e .
```

**2. Configuration**

```bash
sudo install -d -o root -g pronote-blocus -m 0750 /etc/pronote-blocus
sudo install -o root -g pronote-blocus -m 0640 \
     deploy/pronote-blocus.env.example /etc/pronote-blocus/pronote-blocus.env
sudoedit /etc/pronote-blocus/pronote-blocus.env   # token, APP_SECRET, ADMIN_CHAT_ID
```

`APP_SECRET` est la seule donnée irremplaçable : elle chiffre les jetons
PRONOTE de tous les élèves. **Sauvegarde-la hors du dépôt** (gestionnaire de
mots de passe, coffre). La perdre = tout le monde refait son QR.

`DATA_DIR` doit valoir `/var/lib/pronote-blocus` : c'est le seul chemin que
l'unité systemd autorise en écriture (`ReadWritePaths`).

```bash
sudo install -d -o pronote-blocus -g pronote-blocus -m 0700 /var/lib/pronote-blocus
```

**3. Service**

```bash
sudo cp deploy/pronote-blocus.service /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/pronote-blocus.service
sudo systemctl daemon-reload
sudo systemctl enable --now pronote-blocus
```

**4. Vérification**

```bash
systemctl status pronote-blocus            # active (running)
journalctl -u pronote-blocus -f            # « pronote-blocus démarre en long polling »
```

Puis, depuis Telegram, `/qui` en admin doit répondre avec le nombre de comptes
suivis. Un `SystemExit: Configuration incomplète` dans le journal signifie une
variable absente ou invalide dans `/etc/pronote-blocus/pronote-blocus.env`.

**5. Mise à jour**

```bash
cd /opt/pronote-blocus
sudo -u pronote-blocus git pull
sudo -u pronote-blocus .venv/bin/pip install -e .
sudo systemctl restart pronote-blocus
journalctl -u pronote-blocus -n 30 --no-pager
```

Redémarrer le service ne perd rien : l'état vit dans `DATA_DIR/app.db` et les
jetons PRONOTE sont chiffrés avec `APP_SECRET`, inchangé entre deux versions.

**Dépannage rapide**

| Symptôme | Cause probable |
|---|---|
| `Configuration incomplète` au démarrage | variable manquante dans l'env |
| `APP_SECRET invalide` | clé Fernet tronquée ou régénérée à la main |
| service redémarre en boucle (`Restart=on-failure`) | token refusé par Telegram → voir `journalctl` |
| `Read-only file system` | `DATA_DIR` ne correspond pas à `ReadWritePaths` |
| `WINDOW_START invalide` | date mal formée dans l'env : le bot refuse de démarrer |
| `Ta connexion PRONOTE a expiré` | jeton révoilé : seul un `/start` avec un QR neuf répare le compte |
| `PRONOTE nous ralentit…` | 429/403 : pause de 3 s, un seul nouvel essai, puis abandon |
| `PIN requis` | compte PRONOTE protégé par un code : le bot le demande, sinon passe par `/mdp` |
| « Je n'ai pas pu réfléchir » | LLM absent, en panne ou quota épuisé : le bot renvoie la liste brute |
| « Aucun contenu de cours » | phase `cours` encore en cours (tâche de fond) ou plafonnée à 30 lectures |

## Déploiement de l'interface web

> Le paragraphe d'introduction du haut (« Aucun serveur web… ») décrit le **bot
> seul**. L'interface web ajoute un second processus, `pronote-web`, qui sert
> l'API (`/api/*`) et la PWA (`web/`) via uvicorn sur `127.0.0.1:8000`.

### ⚠️ Ne pas exposer avant l'authentification

**Tant que le slice `ApiAuth` n'est pas fusionné et déployé, le serveur web
n'a AUCUNE authentification** : `user_id` vient du client, `logout` supprime un
compte sans preuve d'identité, et le CORS est ouvert (`allow_origins=["*"]`).
Un porteur d'URL peut donc lire et supprimer les données de n'importe qui.
**Ne publiez le domaine qu'après `ApiAuth`.** Contrôle, une fois déployé :

```bash
# Doit répondre 401/403 (non authentifié), PAS 200.
curl -s -o /dev/null -w '%{http_code}\n' https://pronote.example.org/api/items
```

### 1. Configuration

```bash
sudo install -o root -g pronote-blocus -m 0640 \
     deploy/pronote-web.env.example /etc/pronote-blocus/pronote-web.env
sudoedit /etc/pronote-blocus/pronote-web.env
```

- `APP_SECRET` doit être **identique** à celui du bot : les deux processus
  lisent la même base `/var/lib/pronote-blocus/app.db` et déchiffrent les jetons
  PRONOTE avec la même clé. Deux clés différentes = jetons illisibles.
- `WEB_ORIGIN` doit valoir **exactement** le domaine public du Caddyfile (schéma
  inclus, sans slash final, ex. `https://pronote.example.org`). Après
  `ApiAuth`, c'est la seule origine autorisée par le CORS : une valeur erronée
  fait bloquer toutes les requêtes par le navigateur.
- `DATA_DIR=/var/lib/pronote-blocus` : seul chemin inscriptible (ReadWritePaths).

### 2. Service

```bash
sudo cp deploy/pronote-web.service /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/pronote-web.service
sudo systemctl daemon-reload
sudo systemctl enable --now pronote-web
systemctl status pronote-web            # active (running)
curl -s http://127.0.0.1:8000/health    # {"status":"ok"}
```

L'unité n'écoute **que sur la boucle locale** (`--host 127.0.0.1`). Ne jamais
la passer en `0.0.0.0` : le TLS et l'accès public passent par le reverse proxy.

### 3. Reverse proxy et TLS

Caddy obtient et renouvelle le certificat TLS automatiquement (ACME),
à condition que le domaine pointe sur la machine et que 80/443 soient ouverts.

```bash
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudoedit /etc/caddy/Caddyfile           # domaine réel + email ACME
caddy validate --config /etc/caddy/Caddyfile   # « Valid configuration »
sudo systemctl reload caddy
```

Le `Caddyfile` fournit : TLS automatique, reverse proxy vers
`127.0.0.1:8000`, HSTS, `X-Content-Type-Options: nosniff`,
`Referrer-Policy`, `X-Frame-Options` et une CSP compatible PWA (le service
worker `sw.js` est autorisé par `worker-src 'self'`). La taille de corps est
limitée à 10 Mo pour l'upload du QR.

La CSP est stricte (`script-src 'self' ; style-src 'self'`, sans
`'unsafe-inline'`) pour l'app shell. **Seule exception** : `/offline.html`,
page de secours dont le style et le script sont en ligne — sa CSP est
assouplie par une directive différée dans le `Caddyfile`, sans donnée
utilisateur ni origine externe.

### 4. Pare-feu

Seuls 80 et 443 sont publics. `pronote-web` n'écoute que sur la boucle locale :
le port 8000 n'a jamais besoin d'être ouvert.

```bash
sudo ufw default deny incoming
sudo ufw allow 22/tcp        # SSH (remplacer par votre port)
sudo ufw allow 80/tcp        # ACME + redirection HTTP→HTTPS
sudo ufw allow 443/tcp       # HTTPS
sudo ufw enable
sudo ufw status verbose
```

Si le serveur est derrière un NAT (VPS cloud), restreindre de la même façon le
*security group* du fournisseur.

### 5. Sauvegarde

Deux éléments suffisent à tout restaurer : `APP_SECRET` et la base.

```bash
# Sauvegarde à chaud, cohérente (SQLite en ligne).
sudo install -d -m 0700 /var/backups/pronote-blocus
sudo sqlite3 /var/lib/pronote-blocus/app.db \
     ".backup '/var/backups/pronote-blocus/app-$(date +%F).db'"
```

- **`APP_SECRET`** : à conserver hors du serveur (gestionnaire de mots de passe,
  coffre). La perdre = chaque élève doit refaire un QR ; la fuiter = les jetons
  PRONOTE stockés sont lisibles.
- **Base** : `/var/lib/pronote-blocus/app.db` (comptes, priorités, état local).
  Copiez-la avec `.backup` (jamais `cp` : une copie à chaud peut être corrompue).

### 6. Rotation des jetons

- **Jeton PRONOTE par élève** : tourne automatiquement à chaque synchro
  (`/refresh`, relecture du digest). Un jeton volé devient inutile au prochain
  accès ; « Ta connexion PRONOTE a expiré » signifie un jeton révoilé côté
  PRONOTE → l'élève refait un QR.
- **`APP_SECRET`** : à changer en cas de fuite. Tout le magasin de jetons devient
  illisible d'un coup, ce qui **force chaque élève à rescanner son QR** :
  prévenez-les. Procédure : arrêter `pronote-web` et `pronote-blocus`, mettre à
  jour `APP_SECRET` dans les deux env, redémarrer. Les anciens jetons sont
  purgés à la reconnexion.
- **Suppression d'un compte** : l'élève fait `/supprimer` (bot) ou la PWA
  supprime ses données ; la ligne `users` et ses jetons chiffrés disparaissent.

## Développement

```bash
pip install -e ".[dev]"          # ajoute pytest et segno (QR de test)
python -m pytest tests -q        # suite hors réseau
python -m app.dev_demo           # pipeline réel sur le compte de démo public
```

`tests/fake_telegram.py` expose `FakeTelegramRequest` : il se branche sur
`build_application(cfg, conn, request=FakeTelegramRequest())` et rejoue tout le
parcours (connexion QR comprise) sans réseau ni token.
