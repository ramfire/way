#!/usr/bin/env bash
#
# Migration du data provider SFTPGo : SQLite -> PostgreSQL (DB dédiée `sftpgo`
# sur l'instance locale 127.0.0.1:5432, à côté de la DB Django `way`).
#
# NE FUSIONNE PAS avec api.ReceivedFile : SFTPGo garde ses propres tables
# (users/admins/folders/shares). Objectif = consolidation ops (un seul moteur,
# backups unifiés). La vraie synchro d'événements est assurée par le webhook
# (#1) et `manage.py reconcile_files` (#2), déjà en place.
#
# Stratégie SANS PERTE d'utilisateurs : dump REST (ancien provider) -> bascule
# config -> initprovider (crée le schéma PG) -> restart avec SFTPGO_LOADDATA_FROM
# (réimporte admins+users au démarrage, puis supprime le fichier) -> restart propre.
#
# Lancer :  ! sudo bash /opt/alfaway/scripts/migrate_sftpgo_to_pg.sh
# (Mot de passe admin SFTPGo lu depuis /root/.sftpgo_bootstrap ; override possible
#  via  SFTPGO_ADMIN_PASS=...  sudo -E bash ...)
#
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "ERREUR : à lancer en root (sudo)."; exit 1; }

API="http://127.0.0.1:8090/api/v2"
CONF="/etc/sftpgo/sftpgo.json"
TS="$(date +%Y%m%d-%H%M%S)"
DUMP="/opt/alfaway/sftpgo-dump-pre-pg-$TS.json"
RESTORE="/var/lib/sftpgo/restore-$TS.json"
DROPIN_DIR="/etc/systemd/system/sftpgo.service.d"
DROPIN="$DROPIN_DIR/20-loaddata.conf"
PY="/opt/alfaway/venv/bin/python"

echo "==> 0. Pré-vérifs"
command -v sftpgo >/dev/null || { echo "binaire sftpgo introuvable"; exit 1; }
sudo -u postgres psql -tAc 'select 1' >/dev/null || { echo "postgres injoignable"; exit 1; }

# --- creds admin SFTPGo (pour le dump REST de l'ANCIEN provider) -------------
ADMIN_USER="${SFTPGO_ADMIN_USER:-admin}"
ADMIN_PASS="${SFTPGO_ADMIN_PASS:-}"
if [ -z "$ADMIN_PASS" ]; then
  for f in /root/.sftpgo_bootstrap /etc/sftpgo/sftpgo.env; do
    [ -r "$f" ] || continue
    ADMIN_PASS="$(grep -iE 'pass(word)?' "$f" | head -1 \
      | sed -E 's/^[^:=]*[:=][[:space:]]*//; s/^["'\'' ]+//; s/["'\'' ]+$//')"
    [ -n "$ADMIN_PASS" ] && { echo "    mot de passe admin lu depuis $f"; break; }
  done
fi
[ -n "$ADMIN_PASS" ] || { echo "ERREUR : mot de passe admin introuvable. Relancez avec SFTPGO_ADMIN_PASS=... sudo -E bash $0"; exit 1; }

echo "==> 1. Token REST + dump de l'ancien provider"
TOKEN="$(curl -fsS -u "$ADMIN_USER:$ADMIN_PASS" "$API/token" \
  | "$PY" -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')" \
  || { echo "ERREUR : auth REST échouée (mauvais mot de passe ?)."; exit 1; }
curl -fsS -H "Authorization: Bearer $TOKEN" "$API/dumpdata?output-data=1" -o "$DUMP"
"$PY" - "$DUMP" <<'PY'
import sys, json
d = json.load(open(sys.argv[1]))
u = [x.get("username") for x in d.get("users", [])]
a = [x.get("username") for x in d.get("admins", [])]
print(f"    dump OK : admins={a} users={u} folders={len(d.get('folders',[]))}")
assert a, "aucun admin dans le dump — abandon"
PY
echo "    sauvegardé : $DUMP"

echo "==> 2. Backups (config + provider SQLite)"
cp -a "$CONF" "$CONF.bak-$TS"
SQLITE_PATH="$("$PY" - "$CONF" <<'PY'
import sys, json, os
c = json.load(open(sys.argv[1]))
dp = c["data_provider"]
name = dp.get("name", "")
# chemin relatif => résolu sous le répertoire de travail du service (/var/lib/sftpgo)
print(name if os.path.isabs(name) else os.path.join("/var/lib/sftpgo", name))
PY
)"
if [ -f "$SQLITE_PATH" ]; then cp -a "$SQLITE_PATH" "$SQLITE_PATH.bak-$TS"; echo "    SQLite sauvegardé : $SQLITE_PATH.bak-$TS"; fi
echo "    config sauvegardée : $CONF.bak-$TS"

echo "==> 3. Rôle + base PostgreSQL pour SFTPGo"
PG_PASS="$(openssl rand -base64 24 | tr -d '/+=' | head -c 32)"
sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='sftpgo') THEN
    CREATE ROLE sftpgo LOGIN;
  END IF;
END \$\$;
ALTER ROLE sftpgo LOGIN PASSWORD '$PG_PASS';
SQL
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='sftpgo'" | grep -q 1 \
  || sudo -u postgres createdb -O sftpgo sftpgo
echo "    rôle 'sftpgo' + base 'sftpgo' prêts."

echo "==> 4. Bascule data_provider -> postgresql dans $CONF"
"$PY" - "$CONF" "$PG_PASS" <<'PY'
import sys, json
path, pw = sys.argv[1], sys.argv[2]
c = json.load(open(path))
dp = c["data_provider"]
dp.update({
    "driver": "postgresql",
    "name": "sftpgo",
    "host": "127.0.0.1",
    "port": 5432,
    "username": "sftpgo",
    "password": pw,
    "sslmode": 0,            # disable (connexion locale loopback)
})
json.dump(c, open(path, "w"), indent=2)
print("    driver =", dp["driver"], "| db =", dp["name"], "| host =", dp["host"])
PY

echo "==> 5. initprovider (création du schéma dans PostgreSQL)"
sudo -u sftpgo sftpgo initprovider --config-dir /etc/sftpgo

echo "==> 6. Préparation du loaddata au démarrage (réimport admins+users)"
install -o sftpgo -g sftpgo -m 600 "$DUMP" "$RESTORE"
mkdir -p "$DROPIN_DIR"
cat > "$DROPIN" <<EOF
[Service]
Environment=SFTPGO_LOADDATA_FROM=$RESTORE
Environment=SFTPGO_LOADDATA_MODE=0
EOF
systemctl daemon-reload

echo "==> 7. Restart SFTPGo (init + import) "
systemctl restart sftpgo
sleep 3
systemctl is-active --quiet sftpgo || { echo "ERREUR : sftpgo ne démarre pas. Voir 'journalctl -u sftpgo'. ROLLBACK plus bas."; exit 1; }

echo "==> 8. Nettoyage du drop-in + restart propre"
rm -f "$DROPIN"
systemctl daemon-reload
systemctl restart sftpgo
sleep 2
systemctl is-active --quiet sftpgo || { echo "ERREUR : sftpgo KO après restart propre."; exit 1; }

echo "==> 9. Vérifications"
echo -n "    REST up (HTTP) : "; curl -s -o /dev/null -w '%{http_code}\n' "$API/version"
echo "    Utilisateurs dans PostgreSQL :"
sudo -u postgres psql -d sftpgo -tAc "SELECT username FROM users ORDER BY username;" | sed 's/^/      - /'
echo "    Admins dans PostgreSQL :"
sudo -u postgres psql -d sftpgo -tAc "SELECT username FROM admins ORDER BY username;" | sed 's/^/      - /'

cat <<EOF

==> TERMINÉ. SFTPGo tourne désormais sur PostgreSQL (db 'sftpgo').
    Le fichier d'import $RESTORE a été supprimé automatiquement (LOADDATA_MODE=0).

ROLLBACK si besoin :
    sudo cp -a "$CONF.bak-$TS" "$CONF"
    $( [ -f "$SQLITE_PATH.bak-$TS" ] && echo "sudo cp -a \"$SQLITE_PATH.bak-$TS\" \"$SQLITE_PATH\"" )
    sudo systemctl restart sftpgo
    # (la DB PostgreSQL 'sftpgo' peut être laissée ou supprimée :
    #  sudo -u postgres dropdb sftpgo ; sudo -u postgres dropuser sftpgo)

PENSER À : retirer le bloc 'sftpgo-direct-S3' du SPOF doc, et vérifier
les checks de l'étape Validation du CLAUDE.md (alfaway/sftpgo/nginx actifs).
EOF
