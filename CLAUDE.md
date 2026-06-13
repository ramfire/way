# CLAUDE.md — AlfaWay Environment (AS-BUILT)

> **État : déployé le 2026-06-06.** Ce document reflète l'installation réelle sur le VPS,
> qui dévie du runbook initial à cause de conflits avec les projets existants (TAP, TrustLink)
> et de l'OS réel (Ubuntu 25.10, Python 3.13). Voir « Déviations » plus bas.
>
> Statut : étapes 1–12 **terminées et vérifiées**, **SSL/HTTPS inclus** (Let's Encrypt actif).

## Contexte VPS
- OS : **Ubuntu 25.10** (le runbook disait 25.04)
- Provider : OVH VPS-1 (4 vCores, 8GB RAM, 75GB SSD)
- IP publique : `141.227.152.10`
- Projets coexistants sur ce VPS (⚠️ contraintes de ports) :
  - TAP        → `/var/www/tap/`      (tap.algotech.lu)   — gunicorn sur **127.0.0.1:8000**
  - TrustLink  → `/var/www/amlkyc/`   (amlkyc.algotech.lu) — occupe 8001/8002
  - AlfaWay    → `/opt/alfaway/`       (alfaway.algotech.lu) — gunicorn sur **127.0.0.1:8010**

## Allocation des ports (RÉELLE)
| Port | Service | Bind |
|------|---------|------|
| **2222** | SSH admin (système) | 0.0.0.0 — *déplacé depuis 22* |
| **22**   | SFTPGo (SFTP fund admins) | 0.0.0.0 |
| **8090** | SFTPGo admin / REST API | 127.0.0.1 |
| **8010** | AlfaWay Gunicorn | 127.0.0.1 (8000 pris par TAP) |
| **80**   | Nginx (alfaway.algotech.lu) | 0.0.0.0 |

> ⚠️ **SSH se connecte désormais sur le port 2222** : `ssh -p 2222 ubuntu@141.227.152.10`.

## Stack technique (RÉELLE)
- **Python 3.13.7** (le runbook visait 3.11, indisponible sur Ubuntu 25.10)
- **Django 5.2** (le runbook visait 5.0, qui ne supporte pas Python 3.13)
- SFTPGo **2.7.3** (installé via `.deb` GitHub, pas l'apt repo)
- Nginx + Gunicorn
- SQLite (provisoire → PostgreSQL OVH en production)
- Scaleway Object Storage (S3-compatible, `fr-par`, bucket dev `alfaway-dev`)

---

## Étape 1 — Structure répertoires
```bash
sudo mkdir -p /opt/alfaway && sudo chown $USER:$USER /opt/alfaway
mkdir -p /opt/alfaway/{static,media,logs}      # NB: pas de sous-dossier 'alfaway' ici
sudo mkdir -p /var/log/alfaway && sudo chown $USER:$USER /var/log/alfaway
```
> Le dossier `alfaway/` est créé par `startproject` (étape 3) ; ne pas le pré-créer (collision).

## Étape 2 — Python virtualenv + dépendances
```bash
cd /opt/alfaway
python3 -m venv venv         # Python 3.13 système
source venv/bin/activate
pip install --upgrade pip
pip install "django>=5.1,<5.3" gunicorn python-decouple dj-database-url \
            boto3 psycopg2-binary djangorestframework django-cors-headers
```
> `dj-database-url` ajouté (le runbook l'oubliait) pour parser `DATABASE_URL`.

## Étape 3 — Projet Django
```bash
cd /opt/alfaway && source venv/bin/activate
django-admin startproject alfaway .
python manage.py startapp core
python manage.py startapp api
```

## Étape 4 — Fichier `.env`
`/opt/alfaway/.env` — secrets générés (`get_random_secret_key`, `secrets.token_urlsafe`).
`ALLOWED_HOSTS` inclut `alfaway.algotech.lu,localhost,127.0.0.1`. Permissions :
```bash
sudo chown ubuntu:www-data /opt/alfaway/.env && sudo chmod 640 /opt/alfaway/.env
```
> 640 + groupe `www-data` (et pas 600) : Gunicorn tourne en `www-data` et `python-decouple`
> lit le **fichier** `.env`, donc le service doit pouvoir le lire.

`settings.py` est câblé sur `decouple.config()` + `dj_database_url`, ajoute `rest_framework`,
`corsheaders`, `core`, `api`, et définit `STATIC_ROOT=/opt/alfaway/static`,
`MEDIA_ROOT=/opt/alfaway/media`, logging vers `/var/log/alfaway/django.log`, et les
constantes `SCW_*`, `SFTPGO_WEBHOOK_TOKEN`, `INTERNAL_TOKEN`.

## Étape 5 — Installation SFTPGo (via .deb, PAS l'apt repo)
> L'apt repo `ftp.osuosl.org/pub/sftpgo/apt stable` n'a pas de fichier `Release` → inutilisable.
```bash
curl -fsSL -o /tmp/sftpgo.deb \
  https://github.com/drakkan/sftpgo/releases/download/v2.7.3/sftpgo_2.7.3-1_amd64.deb
sudo apt-get install -y /tmp/sftpgo.deb
```
> Le paquet crée l'utilisateur `sftpgo`, le provider SQLite, **et son propre unit systemd**
> (`/usr/lib/systemd/system/sftpgo.service`, durci, avec `CAP_NET_BIND_SERVICE` requis pour
> binder le port 22). On **garde ce unit** au lieu de celui du runbook.

## Étape 6 — Configuration SFTPGo
Éditer `/etc/sftpgo/sftpgo.json` (édition chirurgicale, on garde les défauts du paquet) :
- `sftpd.bindings` → port **22**
- `httpd.bindings[0]` → port **8090**, address **127.0.0.1**
- `common.upload_mode` → **1** (atomique)
- `common.actions` → `execute_on: ["upload"]`, hook :
  `http://127.0.0.1:8010/api/internal/sftp-webhook/?token=<SFTPGO_WEBHOOK_TOKEN>`
- `data_provider.create_default_admin` → **true** (pour bootstrap admin, étape 11)

> ⚠️ **Le token passe en query param `?token=`**, pas en header. Les hooks HTTP de SFTPGo
> n'envoient **aucun header custom** ; le check `X-Webhook-Token` du runbook renverrait 403.
> Hôte SFTP : clés générées automatiquement dans `/etc/sftpgo/`.

## Étape 7 — Service SFTPGo
On utilise le unit fourni par le paquet (déjà `enabled`). Bootstrap admin :
```bash
# /etc/sftpgo/sftpgo.env (chown sftpgo:sftpgo, chmod 600)
SFTPGO_DEFAULT_ADMIN_USERNAME=admin
SFTPGO_DEFAULT_ADMIN_PASSWORD=<généré>
sudo systemctl restart sftpgo
```

## Étape 8 — Service Django (Gunicorn) — port 8010
`/etc/systemd/system/alfaway.service` : `User/Group=www-data`,
`WorkingDirectory=/opt/alfaway`, `EnvironmentFile=/opt/alfaway/.env`,
`ExecStart=/opt/alfaway/venv/bin/gunicorn --workers 3 --bind 127.0.0.1:8010 \
  --access-logfile /var/log/alfaway/gunicorn-access.log \
  --error-logfile /var/log/alfaway/gunicorn-error.log alfaway.wsgi:application`.
```bash
sudo systemctl daemon-reload && sudo systemctl enable --now alfaway
```
> Permissions runtime requises : `db.sqlite3` appartient à `www-data`, `/opt/alfaway` est
> groupe `www-data` + g+w (SQLite écrit le WAL/journal dans le dossier parent),
> `media/` et `/var/log/alfaway/` writable par `www-data`.
> ⚠️ **HOME dédié (gunicorn 26)** : le unit définit `StateDirectory=alfaway` +
> `Environment=HOME=/var/lib/alfaway`. Gunicorn 26 écrit son control-socket dans
> `$HOME/.gunicorn` ; le HOME par défaut de `www-data` (`/var/www`) n'est pas writable →
> sinon `[ERROR] Control server error: Permission denied: '/var/www/.gunicorn'` à chaque
> démarrage. `/var/lib/alfaway` est créé/possédé par systemd (www-data).

## Étape 9 — Nginx vhost → 8010
`/etc/nginx/sites-available/alfaway` : `proxy_pass http://127.0.0.1:8010;`,
`location /static/ { alias /opt/alfaway/static/; }`, idem `/media/`,
`client_max_body_size 100M`, headers `X-Forwarded-*`.
```bash
sudo ln -sfn /etc/nginx/sites-available/alfaway /etc/nginx/sites-enabled/alfaway
sudo nginx -t && sudo systemctl reload nginx
```

### SSL — ✅ ACTIF (Let's Encrypt)
Enregistrement A `alfaway.algotech.lu → 141.227.152.10` créé (zone dns.lu), puis :
```bash
sudo certbot --nginx -d alfaway.algotech.lu \
  --non-interactive --agree-tos -m pascal.pierre@gmail.com --redirect
```
- Certificat : `/etc/letsencrypt/live/alfaway.algotech.lu/` — valide jusqu'au **2026-09-04**.
- Redirection HTTP→HTTPS (301) active ; nginx écoute sur **:443**.
- Renouvellement auto via `certbot.timer` (`certbot renew --dry-run` OK).

## Étape 10 — Test S3 Scaleway ✅
`/opt/alfaway/test_s3.py` lit les clés depuis `.env` (jamais en dur), crée `alfaway-dev`
(avec `CreateBucketConfiguration LocationConstraint=fr-par`), upload + list. OK.

## Étape 11 — Test SFTP → S3 ✅
User `sftp_test` créé via REST API (`POST /api/v2/users` avec token admin), backend S3
(`provider:1`, `key_prefix:"test/"`, `access_secret` en objet `{status:Plain,payload:...}`).
Upload SFTP vérifié → `s3://alfaway-dev/test/test_alfaway.csv`, webhook Django → 200.

## Étape 12 — Webhook Django ✅
`api/views.py::SFTPWebhookView` (DRF, `authentication_classes=[]`) accepte le token via
`?token=` **ou** header `X-Webhook-Token`, log l'upload, TODO : déclencher DORA checks.
Route : `path("api/internal/sftp-webhook/", SFTPWebhookView.as_view())`.

## Étape 13 — Réconciliation S3 ↔ base (filet de sécurité) ✅
`api/management/commands/reconcile_files.py` aligne la table `api.ReceivedFile` (journal)
sur la vérité du bucket S3, pour rattraper les divergences que les hooks ne couvrent pas
(webhook perdu si Gunicorn down/en erreur, delete hors-bande, etc.) :
- **backfill** : objet S3 sans ligne `stored` → crée une ligne `stored` `reconciled=True` ;
- **promote** : ligne coincée en `receiving` alors que l'objet existe → `stored` ;
- **missing** : ligne `stored` dont l'objet a disparu → `missing`.
Idempotent ; `--dry-run` n'écrit rien. Clé S3 réelle = chemin **physique** (`path`).
```bash
sudo -u www-data /opt/alfaway/venv/bin/python manage.py reconcile_files --dry-run
```
Planifié via **`alfaway-reconcile.timer`** (systemd, `enabled`) → `reconcile_files` chaque
jour à **03:30 UTC** (`Persistent=true` : rattrape un run manqué). Le service oneshot
`alfaway-reconcile.service` réutilise `EnvironmentFile=/opt/alfaway/.env` + le même HOME que
gunicorn. C'est un **filet**, pas une excuse pour sauter le restart post-migration (voir
« Déploiement » ci-dessous).

## Déploiement / mise à jour du code ⚠️
**Après toute migration ou changement de code Python, redémarrer Gunicorn :**
```bash
sudo -u www-data /opt/alfaway/venv/bin/python manage.py migrate   # si migrations
sudo systemctl restart alfaway
```
> **Pourquoi (incident 2026-06-13)** : une migration ajoutant `reconciled NOT NULL` (0003) a
> été *appliquée à la base* sans redémarrer Gunicorn. Les workers tournaient encore avec
> l'ancien modèle (sans le champ) → chaque insert envoyait `NULL` → `IntegrityError`, ligne
> jamais écrite. Le webhook renvoyant **toujours 200** (try/except volontaire pour ne pas
> bloquer les uploads SFTP), l'échec était **silencieux** : uploads S3 OK mais **rien dans le
> monitoring**. Diagnostic : `IntegrityError` visible dans `/var/log/alfaway/django.log`.
> Résolu par `systemctl restart alfaway` + backfill via `reconcile_files`.
Aussi : `path("healthz/", ...)` pour les health checks.

---

## Validation
```bash
for s in alfaway sftpgo nginx ssh.socket alfaway-reconcile.timer; do systemctl is-active $s; done
curl http://127.0.0.1:8010/healthz/         # {"status":"ok"}
journalctl -fu alfaway ; journalctl -fu sftpgo
```

## Déviations vs runbook initial (résumé)
1. Gunicorn **8010** (pas 8000 — pris par TAP).
2. **Python 3.13 / Django 5.2** (3.11/5.0 incompatibles avec Ubuntu 25.10).
3. SFTPGo via **`.deb` GitHub** (apt repo cassé) ; on garde le **unit systemd du paquet**.
4. Webhook : token en **query param** (les hooks SFTPGo n'envoient pas de header).
5. **SSH déplacé sur 2222** (socket-activé : `/etc/systemd/system/ssh.socket.d/10-alfaway-port.conf`).
6. `.env` en **640 groupe www-data** (lecture par le service Gunicorn).
7. `dj-database-url` ajouté aux dépendances.

## Notes importantes
- Ne jamais committer `.env`.
- **SSH = port 2222** (`ssh -p 2222 ...`). Port 22 = SFTPGo.
- Firewall (ufw) : ouverts → `22/tcp` (SFTP), `2222/tcp` (SSH), `80`, `443`, `8443/tcp` (docker-proxy/Kasm — **ne pas retirer**), `3389` (RDP, autre projet). Règles obsolètes nettoyées (`8000` loopback-only, doublons `22`/profil `OpenSSH`).
- Object Lock : désactivé en dev, activer en production.
- Prod : migrer SQLite → PostgreSQL OVH, durcir CORS, `DEBUG=False` (déjà), activer Object Lock.
- ⚠️ **SPOF S3 → SFTP (à traiter plus tard)** : les users SFTPGo utilisent le backend S3
  **directement** (étape 11, `provider:1`). Le système de fichiers vu en SFTP **EST** le bucket
  Scaleway. Donc en cas de panne S3 (`fr-par` down ou réseau VPS→Scaleway coupé), le SFTP
  devient **inutilisable** : navigation cassée, uploads en échec au `close` (upload_mode
  atomique), erreurs opaques côté fund admin. Le webhook Django n'aide pas (appelé seulement
  après upload réussi).
  **Décision retenue : option B** (implémentation reportée) → backend SFTPGo **local
  (`provider:0`, filesystem)** + **sync async** vers S3 via worker déclenché par le webhook
  (`execute_on:["upload"]` est déjà câblé). Découple la dispo du SFTP de celle de S3 : le
  fichier atterrit sur le disque local instantanément, un worker le pousse vers S3 avec retry,
  puis purge le local après confirmation. À cadrer lors de l'implémentation : rétention/purge
  disque (75GB SSD), file de retry + reprise après panne, idempotence, et mapping local→clé S3
  (`key_prefix`). Écartées : (A) statu quo S3-direct (= le SPOF actuel) ; (C) S3-direct +
  résilience (ne supprime pas le SPOF, dégradation propre seulement).
- Creds SFTPGo admin : `/root/.sftpgo_bootstrap` + `/etc/sftpgo/sftpgo.env` (600).
- Artefacts de test : bucket `alfaway-dev`, objets `test/*`, user SFTP `sftp_test`.
