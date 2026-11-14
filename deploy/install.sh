#!/usr/bin/env bash
# ADUST IT Section Portal — install or update on an Ubuntu / Debian server.
#
#   sudo bash deploy/install.sh          (run from the unzipped project folder)
#
# First run : installs packages, copies the app to /opt/adust-it-portal, creates the
#             data folders, a settings file with a random secret key, the systemd
#             services, the nginx site, and asks for the first IT admin.
# Later runs: updates the code and Python packages, keeps all data and settings.
set -euo pipefail

APP_DIR=/opt/adust-it-portal
DATA_DIR=/var/lib/adust-it-portal
ENV_FILE=/etc/adust-it-portal.env
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER=www-data

say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

if [[ $EUID -ne 0 ]]; then echo "Please run with sudo: sudo bash deploy/install.sh"; exit 1; fi
if [[ ! -f "$SRC_DIR/app.py" ]]; then echo "app.py not found next to deploy/ — run this from the unzipped project."; exit 1; fi

say "Installing system packages (python3, venv, nginx)"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip nginx >/dev/null

say "Copying the application to $APP_DIR"
mkdir -p "$APP_DIR"
# Code only: never touches the data folder or the settings file.
for item in app.py db.py i18n.py schema.sql requirements.txt README.md templates static data deploy tests; do
    rm -rf "${APP_DIR:?}/$item"
    cp -r "$SRC_DIR/$item" "$APP_DIR/"
done

say "Python environment and packages"
[[ -x "$APP_DIR/venv/bin/python" ]] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

say "Data folders in $DATA_DIR"
mkdir -p "$DATA_DIR/uploads" "$DATA_DIR/backups"
chown -R "$RUN_USER:$RUN_USER" "$DATA_DIR"
chmod 750 "$DATA_DIR"

FIRST_INSTALL=0
if [[ ! -f "$ENV_FILE" ]]; then
    FIRST_INSTALL=1
    say "Creating settings file $ENV_FILE (random secret key)"
    SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    IP=$(hostname -I | awk '{print $1}')
    sed -e "s|^PORTAL_SECRET_KEY=.*|PORTAL_SECRET_KEY=$SECRET|" \
        -e "s|^PORTAL_BACKUP_COPY=.*|PORTAL_BACKUP_COPY=|" \
        -e "s|^PORTAL_BASE_URL=.*|PORTAL_BASE_URL=http://$IP|" \
        "$APP_DIR/deploy/portal.env.example" > "$ENV_FILE"
    chmod 640 "$ENV_FILE"
    chown root:"$RUN_USER" "$ENV_FILE"
fi

say "systemd services (portal + reminders/backup timer)"
cp "$APP_DIR/deploy/adust-it-portal.service" /etc/systemd/system/
cp "$APP_DIR/deploy/adust-it-portal-housekeeping.service" "$APP_DIR/deploy/adust-it-portal-housekeeping.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now adust-it-portal-housekeeping.timer >/dev/null

say "nginx site"
cp "$APP_DIR/deploy/nginx.conf" /etc/nginx/sites-available/adust-it-portal
ln -sf /etc/nginx/sites-available/adust-it-portal /etc/nginx/sites-enabled/adust-it-portal
# The stock "Welcome to nginx" site would answer instead of the portal.
if [[ -L /etc/nginx/sites-enabled/default ]] && grep -q "Welcome to nginx\|/var/www/html" /etc/nginx/sites-available/default 2>/dev/null; then
    rm -f /etc/nginx/sites-enabled/default
fi
nginx -t
systemctl reload nginx

say "Database"
sudo -u "$RUN_USER" bash -c "set -a; . $ENV_FILE; cd $APP_DIR; venv/bin/flask --app app init-db"

if [[ $FIRST_INSTALL -eq 1 ]]; then
    say "Create the first IT admin account"
    sudo -u "$RUN_USER" bash -c "set -a; . $ENV_FILE; cd $APP_DIR; venv/bin/flask --app app create-admin"
fi

systemctl enable adust-it-portal >/dev/null
systemctl restart adust-it-portal
sleep 2
if systemctl is-active --quiet adust-it-portal; then
    IP=$(hostname -I | awk '{print $1}')
    say "Done. The portal is running."
    echo "    Open from a campus PC:  http://$IP/"
    echo "    Settings file:          $ENV_FILE   (email, SMS, allowed networks)"
    echo "    Data (keep backed up):  $DATA_DIR"
    echo "    Logs:                   sudo journalctl -u adust-it-portal -f"
    echo "    Reset a password:       sudo -u $RUN_USER bash -c 'set -a; . $ENV_FILE; cd $APP_DIR; venv/bin/flask --app app reset-password'"
else
    echo "The portal did not start. See: sudo journalctl -u adust-it-portal -n 50"
    exit 1
fi
