# AlfaWay — Description technique complète (état au 2026-06-13)

> Document de référence destiné à servir de base de connaissance (projet Claude)
> pour cadrer la suite du développement. Il décrit l'application **telle qu'elle
> est réellement déployée et codée**, pas le runbook initial. Sources : code du
> dépôt `/opt/alfaway`, `CLAUDE.md` (as-built), configuration VPS.

---

## 1. En une phrase

AlfaWay est une plateforme de **réception de fichiers par SFTP pour des fund
admins**, qui **stocke les fichiers dans un object storage S3 (Scaleway)** et
offre un **back-office de supervision (monitoring) en temps réel** des fichiers
reçus, avec téléchargement sécurisé par URL pré-signée. C'est le socle d'une
future chaîne de contrôles réglementaires (DORA).

---

## 2. Rôle métier et flux principal

1. Un **fund admin** se connecte en **SFTP** (client standard) et dépose un fichier.
2. Le serveur SFTP (**SFTPGo**) écrit le fichier **directement dans le bucket S3**
   Scaleway (backend S3 « direct », `provider:1`). Le système de fichiers vu en
   SFTP **EST** le bucket.
3. SFTPGo déclenche des **webhooks HTTP** vers l'application Django à deux moments :
   `pre-upload` (avant écriture S3) et `upload` (après). Django journalise chaque
   fichier dans une base PostgreSQL (modèle `ReceivedFile`).
4. Un **administrateur** consulte la page **`/monitoring/`** (auto-rafraîchie) :
   il voit le flux des fichiers, leur état, et peut **télécharger** un fichier via
   une **URL pré-signée** S3 (le navigateur tape Scaleway directement, sans transiter
   par le serveur).
5. Une commande planifiée (`reconcile_files`, 03:30 UTC) **réconcilie** la base avec
   la vérité du bucket S3, pour rattraper les webhooks perdus / divergences.

**Décision d'architecture clé (SPOF assumé) :** SFTPGo écrit en **S3 direct**.
Conséquence : si S3 (`fr-par`) tombe, le SFTP devient inutilisable. Cette panne
est un **SPOF connu et accepté** (« option C » : on ajoute de l'observabilité, pas
de la résilience). L'alternative « option B » (backend local + worker de sync async
vers S3) a été **écartée puis reportée** — voir §10.

---

## 3. Stack technique réelle

| Composant | Version / détail |
|-----------|------------------|
| OS VPS | Ubuntu 25.10, OVH VPS-1 (4 vCores, 8 Go RAM, 75 Go SSD) |
| IP publique | `141.227.152.10` — domaine `alfaway.algotech.lu` |
| Python | **3.13.7** (système) |
| Django | **5.2** |
| API | Django REST Framework |
| Serveur SFTP | **SFTPGo 2.7.3** (installé via `.deb` GitHub) |
| Object storage | **Scaleway** S3-compatible, région `fr-par`, bucket dev `alfaway-dev` |
| Base de données | **PostgreSQL 17 local** (`127.0.0.1:5432`), base/rôle **`way`** |
| App server | Gunicorn (3 workers) sur `127.0.0.1:8010` |
| Reverse proxy | Nginx + **HTTPS Let's Encrypt** (redirect 80→443, certbot auto-renew) |
| SDK S3 | boto3 (signature v4 obligatoire pour le presign) |

> ⚠️ **PostgreSQL est déjà en service** (bascule SQLite→PG le 2026-06-12).
> Le `CLAUDE.md` mentionne encore SQLite « provisoire » à certains endroits, et
> `db.sqlite3` traîne dans le repo mais est **inerte**. La vérité : `DATABASE_URL=
> postgres://way:…@127.0.0.1:5432/way` dans `.env`. **NB : la base s'appelle `way`,
> pas `alfaway`.**

### Cohabitation VPS (contrainte de ports)

Le VPS héberge 3 projets ; d'où l'allocation de ports atypique :

| Port | Service |
|------|---------|
| **2222** | SSH admin (déplacé depuis 22 !) — `ssh -p 2222 ubuntu@141.227.152.10` |
| **22**   | **SFTPGo** (les fund admins) |
| **8090** | SFTPGo admin / REST API (loopback) |
| **8010** | Gunicorn AlfaWay (8000 = TAP, 8001/8002 = TrustLink) |
| **80/443** | Nginx |

---

## 4. Structure du dépôt

```
/opt/alfaway/
├── alfaway/                  # projet Django (settings, urls, wsgi/asgi)
│   ├── settings.py           # config via python-decouple + dj-database-url
│   └── urls.py               # toutes les routes (pas de urls.py par app)
├── api/                      # app principale (webhook, monitoring, S3, presign)
│   ├── models.py             # ReceivedFile (le seul modèle métier)
│   ├── views.py              # webhook + monitoring + download + actions
│   ├── s3.py                 # client boto3 + presigned_get_url
│   ├── admin.py              # ReceivedFileAdmin (read-only + lien download)
│   ├── management/commands/
│   │   └── reconcile_files.py # réconciliation S3 ↔ base (filet de sécurité)
│   ├── migrations/           # 0001..0004
│   └── templates/
│       ├── monitoring.html   # SPA monitoring (1 fichier, vanilla JS, i18n)
│       └── admin/receivedfile_changelist.html
├── core/                     # app quasi vide (placeholder, à utiliser plus tard)
├── scripts/
│   └── migrate_sftpgo_to_pg.sh # migration du provider SFTPGo SQLite→PG (ops)
├── examples/download_demo.html # démo d'usage de l'endpoint presign par token
├── test_s3.py                # script de test connectivité S3
├── .env                      # secrets (NON versionné), perms 640 ubuntu:www-data
├── CLAUDE.md                 # doc as-built (déploiement, déviations, incidents)
└── manage.py
```

> `core/` est généré par `startproject` mais **vide** (views/models placeholders).
> Tout le code métier vit dans `api/`. Il n'y a **pas de fichier `urls.py` par app**
> : toutes les routes sont déclarées dans `alfaway/urls.py`.

---

## 5. Modèle de données — `api.ReceivedFile`

Unique modèle métier. Une ligne = un fichier reçu (ou en cours de réception).
Les champs sont calqués sur le payload des webhooks SFTPGo + suivi interne.

### Machine à états (`state`, `TextChoices`)

| État | Sens | Posé par |
|------|------|----------|
| `receiving` | Réception en cours (avant écriture S3) | hook `pre-upload` |
| `stored` | Écrit avec succès dans S3 | hook `upload` (succès) |
| `failed` | Échec d'upload | hook `upload` (status ≠ 1) |
| `deleted` | Supprimé via SFTP | hook `delete` |
| `missing` | Ligne `stored` mais objet absent de S3 (drift) | `reconcile_files` |
| `archived` | `failed` traité, sorti du board Live (manuel, réversible) | action UI |

### Champs notables

- **Identité** : `s3_key` = chemin **virtuel** SFTPGo ; `path` = chemin **physique**
  (= la vraie clé S3 = `key_prefix` + virtuel). **Piège récurrent : le presign et la
  réconciliation utilisent `path` (physique), PAS `s3_key`**, sinon 404 S3.
- **Transport** : `username`, `protocol`, `ip`, `session_id`.
- **Caractéristiques** : `file_size`, `status` (1 = OK côté SFTPGo), `bucket`,
  `action`, `sftpgo_timestamp` (epoch ns).
- **Suivi interne** : `processed` (booléen **réservé au futur worker DORA**, jamais
  utilisé pour l'instant), `received_at`, `stored_at`, `deleted_at`, `archived_at`,
  `reconciled` (True si la ligne vient d'un backfill et non d'un hook).
- **`raw`** : `JSONField` conservant le **payload webhook complet** (robustesse si
  SFTPGo évolue ; utilisé p.ex. pour extraire `elapsed` = durée de transfert).

Index : sur `state`, `s3_key`, `username`, `received_at`, `processed`, `reconciled`,
+ index composite `(username, -received_at)`. `ordering = ['-received_at']`.

---

## 6. Endpoints (toutes les routes dans `alfaway/urls.py`)

| Route | Vue | Auth | Rôle |
|-------|-----|------|------|
| `POST /api/internal/sftp-webhook/` | `SFTPWebhookView` | **token partagé** (`?token=` ou header `X-Webhook-Token`) | Reçoit les hooks SFTPGo |
| `GET /api/internal/files/<pk>/download-url/` | `PresignedDownloadView` | **token interne** (`INTERNAL_TOKEN`) | Renvoie une URL pré-signée (JSON) |
| `GET /files/<pk>/download/` | `download_received_file` | **session staff** | Proxy authentifié → 302 vers S3 |
| `POST /files/<pk>/archive/` | `archive_received_file` | session staff + CSRF | `failed` → `archived` |
| `POST /files/<pk>/restore/` | `restore_received_file` | session staff + CSRF | `archived` → `failed` |
| `GET /monitoring/` | `monitoring_page` | session staff | Page HTML de supervision |
| `GET /monitoring/feed/` | `monitoring_feed` | session staff | JSON (lignes + compteurs) |
| `GET /healthz/` | lambda | aucune | Health check → `{"status":"ok"}` |
| `…/admin/` | Django admin | superuser | Admin (read-only sur ReceivedFile) |

**Deux modèles d'authentification cohabitent :**
- **Webhook / endpoint presign-API** : **tokens partagés** (pas de session). Les
  hooks SFTPGo **n'envoient aucun header custom** → le token passe en **query param
  `?token=`** (l'acceptation par header n'existe que pour le test manuel).
- **Monitoring / download / actions** : **session Django staff** (`@staff_member_required`),
  cookie same-origin. Aucun token côté client.

DRF est configuré avec **`SessionAuthentication` uniquement** ; les endpoints à token
mettent `authentication_classes = []` et vérifient le token à la main.

---

## 7. Le webhook SFTPGo (`SFTPWebhookView`) — cœur du système

Reçoit un POST JSON de SFTPGo et dispatche selon `action` :

- **`pre-upload`** → crée la ligne en `receiving` (avant S3). **Idempotent** via
  `get_or_create` sur `(state=receiving, s3_key, session_id)`.
- **`upload`** → corrèle avec la ligne `receiving` (même `session_id` + `s3_key`) et
  la passe en `stored` (succès : `status ∈ {None, 1}`, pose `stored_at`) ou `failed`.
  Si aucune ligne `receiving` n'existe, en crée une directement.
- **`delete`** → passe les lignes `stored` correspondantes en `deleted` (jamais
  d'effacement physique de la ligne ; audit conservé).
- **`rename`** → repointe `s3_key`/`path` de l'ancienne clé vers la nouvelle.

### ⚠️ Invariants critiques à connaître

1. **Le handler renvoie TOUJOURS 200 sur token valide.** `pre-upload` est
   **synchrone et bloquant** côté SFTPGo : si Django renvoyait une erreur, l'upload
   SFTP du fund admin serait **refusé**. D'où un `try/except` englobant qui log mais
   ne propage jamais. **Effet de bord dangereux** : un échec d'enregistrement (ex.
   migration non suivie d'un restart) est **silencieux** — uploads S3 OK mais rien
   dans le monitoring (cf. incident 2026-06-13, §11).
2. **`session_id` + `s3_key`** sont la clé de corrélation pré/post-upload.
3. **Angle mort non testé** : on n'est pas certain que SFTPGo déclenche `upload` en
   cas d'**échec S3**. Si non, la ligne reste coincée en `receiving` (= signal de
   panne implicite). Reco non implémentée : une commande `reconcile_uploads` qui
   bascule les `receiving` trop vieux en `failed`.

---

## 8. Téléchargement & accès S3 (`api/s3.py`, presign)

- `get_s3_client()` : client boto3 Scaleway, **signature v4** (obligatoire pour le
  presign). Creds lus depuis `.env` via `settings` (jamais en dur).
- `presigned_get_url(bucket, key, …)` : URL GET pré-signée, **15 min** par défaut,
  avec `Content-Disposition: attachment; filename=…` pour forcer le nom de fichier.
- `presign_received_file(rf)` : refuse (`FileNotReady`) si l'état n'est pas `stored` ;
  **résout la clé sur `path` (physique)** avec repli sur `s3_key`.

**Deux chemins de téléchargement :**
1. **Web admin** (`/files/<pk>/download/`) : gardé par la session staff, fait un
   302 vers l'URL pré-signée. Aucun token ne transite côté client. C'est ce
   qu'utilise le bouton « Télécharger » du monitoring et de l'admin Django.
2. **API par token** (`/api/internal/files/<pk>/download-url/`) : renvoie l'URL en
   JSON, gardé par `INTERNAL_TOKEN`. Pensé pour une intégration server-side (la démo
   `examples/download_demo.html` montre l'usage, avec l'avertissement de ne **jamais**
   exposer le token en clair côté public).

Dans les deux cas, **le fichier ne transite pas par le serveur** : le navigateur/
client tape Scaleway directement avec l'URL signée.

---

## 9. Page de monitoring (`templates/monitoring.html` + `monitoring_feed`)

SPA mono-fichier (vanilla JS, pas de framework, thème sombre) qui **poll** le feed
JSON toutes les **10 s** (suspendu quand l'onglet est masqué).

### Fonctionnalités UI

- **Deux onglets** : **Live** (`receiving`/`stored`/`failed` — flux opérationnel,
  échecs en tête) et **History** (`deleted`/`missing`/`archived`).
- **Compteurs (chips) par état**, cliquables = **filtre par état**.
- **Tri par colonne** (state, fichier, user, proto, IP, taille, transfert, reçu, stocké).
- **Actions par ligne** : **Archive** (`failed`→`archived`, en Live) et **Restore**
  (`archived`→`failed`, en History), avec CSRF.
- **Téléchargement** par ligne (lien vers le proxy authentifié).
- **i18n complète EN (défaut) / FR / IT** (dictionnaire JS, choix mémorisé en
  `localStorage`).
- Détection des « nouvelles » lignes (flash), badge de session expirée, indicateur
  live/pause/erreur.

### Côté serveur (`monitoring_feed`) — important

Le **tri et le filtre sont appliqués côté serveur sur TOUTE la table**, pas seulement
sur la page renvoyée. La vue annote en base les champs **dérivés** pour que le tri
soit global :
- `_filename` : basename de `s3_key` via `regexp_replace` (insensible casse) ;
- `_elapsed` : durée de transfert extraite du JSON `raw` (`KeyTextTransform` + cast) ;
- `_stored_eff` : `Coalesce(archived_at, deleted_at, stored_at)` pour la colonne
  « horodatage » en History.

Pagination top-N : `limit` défaut **50**, borne dure **500**. Renvoie aussi
`matched_total`, `per_state`, `live_total`, `history_total` pour l'affichage
« affichés / total ». **Lecture seule, pas de CSRF** (GET non mutant), session staff.

---

## 10. Réconciliation (`reconcile_files`) — filet de sécurité

Commande Django planifiée (systemd `alfaway-reconcile.timer`, **03:30 UTC**,
`Persistent=true`) qui aligne la table sur la **vérité du bucket S3** :

- **backfill** : objet S3 sans ligne `stored` → crée une ligne `stored reconciled=True`.
- **promote** : ligne `receiving` alors que l'objet existe → `stored` (hook `upload`
  perdu). Idem `missing` réapparu → `stored`.
- **missing** : ligne `stored` dont l'objet a disparu de S3 → `missing`.

Idempotent ; `--dry-run` n'écrit rien. La clé S3 réelle = `path` (physique), comme le
presign. C'est un **filet**, pas une excuse pour sauter le restart post-migration.

```bash
sudo -u www-data /opt/alfaway/venv/bin/python manage.py reconcile_files --dry-run
```

---

## 11. Exploitation : services, déploiement, pièges

### Services systemd

```
alfaway.service              # Gunicorn (User/Group=www-data, EnvironmentFile=.env)
sftpgo.service               # fourni par le .deb (durci, CAP_NET_BIND_SERVICE pour :22)
alfaway-reconcile.timer      # réconciliation quotidienne 03:30 UTC
nginx / ssh.socket / certbot.timer
```

Particularités du unit Gunicorn (durement acquises) :
- `Environment=HOME=/var/lib/alfaway` + `StateDirectory=alfaway` : **Gunicorn 26**
  écrit son control-socket dans `$HOME/.gunicorn` ; sans HOME dédié writable →
  `Permission denied: '/var/www/.gunicorn'` au démarrage.
- `db.sqlite3` (legacy) et `/opt/alfaway` étaient groupe `www-data` + g+w pour SQLite
  (WAL dans le dossier parent) — résiduel depuis la bascule PG.

### Procédure de déploiement (OBLIGATOIRE)

```bash
# après toute migration OU changement de code Python :
sudo -u www-data /opt/alfaway/venv/bin/python manage.py migrate
sudo systemctl restart alfaway
```

> **Incident 2026-06-13 (à retenir absolument)** : une migration ajoutant
> `reconciled NOT NULL` a été appliquée **sans redémarrer Gunicorn**. Les workers
> tournaient avec l'ancien modèle → chaque insert envoyait `NULL` → `IntegrityError`.
> Comme le webhook renvoie **toujours 200**, l'échec était **silencieux** : uploads
> S3 OK, **rien dans le monitoring**. Diagnostic via `IntegrityError` dans
> `/var/log/alfaway/django.log`. **Règle : migration ⇒ TOUJOURS restart alfaway.**

### Pièges migrations / permissions

- `www-data` ne peut pas écrire dans `api/migrations/` (owné `ubuntu`) → le dossier a
  reçu `chgrp www-data` + `g+w`. Lancer makemigrations/migrate en
  **`sudo -u www-data ./venv/bin/python manage.py …`** (lancer en `ubuntu` casse sur le
  handler de log `/var/log/alfaway/django.log`).

### Validation

```bash
for s in alfaway sftpgo nginx ssh.socket alfaway-reconcile.timer; do systemctl is-active $s; done
curl http://127.0.0.1:8010/healthz/    # {"status":"ok"}
```

---

## 12. Configuration & secrets (`.env`)

Lu par `python-decouple`. Clés présentes (valeurs jamais versionnées) :

```
SECRET_KEY, DEBUG (=False), ALLOWED_HOSTS
SCW_ACCESS_KEY, SCW_SECRET_KEY, SCW_ENDPOINT, SCW_REGION, SCW_BUCKET_PREFIX
DATABASE_URL              # postgres://way:…@127.0.0.1:5432/way
SFTPGO_WEBHOOK_TOKEN      # token du webhook (?token=)
INTERNAL_TOKEN           # token de l'endpoint presign-API
```

Perms : `640 ubuntu:www-data` (Gunicorn tourne en `www-data` et doit lire le fichier).
CORS limité par défaut à `https://alfaway.algotech.lu`. `DEBUG=False`.

---

## 13. Sécurité — état actuel

**Acquis :** HTTPS Let's Encrypt (redirect + auto-renew), `DEBUG=False`, secrets hors
repo, presign 15 min (pas de creds S3 côté client), webhook/presign-API protégés par
token, monitoring/download protégés par session staff, admin read-only sur ReceivedFile,
SSH déplacé sur 2222, ufw configuré.

**Points ouverts / à durcir :**
- **SPOF S3→SFTP** assumé (voir §14).
- **Object Lock S3 désactivé** en dev → à activer en production.
- **CORS** à resserrer en prod.
- Tokens partagés en **query param** (`?token=`) → présents dans les logs d'accès ;
  acceptable pour du loopback/interne mais à garder en tête.
- **Aucun superuser Django créé** à ce jour (l'admin `ReceivedFileAdmin` existe mais
  n'est pas accessible tant qu'un staff n'est pas créé).
- **Pas de tests** (`tests.py` vides).

---

## 14. Dette technique & décisions reportées

1. **SPOF S3→SFTP (le gros sujet).** SFTPGo écrit en **S3 direct** (`provider:1`).
   Si Scaleway `fr-par` tombe ou le réseau VPS→Scaleway est coupé : navigation SFTP
   cassée, uploads en échec au `close` (upload atomique), erreurs opaques côté fund
   admin. Le webhook n'aide pas (appelé seulement après upload réussi).
   - **Décision actée (2026-06-12) : option C** = on **garde** le S3 direct et on
     ajoute seulement de l'**observabilité** (les 2 hooks + reconcile). Le SPOF
     **n'est pas résolu**, juste rendu visible.
   - **Option B (reportée, pas écartée à terme)** : backend SFTPGo **local
     (`provider:0`, filesystem)** + **worker async** qui pousse vers S3 (retry,
     purge locale après confirmation). Découple la dispo SFTP de celle de S3. À
     cadrer : rétention/purge disque (75 Go), file de retry + reprise après panne,
     idempotence, mapping local→clé S3 (`key_prefix`). Le câblage `execute_on:
     ["upload"]` est déjà en place pour ça.
2. **`reconcile_uploads`** (non implémenté) : basculer les `receiving` orphelins/
   trop vieux en `failed` (angle mort « SFTPGo déclenche-t-il `upload` sur échec S3 ? »).
3. **Worker DORA** : le champ `processed` et le TODO dans `_on_upload` sont les hooks
   pour une future chaîne de **contrôles réglementaires DORA** sur les fichiers reçus.
4. **Migration du provider SFTPGo SQLite→PostgreSQL** : script
   `scripts/migrate_sftpgo_to_pg.sh` **écrit et prêt** (DB `sftpgo` dédiée, à côté de
   `way`), mais à exécuter en ops (consolidation backups ; **ne fusionne pas** avec
   `ReceivedFile`). À vérifier s'il a été lancé.
5. **Migration prod** prévue par CLAUDE.md : PostgreSQL OVH (vs PG local actuel),
   Object Lock, durcissement CORS.
6. **`db.sqlite3` legacy** à nettoyer du repo (inerte mais présent).
7. **`core/`** vide : app placeholder disponible pour du nouveau code métier.

---

## 15. Chantier majeur à venir — refonte IAM (droits utilisateur)

Évoqué le 2026-06-13, **sans date fixée** : **refonte complète de la définition des
droits utilisateur** via une **interface IAM dédiée** (pas une surcouche de l'admin
Django).

Implications à cadrer quand ce chantier démarre :
- Dépasse l'i18n de l'admin Django : la gestion des droits **ne passera pas** par
  l'admin standard mais par une **UI IAM propre** (rôles, permissions, attribution).
- À définir : modèle de rôles/permissions (vs `auth.Permission`/groups Django),
  **mapping vers les users SFTPGo**, périmètre (qui voit quoi dans le monitoring, qui
  télécharge), et l'interface elle-même.
- La page monitoring est déjà EN/FR/IT ; l'i18n de l'admin Django devient secondaire/
  obsolète si l'admin n'est plus le point d'entrée des droits.

---

## 16. Glossaire des pièges récurrents (à ne jamais oublier)

| Piège | À retenir |
|-------|-----------|
| Clé S3 | objet réel = **`path` (physique)**, pas `s3_key` (virtuel) → sinon 404 |
| Webhook | renvoie **toujours 200** ; échecs **silencieux** → vérifier `django.log` |
| Migration | **TOUJOURS** `systemctl restart alfaway` après migrate |
| Migrations en CLI | lancer en **`sudo -u www-data`**, pas en `ubuntu` (log handler) |
| Token webhook | en **query param `?token=`** (SFTPGo n'envoie pas de header) |
| Base de données | s'appelle **`way`** (PostgreSQL local), pas `alfaway` ni SQLite |
| Ports | **SSH = 2222**, **SFTP = 22**, Gunicorn = 8010 |
| Worker SFTPGo | écrit en **S3 direct** → SPOF S3 assumé (option C) |
```
