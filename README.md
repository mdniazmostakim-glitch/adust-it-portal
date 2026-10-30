# ADUST IT Section — Service Request Portal

University staff send work to the **IT section**; IT staff work on each request and post
updates, and everyone is notified. The portal opens only from the **university network**,
works in **English and Bangla**, and runs on a single small server (Python + SQLite).

![IT dashboard](docs/screenshots/08-it-dashboard.png)

## Features

| Area | What it does |
|---|---|
| **University network only** | Only allowed IPs/ranges can open the site (ADUST: `172.20.0.0/16`); others see "Access restricted". Managed in *Admin → Network access*. Admin cannot lock themselves out. |
| **Sign-in** | **Any email** (institutional or personal, e.g. Gmail) **or** user ID + password. Anyone on the university network can register; set `PORTAL_ALLOWED_EMAIL_DOMAINS` to limit domains. **Forgot password** by email link (1 hour, single use). **Lock-out** after 5 wrong passwords (15 min). *Optional, off by default:* sign in with the university webmail password (IMAP). |
| **Assign work to IT** | Category, priority 1–10, urgent flag, needed-by date, which IT staff, location, **device (asset tag)**, description, remarks, attachment. Code like `IT-2026-0001`. Matching **help articles are suggested** while filling the form. |
| **Department-head approval** | Categories marked "needs approval" (e.g. *New equipment / Purchase*) go first to the requester's department head, who approves or rejects with a reason. |
| **IT workflow** | Queue sorted by urgency → priority → deadline. Status, reassign, priority, device, replies, attachments, **internal IT notes**. **Printable job card** with signature lines. |
| **Requester** | Track progress, comment, cancel, **confirm + rate (1–5 ★)** when done, or reopen. |
| **Notifications** | Bell + desktop pop-up + **email**; **SMS** to IT for urgent work and escalations. |
| **Reminders & escalation** | New request untouched for 4 h (1 h if urgent) → IT admins alerted. Overdue → daily alert. Pending approvals → reminder to the head. Resolved but not confirmed for 7 days → closed automatically. All times adjustable in *Settings*. |
| **Devices (asset inventory)** | Every PC/printer/projector/switch with tag, serial, location, warranty; full repair history; "consider replacing" hint; CSV import & Excel export. |
| **Recurring maintenance** | Daily / weekly / monthly / quarterly / yearly jobs become requests automatically. |
| **Help centre** | IT writes short how-to articles (headings, steps, links); searchable. |
| **Announcements** | Banner on every page, e.g. "Campus internet is down; we are working on it". |
| **Reports** | Any period: totals, avg. first response, avg. time to resolve, avg. rating, per IT staff, per category, per department, devices with most requests, last 12 months. **Excel** download and **Print / PDF**. Request lists also export to Excel. |
| **Backups** | Daily automatic zip of database + uploaded files (kept 14 days), plus "Create backup now" and download in *Admin → Backups*. |
| **Bangla / English** | Switch with one click in the header; remembered per user. |

Roles: **Requester** (staff/faculty), **IT staff**, **IT admin** (IT staff + manages the portal). Any user can also be marked **Department head** (approver).

## Quick start (try it)

```bash
pip install -r requirements.txt
flask --app app create-admin
flask --app app run --host 0.0.0.0
```

## Run on a Windows PC (testing)

1. Install Python from python.org, then unzip the project (e.g. `G:\adust-it-portal`).
2. Open `windows\start-portal.ps1` in Notepad and check the settings at the top (data folder, backup copy, network).
3. Right-click `windows\allow-firewall.bat` → **Run as administrator** (once).
4. Double-click `windows\create-admin.bat` (once) to create the IT admin.
5. Double-click `windows\start-portal.bat` every time you want to run the portal.

## Deploy on the university server (Ubuntu)

```bash
sudo apt install python3-venv nginx
sudo mkdir -p /opt/adust-it-portal /var/lib/adust-it-portal/uploads /var/lib/adust-it-portal/backups
sudo cp -r . /opt/adust-it-portal && cd /opt/adust-it-portal
sudo python3 -m venv venv && sudo venv/bin/pip install -r requirements.txt
sudo cp deploy/portal.env.example /etc/adust-it-portal.env   # then edit it
sudo chown -R www-data:www-data /var/lib/adust-it-portal
sudo -u www-data bash -c 'set -a; . /etc/adust-it-portal.env; venv/bin/flask --app app create-admin'
sudo cp deploy/adust-it-portal.service deploy/adust-it-portal-housekeeping.* /etc/systemd/system/
sudo systemctl enable --now adust-it-portal adust-it-portal-housekeeping.timer
sudo cp deploy/nginx.conf /etc/nginx/sites-available/adust-it-portal
sudo ln -s /etc/nginx/sites-available/adust-it-portal /etc/nginx/sites-enabled/ && sudo systemctl reload nginx
```

Then, from a campus PC:

1. Sign in as admin → **Admin → Network access**: check the IP the server sees and add the campus ranges.
2. **Admin → Settings**: check the system status, send a test email / SMS.
3. **Admin → Users**: add IT staff; tick *Department head* for heads of departments.
4. **Admin → Categories**: choose which categories need approval.
5. **IT → Devices**: import your device list (CSV template provided).

Reminders and backups also run on their own when people use the portal; the systemd timer
makes sure they run at night too.

### Keeping the data safe (backup & restore)

All requests, users and history live in **one database file (`portal.db`)** plus the
**`uploads/`** folder. The portal never deletes requests, so the history stays forever
as long as these are safe. Follow the 3-2-1 rule:

| Copy | Where | Setting |
|---|---|---|
| Live data | Server's own local disk (not a network/Google Drive folder) | `PORTAL_DATABASE`, `PORTAL_UPLOADS` |
| Backup copy 1 | Same server, separate folder/disk (automatic daily zip) | `PORTAL_BACKUPS` |
| Backup copy 2 | **Another device**: NAS share, second disk, USB disk or a Google Drive/OneDrive synced folder | `PORTAL_BACKUP_COPY` |

- A backup is made every day; daily backups are kept *N* days (Settings) and **the first
  backup of every month is kept forever**, in both locations.
- *Admin → Backups* shows where everything is stored and warns if copy 2 fails.
- Once a month, also download the newest backup and store it offline (USB/external disk).
- Don't put the live `portal.db` inside Google Drive/OneDrive — syncing can corrupt it.
  Backup zips there are fine.

**Restore:** stop the portal, unzip a backup, copy `portal.db` and `uploads/` back to
their configured locations, start the portal.

### Useful commands

```bash
flask --app app create-admin                                   # another IT admin
flask --app app allow-network 192.168.10.0/24 --label "Admin building"
flask --app app housekeeping                                   # run reminders/backup now
flask --app app backup                                         # backup now
```

## Project layout

```
app.py              routes, database, notifications, housekeeping, CLI
i18n.py             Bangla translations of all interface text
schema.sql          database tables
templates/          pages (help/, assets/, admin/ sub-folders)
static/             style.css, print.css, ADUST logo
deploy/             systemd units, nginx config, environment template
```
