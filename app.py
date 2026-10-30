"""ADUST IT Section — Service Request Portal.

University staff (requesters) assign work to the IT section: hardware,
network, software, email and other jobs, each with a category, priority,
urgency, required-by date, the IT person it goes to, and remarks. IT staff
update the request and both sides are notified in the portal, by email and
(for urgent work) by SMS.

Also included: department-head approval, an asset inventory, recurring
maintenance tasks, a help centre, announcements, reports with Excel/PDF
export, reminders and escalation, automatic backups, password reset and
sign-in lock-out, and an English/Bangla interface.

The portal only answers requests coming from the university's own networks
(see "Network access" in the admin area or PORTAL_ALLOWED_NETWORKS).
"""

import calendar
import csv
import imaplib
import io
import ipaddress
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import threading
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from functools import wraps

import click
from flask import (
    Flask, abort, flash, g, has_request_context, jsonify, redirect, render_template, request,
    send_file, send_from_directory, session, url_for,
)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from markupsafe import Markup, escape
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from i18n import translate

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
env = os.environ.get

ALLOWED_EXTENSIONS = {
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv", "zip",
    "rar", "7z", "png", "jpg", "jpeg", "gif", "webp", "log",
}

DEFAULT_CATEGORIES = [
    ("Hardware / Computer", 0), ("Network / Internet / Wi-Fi", 0), ("Software installation", 0),
    ("Email / Webmail", 0), ("University website", 0), ("Printer / Scanner / Photocopier", 0),
    ("CCTV / Security system", 0), ("Account / Password", 0), ("Projector / Multimedia", 0),
    ("Lab setup", 0), ("Server / Database", 0), ("Data / Report request", 0),
    ("New equipment / Purchase", 1), ("Maintenance", 0), ("Other", 0),
]

DEFAULT_DEPARTMENTS = [
    "Computer Science & Engineering (CSE)", "Electrical & Electronic Engineering (EEE)",
    "Textile Engineering", "Pharmacy", "Business Administration (BBA)", "English", "Law",
    "Public Health", "Registrar Office", "Accounts", "Admission Office",
    "Office of the Controller of Examinations", "Library", "Vice-Chancellor's Office",
    "Proctor Office", "IT Section", "Administration",
]

STATUSES = {
    "pending_approval": "Waiting for approval",
    "new": "New",
    "in_progress": "In progress",
    "on_hold": "On hold",
    "resolved": "Resolved",
    "closed": "Closed",
    "rejected": "Rejected",
    "cancelled": "Cancelled",
}
OPEN_STATUSES = ("new", "in_progress", "on_hold")
IT_SETTABLE = ("new", "in_progress", "on_hold", "resolved", "rejected", "closed")
ROLES = {"user": "Requester", "it": "IT staff", "admin": "IT admin"}

ASSET_TYPES = [
    "Desktop PC", "Laptop", "Monitor", "Printer", "Scanner", "Photocopier", "Projector",
    "Router / Switch", "Wi-Fi access point", "Server", "UPS", "CCTV camera / NVR", "Other",
]
ASSET_STATUSES = {"in_use": "In use", "spare": "Spare", "in_repair": "In repair", "retired": "Retired"}
FREQUENCIES = {"daily": "Daily", "weekly": "Weekly", "monthly": "Monthly",
               "quarterly": "Every 3 months", "yearly": "Yearly"}
LEVELS = {"info": "Information", "warning": "Warning", "danger": "Critical"}

DEFAULT_SETTINGS = {
    "escalate_hours": "4",          # a normal request untouched this long is escalated
    "escalate_hours_urgent": "1",   # same, for urgent requests
    "approval_remind_hours": "24",  # remind department heads about pending approvals
    "auto_close_days": "7",         # resolved and not confirmed -> closed automatically
    "backup_keep_days": "14",
    "sms_urgent": "1",
    "hk_last": "",
    "last_backup": "",
}

MAX_FAILS_PER_ID = 5
MAX_FAILS_PER_IP = 20
LOCKOUT_MINUTES = 15

app = Flask(__name__)
app.config.update(
    SECRET_KEY=env("PORTAL_SECRET_KEY") or secrets.token_hex(32),
    DATABASE=env("PORTAL_DATABASE", os.path.join(BASE_DIR, "portal.db")),
    UPLOAD_FOLDER=env("PORTAL_UPLOADS", os.path.join(BASE_DIR, "uploads")),
    BACKUP_FOLDER=env("PORTAL_BACKUPS", os.path.join(BASE_DIR, "backups")),
    MAX_CONTENT_LENGTH=25 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=env("PORTAL_HTTPS") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    EMAIL_DOMAIN=env("PORTAL_EMAIL_DOMAIN", "adust.edu.bd").lower(),
    BASE_URL=env("PORTAL_BASE_URL", "").rstrip("/"),
    AUTO_HOUSEKEEPING=env("PORTAL_AUTO_HOUSEKEEPING", "1") == "1",
)

if env("PORTAL_TRUST_PROXY") == "1":
    # Behind nginx/Apache: trust one X-Forwarded-For hop for the client IP.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


def now(delta=None):
    t = datetime.now() + (delta or timedelta())
    return t.strftime("%Y-%m-%d %H:%M:%S")


def _(text, **kw):
    return translate(text, getattr(g, "lang", "en"), **kw)


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"], timeout=15)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    with open(os.path.join(BASE_DIR, "schema.sql")) as f:
        db.executescript(f.read())
    db.execute("PRAGMA journal_mode = WAL")
    if db.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0:
        db.executemany("INSERT INTO categories (name, needs_approval) VALUES (?, ?)", DEFAULT_CATEGORIES)
    if db.execute("SELECT COUNT(*) FROM departments").fetchone()[0] == 0:
        db.executemany("INSERT INTO departments (name) VALUES (?)", [(d,) for d in DEFAULT_DEPARTMENTS])
    db.executemany("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", DEFAULT_SETTINGS.items())
    db.commit()
    for folder in ("UPLOAD_FOLDER", "BACKUP_FOLDER"):
        os.makedirs(app.config[folder], exist_ok=True)


def get_setting(key):
    row = get_db().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else DEFAULT_SETTINGS.get(key, "")


def int_setting(key):
    try:
        return int(get_setting(key))
    except ValueError:
        return int(DEFAULT_SETTINGS[key])


def set_setting(key, value):
    db = get_db()
    db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
               "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))
    db.commit()


@app.cli.command("init-db")
def init_db_command():
    """Create the database tables and default data."""
    init_db()
    click.echo("Database initialised.")


@app.cli.command("create-admin")
@click.option("--name", prompt=True)
@click.option("--email", prompt=True)
@click.option("--login-id", prompt="Login ID (e.g. employee ID)")
@click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
def create_admin_command(name, email, login_id, password):
    """Create an IT admin account."""
    init_db()
    db = get_db()
    try:
        db.execute(
            "INSERT INTO users (login_id, name, email, department, designation, role, password_hash, created_at) "
            "VALUES (?, ?, ?, 'IT Section', 'IT Admin', 'admin', ?, ?)",
            (login_id.strip().lower() or None, name.strip(), email.strip().lower(),
             generate_password_hash(password), now()),
        )
        db.commit()
    except sqlite3.IntegrityError:
        raise click.ClickException("A user with that email or login ID already exists.")
    click.echo(f"Admin {email} created.")


@app.cli.command("allow-network")
@click.argument("cidr")
@click.option("--label", default="")
def allow_network_command(cidr, label):
    """Allow a university network, e.g. flask allow-network 103.4.5.0/24"""
    init_db()
    net = ipaddress.ip_network(cidr, strict=False)
    db = get_db()
    db.execute("INSERT OR IGNORE INTO allowed_networks (cidr, label) VALUES (?, ?)", (str(net), label))
    db.commit()
    click.echo(f"Allowed {net}.")


@app.cli.command("housekeeping")
def housekeeping_command():
    """Run reminders, escalation, auto-close, recurring tasks and the daily backup."""
    init_db()
    for k, v in housekeeping().items():
        click.echo(f"{k}: {v}")


@app.cli.command("backup")
def backup_command():
    """Create a backup zip (database + uploaded files) now."""
    init_db()
    click.echo(make_backup())


# --------------------------------------------------------------------------
# University-network-only access
# --------------------------------------------------------------------------

def allowed_networks():
    nets = []
    for raw in (env("PORTAL_ALLOWED_NETWORKS") or "").split(","):
        if raw.strip():
            nets.append(ipaddress.ip_network(raw.strip(), strict=False))
    for row in get_db().execute("SELECT cidr FROM allowed_networks"):
        nets.append(ipaddress.ip_network(row["cidr"], strict=False))
    return nets


def ip_allowed(ip_text):
    """Loopback is always allowed so the server itself can be administered.
    With no networks configured the portal is open (setup mode)."""
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    nets = allowed_networks()
    if not nets:
        return True
    return any(ip in net for net in nets if net.version == ip.version)


@app.before_request
def restrict_to_university_network():
    g.client_ip = request.remote_addr or ""
    g.lang = session.get("lang", "en")
    if request.path.startswith("/static/img/"):
        return None
    if not ip_allowed(g.client_ip):
        return render_template("blocked.html", ip=g.client_ip), 403


# --------------------------------------------------------------------------
# Auth, CSRF, helpers
# --------------------------------------------------------------------------

@app.before_request
def load_user():
    g.user = None
    uid = session.get("user_id")
    if uid is not None:
        g.user = get_db().execute("SELECT * FROM users WHERE id = ? AND active = 1", (uid,)).fetchone()
        if g.user is None:
            session.clear()
        elif "lang" not in session:
            g.lang = session["lang"] = g.user["lang"]


@app.before_request
def csrf_protect():
    if request.method == "POST":
        token = session.get("csrf_token")
        if not token or token != request.form.get("csrf_token"):
            abort(400, _("Your session expired. Please reload the page and try again."))


@app.before_request
def trigger_housekeeping():
    """Runs reminders/backups at most every 5 minutes, piggy-backing on normal
    traffic, so no extra scheduler is required (a cron job also works)."""
    if not app.config["AUTO_HOUSEKEEPING"] or request.path.startswith(("/static", "/api")):
        return
    last = get_setting("hk_last")
    if last and last > now(timedelta(minutes=-5)):
        return
    db = get_db()
    claimed = db.execute("UPDATE settings SET value = ? WHERE key = 'hk_last' AND value = ?", (now(), last))
    db.commit()
    if claimed.rowcount == 1:  # only one worker wins the race
        threading.Thread(target=_housekeeping_thread, daemon=True).start()


def _housekeeping_thread():
    with app.app_context():
        try:
            housekeeping()
        except Exception as exc:
            app.logger.exception("Housekeeping failed: %s", exc)


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    return session["csrf_token"]


def is_it(user=None):
    user = user or g.user
    return user is not None and user["role"] in ("it", "admin")


def is_admin():
    return g.user is not None and g.user["role"] == "admin"


def unread_count():
    if g.user is None:
        return 0
    return get_db().execute("SELECT COUNT(*) FROM notifications WHERE user_id = ? AND is_read = 0",
                            (g.user["id"],)).fetchone()[0]


def approvals_count():
    if g.user is None or not (g.user["is_approver"] or is_admin()):
        return 0
    sql, args = approvals_scope()
    return get_db().execute(f"SELECT COUNT(*) FROM tickets t JOIN users r ON r.id = t.requester_id "
                            f"WHERE t.status = 'pending_approval' AND {sql}", args).fetchone()[0]


def active_announcements():
    return get_db().execute(
        "SELECT * FROM announcements WHERE active = 1 AND (ends_at IS NULL OR ends_at > ?) ORDER BY id DESC",
        (now(),)).fetchall()


def departments():
    return [r["name"] for r in get_db().execute("SELECT name FROM departments WHERE active = 1 ORDER BY name")]


app.jinja_env.globals.update(
    _=_, csrf_token=csrf_token, STATUSES=STATUSES, ROLES=ROLES, OPEN_STATUSES=OPEN_STATUSES,
    ASSET_TYPES=ASSET_TYPES, ASSET_STATUSES=ASSET_STATUSES, FREQUENCIES=FREQUENCIES, LEVELS=LEVELS,
    is_it=is_it, is_admin=is_admin, unread_count=unread_count, approvals_count=approvals_count,
    active_announcements=active_announcements, departments=departments,
    now_year=lambda: date.today().year, today=lambda: date.today().isoformat(),
)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)
    return wrapped


def it_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not is_it():
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not is_admin():
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def save_upload(field="file"):
    """Store an uploaded file. Returns (original, stored), (None, None) when
    nothing was uploaded, or False when the file type is not allowed."""
    upload = request.files.get(field)
    if not upload or not upload.filename:
        return None, None
    ext = upload.filename.rsplit(".", 1)[-1].lower() if "." in upload.filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        return False
    original = secure_filename(upload.filename) or f"file.{ext}"
    stored = f"{uuid.uuid4().hex}_{original}"
    upload.save(os.path.join(app.config["UPLOAD_FOLDER"], stored))
    return original, stored


def valid_date(value):
    if not value:
        return None
    date.fromisoformat(value)  # raises ValueError
    return value


@app.template_filter("dt")
def format_dt(value):
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value).strftime("%d %b %Y, %I:%M %p")
    except ValueError:
        return value


@app.template_filter("d")
def format_d(value):
    if not value:
        return "—"
    try:
        return date.fromisoformat(value[:10]).strftime("%d %b %Y")
    except ValueError:
        return value


@app.template_filter("due_state")
def due_state(ticket):
    if not ticket["due_date"] or ticket["status"] not in OPEN_STATUSES:
        return ""
    days = (date.fromisoformat(ticket["due_date"]) - date.today()).days
    return "overdue" if days < 0 else "soon" if days <= 1 else ""


@app.template_filter("first_name")
def first_name(name):
    """'Prof. Dr. Mahbub Alam' -> 'Mahbub' (skips titles like Dr., Md., Engr.)."""
    titles = {"prof", "dr", "md", "mr", "mrs", "ms", "engr", "mohammad", "muhammad"}
    parts = (name or "").split()
    for p in parts:
        if not p.endswith(".") and p.lower().strip(".") not in titles:
            return p
    return parts[-1] if parts else ""


@app.template_filter("prio")
def prio_band(p):
    return "high" if p >= 8 else "medium" if p >= 5 else "low"


@app.template_filter("filesize")
def filesize(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


URL_RE = re.compile(r"(https?://[^\s<]+)")


@app.template_filter("article")
def render_article(text):
    """Tiny formatter for help articles: '## ' headings, '- ' bullets,
    numbered steps, blank-line paragraphs and clickable links."""
    out, para, items, list_tag = [], [], [], None

    def inline(s):
        return URL_RE.sub(r'<a href="\1" target="_blank" rel="noopener">\1</a>', str(escape(s)))

    def flush():
        nonlocal para, items, list_tag
        if para:
            out.append("<p>" + "<br>".join(inline(p) for p in para) + "</p>")
        if items:
            out.append(f"<{list_tag}>" + "".join(f"<li>{inline(i)}</li>" for i in items) + f"</{list_tag}>")
        para, items, list_tag = [], [], None

    for line in (text or "").splitlines():
        s = line.strip()
        num = re.match(r"^\d+[.)]\s+(.*)", s)
        if not s:
            flush()
        elif s.startswith("## "):
            flush()
            out.append(f"<h3>{inline(s[3:])}</h3>")
        elif s.startswith(("- ", "* ")) or num:
            tag = "ol" if num else "ul"
            if para or (list_tag and list_tag != tag):
                flush()
            list_tag = tag
            items.append(num.group(1) if num else s[2:])
        else:
            if items:
                flush()
            para.append(s)
    flush()
    return Markup("".join(out))


# --------------------------------------------------------------------------
# Email, SMS and in-portal notifications
# --------------------------------------------------------------------------

def send_email_now(to_addrs, subject, body):
    """Send one email per recipient. Returns an error message or None."""
    host = env("SMTP_HOST")
    if not host:
        return "SMTP is not configured"
    if not to_addrs:
        return None
    sender = env("SMTP_FROM") or env("SMTP_USER") or f"it-portal@{app.config['EMAIL_DOMAIN']}"
    try:
        port = int(env("SMTP_PORT", "587"))
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=15, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=15)
            if env("SMTP_STARTTLS", "1") == "1":
                server.starttls(context=ssl.create_default_context())
        with server:
            if env("SMTP_USER"):
                server.login(env("SMTP_USER"), env("SMTP_PASSWORD", ""))
            for addr in to_addrs:  # one message each, so recipients don't see each other
                msg = EmailMessage()
                msg["Subject"], msg["From"], msg["To"] = subject, sender, addr
                msg.set_content(body)
                server.send_message(msg)
    except Exception as exc:
        app.logger.warning("Email to %s failed: %s", to_addrs, exc)
        return str(exc)
    return None


def send_email(to_addrs, subject, body):
    if env("SMTP_HOST") and to_addrs:
        threading.Thread(target=send_email_now, args=(list(to_addrs), subject, body), daemon=True).start()


def normalize_phone(phone):
    digits = re.sub(r"[^\d+]", "", phone or "")
    prefix = env("SMS_COUNTRY_PREFIX", "88")
    if digits.startswith("0") and prefix:
        digits = prefix + digits
    return digits.lstrip("+")


def send_sms_now(phones, text):
    """Send through any HTTP SMS gateway. SMS_API_URL contains {phone} and
    {message} placeholders, e.g. a BulkSMSBD / SSL Wireless / Twilio-style URL."""
    template = env("SMS_API_URL")
    if not template:
        return "SMS gateway is not configured"
    errors = []
    for phone in {normalize_phone(p) for p in phones if p}:
        url = template.replace("{phone}", urllib.parse.quote(phone)).replace(
            "{message}", urllib.parse.quote(text[:300]))
        try:
            if env("SMS_METHOD", "GET").upper() == "POST":
                base, _q, query = url.partition("?")
                req = urllib.request.Request(base, data=query.encode(), method="POST")
            else:
                req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read(200)
        except Exception as exc:
            app.logger.warning("SMS to %s failed: %s", phone, exc)
            errors.append(f"{phone}: {exc}")
    return "; ".join(errors) or None


def send_sms(phones, text):
    phones = [p for p in phones if p]
    if env("SMS_API_URL") and phones:
        threading.Thread(target=send_sms_now, args=(phones, text), daemon=True).start()


def notify(user_ids, ticket, message, sms=False):
    """In-portal notification + email (+ SMS when asked) to the given users,
    skipping whoever caused the event."""
    actor = getattr(g, "user", None)
    user_ids = {u for u in user_ids if u and (actor is None or u != actor["id"])}
    if not user_ids:
        return
    db = get_db()
    ts = now()
    db.executemany(
        "INSERT INTO notifications (user_id, ticket_id, message, created_at) VALUES (?, ?, ?, ?)",
        [(u, ticket["id"], message, ts) for u in user_ids])
    db.commit()
    marks = ",".join("?" * len(user_ids))
    people = db.execute(f"SELECT email, phone FROM users WHERE active = 1 AND id IN ({marks})",
                        tuple(user_ids)).fetchall()
    base = app.config["BASE_URL"] or (request.host_url.rstrip("/") if has_request_context() else "")
    link = f"{base}/tickets/{ticket['id']}" if base else ""
    body = f"{message}\n\nRequest: {ticket['code']} — {ticket['title']}\n{link}\n\n— ADUST IT Section Portal"
    send_email([p["email"] for p in people], f"[{ticket['code']}] {message}", body)
    if sms and get_setting("sms_urgent") == "1":
        send_sms([p["phone"] for p in people], f"ADUST IT {ticket['code']}: {message}"[:300])


def it_team_ids(admins_only=False):
    roles = ("admin",) if admins_only else ("it", "admin")
    marks = ",".join("?" * len(roles))
    return [r[0] for r in get_db().execute(
        f"SELECT id FROM users WHERE active = 1 AND role IN ({marks})", roles)]


def it_recipients(ticket):
    """Who on the IT side hears about a ticket: the assignee plus IT admins,
    or the whole IT team while it is unassigned."""
    if ticket["assigned_to"]:
        return [ticket["assigned_to"], *it_team_ids(admins_only=True)]
    return it_team_ids()


def approver_ids(requester):
    rows = get_db().execute(
        "SELECT id FROM users WHERE active = 1 AND is_approver = 1 AND id != ? "
        "AND lower(department) = lower(?) AND department != ''",
        (requester["id"], requester["department"])).fetchall()
    return [r[0] for r in rows] or it_team_ids(admins_only=True)


def approvals_scope():
    """SQL condition (on requester r) for the tickets the current user may approve."""
    if is_admin():
        return "1=1", ()
    return "lower(r.department) = lower(?) AND r.id != ?", (g.user["department"], g.user["id"])


def add_update(ticket_id, user_id, event="", body="", internal=0, attachment=(None, None)):
    get_db().execute(
        "INSERT INTO ticket_updates (ticket_id, user_id, body, event, internal, attachment_name, "
        "attachment_stored, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ticket_id, user_id, body, event, internal, attachment[0], attachment[1], now()))


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------

def webmail_login(email, password):
    """Verify a university webmail password against the mail server (IMAP)."""
    host = env("PORTAL_IMAP_HOST")
    if not host or not email or not password:
        return False
    try:
        conn = imaplib.IMAP4_SSL(host, int(env("PORTAL_IMAP_PORT", "993")), timeout=10)
        conn.login(email, password)
        conn.logout()
        return True
    except Exception:
        return False


def in_university_domain(email):
    return email.endswith("@" + app.config["EMAIL_DOMAIN"])


def locked_out(ident):
    since = now(timedelta(minutes=-LOCKOUT_MINUTES))
    db = get_db()
    by_id = db.execute("SELECT COUNT(*) FROM login_attempts WHERE ident = ? AND created_at > ?",
                       (ident, since)).fetchone()[0]
    by_ip = db.execute("SELECT COUNT(*) FROM login_attempts WHERE ip = ? AND created_at > ?",
                       (g.client_ip, since)).fetchone()[0]
    return by_id >= MAX_FAILS_PER_ID or by_ip >= MAX_FAILS_PER_IP


def record_failure(ident):
    db = get_db()
    db.execute("INSERT INTO login_attempts (ident, ip, created_at) VALUES (?, ?, ?)", (ident, g.client_ip, now()))
    db.commit()


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("index"))
    if request.method == "POST":
        ident = request.form.get("identifier", "").strip().lower()
        password = request.form.get("password", "")
        db = get_db()
        if locked_out(ident):
            flash(_("Too many failed attempts. Please try again after {n} minutes.", n=LOCKOUT_MINUTES), "error")
            return render_template("login.html", webmail=bool(env("PORTAL_IMAP_HOST")),
                                   domain=app.config["EMAIL_DOMAIN"]), 429
        user = db.execute("SELECT * FROM users WHERE email = ? OR login_id = ?", (ident, ident)).fetchone()
        ok = bool(user and user["password_hash"] and check_password_hash(user["password_hash"], password))
        new_account = False
        if not ok:
            # Fall back to the university webmail password.
            email = user["email"] if user else ident
            if in_university_domain(email) and webmail_login(email, password):
                ok = True
                if user is None:
                    db.execute("INSERT INTO users (name, email, role, created_at) VALUES (?, ?, 'user', ?)",
                               (email.split("@")[0].replace(".", " ").title(), email, now()))
                    db.commit()
                    user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
                    new_account = True
        if not ok:
            record_failure(ident)
            flash(_("Incorrect email / user ID or password."), "error")
        elif not user["active"]:
            flash(_("Your account is deactivated. Please contact the IT section."), "error")
        else:
            db.execute("DELETE FROM login_attempts WHERE ident = ?", (ident,))
            db.execute("UPDATE users SET last_login = ? WHERE id = ?", (now(), user["id"]))
            db.commit()
            lang = session.get("lang")
            session.clear()
            session["user_id"] = user["id"]
            session["lang"] = lang or user["lang"]
            g.lang = session["lang"]
            session.permanent = True
            if new_account:
                flash(_("Welcome! Please complete your profile."), "success")
                return redirect(url_for("profile"))
            nxt = request.args.get("next", "")
            if nxt.startswith("/") and not nxt.startswith("//"):
                return redirect(nxt)
            return redirect(url_for("index"))
    return render_template("login.html", webmail=bool(env("PORTAL_IMAP_HOST")),
                           domain=app.config["EMAIL_DOMAIN"])


@app.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("index"))
    form = request.form
    if request.method == "POST":
        email = form.get("email", "").strip().lower()
        login_id = form.get("login_id", "").strip().lower() or None
        error = None
        if not all(form.get(k, "").strip() for k in ("name", "email", "department")):
            error = _("Name, email and department are required.")
        elif not in_university_domain(email):
            error = _("Please use your university email (@{domain}).", domain=app.config["EMAIL_DOMAIN"])
        elif len(form.get("password", "")) < 8:
            error = _("Password must be at least 8 characters.")
        elif form.get("password") != form.get("confirm"):
            error = _("Passwords do not match.")
        if error is None:
            try:
                db = get_db()
                db.execute(
                    "INSERT INTO users (login_id, name, email, phone, department, designation, role, lang, "
                    "password_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, 'user', ?, ?, ?)",
                    (login_id, form["name"].strip(), email, form.get("phone", "").strip(),
                     form["department"].strip(), form.get("designation", "").strip(), g.lang,
                     generate_password_hash(form["password"]), now()))
                db.commit()
                flash(_("Account created. Please sign in."), "success")
                return redirect(url_for("login"))
            except sqlite3.IntegrityError:
                error = _("An account with that email or user ID already exists.")
        flash(error, "error")
    return render_template("register.html", domain=app.config["EMAIL_DOMAIN"])


def reset_serializer():
    return URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="password-reset")


def password_fingerprint(user):
    # Changes whenever the password changes, so a reset link works only once.
    return (user["password_hash"] or "none")[-16:]


@app.route("/forgot", methods=["GET", "POST"])
def forgot_password():
    if g.user:
        return redirect(url_for("index"))
    if request.method == "POST":
        ident = request.form.get("identifier", "").strip().lower()
        key = f"reset:{ident}"
        if not env("SMTP_HOST"):
            flash(_("Email is not set up on this server. Please contact the IT section to reset your password."), "error")
        elif locked_out(key):
            flash(_("Too many requests. Please try again after {n} minutes.", n=LOCKOUT_MINUTES), "error")
        else:
            record_failure(key)  # counts reset requests too, to prevent email flooding
            user = get_db().execute("SELECT * FROM users WHERE (email = ? OR login_id = ?) AND active = 1",
                                    (ident, ident)).fetchone()
            if user:
                token = reset_serializer().dumps({"id": user["id"], "fp": password_fingerprint(user)})
                base = app.config["BASE_URL"] or request.host_url.rstrip("/")
                link = f"{base}{url_for('reset_password', token=token)}"
                send_email([user["email"]], "ADUST IT Portal — reset your password",
                           f"Hello {user['name']},\n\nOpen this link within 1 hour to set a new password:\n{link}\n\n"
                           "If you did not ask for this, ignore this email.\n\n— ADUST IT Section Portal")
            flash(_("If an account exists for that email or user ID, a reset link has been sent to its email address."), "success")
            return redirect(url_for("login"))
    return render_template("forgot.html")


@app.route("/reset/<token>", methods=["GET", "POST"])
def reset_password(token):
    try:
        data = reset_serializer().loads(token, max_age=3600)
    except (SignatureExpired, BadSignature):
        flash(_("This reset link is invalid or has expired. Please request a new one."), "error")
        return redirect(url_for("forgot_password"))
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ? AND active = 1", (data.get("id"),)).fetchone()
    if user is None or password_fingerprint(user) != data.get("fp"):
        flash(_("This reset link is invalid or has expired. Please request a new one."), "error")
        return redirect(url_for("forgot_password"))
    if request.method == "POST":
        new = request.form.get("new", "")
        if len(new) < 8 or new != request.form.get("confirm"):
            flash(_("New password must be 8+ characters and match the confirmation."), "error")
        else:
            db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(new), user["id"]))
            db.execute("DELETE FROM login_attempts WHERE ident IN (?, ?)", (user["email"], user["login_id"] or ""))
            db.commit()
            flash(_("Password updated. Please sign in."), "success")
            return redirect(url_for("login"))
    return render_template("reset.html", user=user)


@app.route("/logout", methods=["POST"])
def logout():
    lang = session.get("lang")
    session.clear()
    if lang:
        session["lang"] = lang
    flash(_("You have been signed out."), "success")
    return redirect(url_for("login"))


@app.route("/lang/<code>")
def set_language(code):
    if code in ("en", "bn"):
        session["lang"] = code
        if g.user:
            db = get_db()
            db.execute("UPDATE users SET lang = ? WHERE id = ?", (code, g.user["id"]))
            db.commit()
    back = request.referrer or url_for("index")
    return redirect(back if urllib.parse.urlparse(back).netloc in ("", request.host) else url_for("index"))


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    db = get_db()
    if request.method == "POST":
        f = request.form
        if f.get("action") == "password":
            if g.user["password_hash"] and not check_password_hash(g.user["password_hash"], f.get("current", "")):
                flash(_("Current password is incorrect."), "error")
            elif len(f.get("new", "")) < 8 or f.get("new") != f.get("confirm"):
                flash(_("New password must be 8+ characters and match the confirmation."), "error")
            else:
                db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                           (generate_password_hash(f["new"]), g.user["id"]))
                db.commit()
                flash(_("Password updated."), "success")
        elif not f.get("name", "").strip():
            flash(_("Name is required."), "error")
        else:
            lang = f.get("lang") if f.get("lang") in ("en", "bn") else g.user["lang"]
            try:
                db.execute("UPDATE users SET name=?, login_id=?, phone=?, department=?, designation=?, lang=? WHERE id=?",
                           (f["name"].strip(), f.get("login_id", "").strip().lower() or None,
                            f.get("phone", "").strip(), f.get("department", "").strip(),
                            f.get("designation", "").strip(), lang, g.user["id"]))
                db.commit()
                session["lang"] = g.lang = lang
                flash(_("Profile saved."), "success")
            except sqlite3.IntegrityError:
                flash(_("That user ID is already taken."), "error")
        return redirect(url_for("profile"))
    return render_template("profile.html")


# --------------------------------------------------------------------------
# Dashboard & ticket list
# --------------------------------------------------------------------------

TICKET_SELECT = """
    SELECT t.*, c.name AS category, r.name AS requester_name, r.department AS requester_dept,
           r.phone AS requester_phone, r.email AS requester_email,
           a.name AS assignee_name, s.tag AS asset_tag, s.type AS asset_type, s.name AS asset_name,
           ap.name AS approver_name
    FROM tickets t
    LEFT JOIN categories c ON c.id = t.category_id
    JOIN users r ON r.id = t.requester_id
    LEFT JOIN users a ON a.id = t.assigned_to
    LEFT JOIN assets s ON s.id = t.asset_id
    LEFT JOIN users ap ON ap.id = t.approved_by
"""
TICKET_ORDER = """
    ORDER BY (t.status IN ('new','in_progress','on_hold')) DESC, t.urgent DESC,
             t.priority DESC, (t.due_date IS NULL), t.due_date, t.id DESC
"""


@app.route("/")
@login_required
def index():
    db = get_db()
    if is_it():
        scope, args = "1=1", ()
    else:
        scope, args = "t.requester_id = ?", (g.user["id"],)
    counts = {s: 0 for s in STATUSES}
    for row in db.execute(f"SELECT status, COUNT(*) n FROM tickets t WHERE {scope} GROUP BY status", args):
        counts[row["status"]] = row["n"]
    stats = {
        "open": sum(counts[s] for s in OPEN_STATUSES),
        "new": counts["new"],
        "pending": counts["pending_approval"],
        "in_progress": counts["in_progress"] + counts["on_hold"],
        "resolved": counts["resolved"] + counts["closed"],
        "to_confirm": counts["resolved"],
    }
    if is_it():
        stats["mine"] = db.execute(
            f"SELECT COUNT(*) FROM tickets WHERE assigned_to = ? AND status IN {OPEN_STATUSES}",
            (g.user["id"],)).fetchone()[0]
        stats["unassigned"] = db.execute(
            f"SELECT COUNT(*) FROM tickets WHERE assigned_to IS NULL AND status IN {OPEN_STATUSES}").fetchone()[0]
        stats["overdue"] = db.execute(
            f"SELECT COUNT(*) FROM tickets WHERE status IN {OPEN_STATUSES} AND due_date < ?",
            (date.today().isoformat(),)).fetchone()[0]
        stats["urgent"] = db.execute(
            f"SELECT COUNT(*) FROM tickets WHERE status IN {OPEN_STATUSES} AND urgent = 1").fetchone()[0]
    status_scope = OPEN_STATUSES if is_it() else (*OPEN_STATUSES, "pending_approval", "resolved")
    tickets = db.execute(
        f"{TICKET_SELECT} WHERE {scope} AND t.status IN {status_scope} {TICKET_ORDER} LIMIT 15", args).fetchall()
    return render_template("dashboard.html", stats=stats, tickets=tickets, approvals=approvals_count())


def ticket_filters(q):
    """Build the WHERE clause for the request list / Excel export from query args."""
    where, args = [], []
    if not is_it():
        where.append("t.requester_id = ?")
        args.append(g.user["id"])
    status = q.get("status", "open")
    if status == "open":
        where.append(f"t.status IN {OPEN_STATUSES}")
    elif status in STATUSES:
        where.append("t.status = ?")
        args.append(status)
    if q.get("category", "").isdigit():
        where.append("t.category_id = ?")
        args.append(int(q["category"]))
    if q.get("priority") == "high":
        where.append("(t.priority >= 8 OR t.urgent = 1)")
    if q.get("urgent"):
        where.append("t.urgent = 1")
    if q.get("overdue"):
        where.append(f"t.status IN {OPEN_STATUSES} AND t.due_date < ?")
        args.append(date.today().isoformat())
    if q.get("from"):
        where.append("t.created_at >= ?")
        args.append(q["from"])
    if q.get("to"):
        where.append("t.created_at < date(?, '+1 day')")
        args.append(q["to"])
    if is_it():
        assigned = q.get("assigned", "")
        if assigned == "me":
            where.append("t.assigned_to = ?")
            args.append(g.user["id"])
        elif assigned == "none":
            where.append("t.assigned_to IS NULL")
        elif assigned.isdigit():
            where.append("t.assigned_to = ?")
            args.append(int(assigned))
        if q.get("asset", "").isdigit():
            where.append("t.asset_id = ?")
            args.append(int(q["asset"]))
    if q.get("q", "").strip():
        term = f"%{q['q'].strip()}%"
        where.append("(t.code LIKE ? OR t.title LIKE ? OR t.description LIKE ? OR r.name LIKE ? "
                     "OR r.department LIKE ? OR s.tag LIKE ?)")
        args += [term] * 6
    return (" WHERE " + " AND ".join(where) if where else ""), args, status


@app.route("/tickets")
@login_required
def ticket_list():
    where, args, status = ticket_filters(request.args)
    db = get_db()
    tickets = db.execute(f"{TICKET_SELECT} {where} {TICKET_ORDER} LIMIT 500", args).fetchall()
    return render_template("tickets.html", tickets=tickets, status=status,
                           categories=db.execute("SELECT * FROM categories ORDER BY name").fetchall(),
                           it_staff=it_staff())


@app.route("/tickets/export.xlsx")
@login_required
def ticket_export():
    where, args, _status = ticket_filters(request.args)
    rows = get_db().execute(f"{TICKET_SELECT} {where} ORDER BY t.id DESC", args).fetchall()
    headers = ["Code", "Title", "Category", "Priority", "Urgent", "Status", "Needed by", "Requested by",
               "Department", "Assigned to", "Asset", "Location", "Submitted", "First response",
               "Resolved", "Rating", "Description", "Remarks"]
    data = [[t["code"], t["title"], t["category"], t["priority"], "Yes" if t["urgent"] else "",
             STATUSES[t["status"]], t["due_date"], t["requester_name"], t["requester_dept"],
             t["assignee_name"] or "", t["asset_tag"] or "", t["location"], t["created_at"],
             t["first_response_at"], t["resolved_at"], t["rating"], t["description"], t["remarks"]]
            for t in rows]
    return xlsx_response(f"it-requests-{date.today()}.xlsx", [("Requests", headers, data)])


def it_staff():
    return get_db().execute(
        "SELECT id, name, designation FROM users WHERE active = 1 AND role IN ('it','admin') ORDER BY name"
    ).fetchall()


def xlsx_response(filename, sheets):
    """sheets: list of (title, headers, rows)."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    wb.remove(wb.active)
    for title, headers, rows in sheets:
        ws = wb.create_sheet(title[:31])
        ws.append(headers)
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1F3461")
            cell.alignment = Alignment(vertical="center")
        for row in rows:
            ws.append(["" if v is None else v for v in row])
        for i, h in enumerate(headers, 1):
            longest = max([len(str(h))] + [len(str(r[i - 1] or "")) for r in rows[:300]])
            ws.column_dimensions[ws.cell(1, i).column_letter].width = min(max(10, longest + 2), 60)
        ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=filename,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# --------------------------------------------------------------------------
# Create & view tickets
# --------------------------------------------------------------------------

def create_ticket(requester, title, category_id, priority=5, urgent=False, due_date=None, assigned_to=None,
                  description="", remarks="", location="", asset_id=None, attachment=(None, None),
                  recurring_id=None):
    """Insert a ticket, route it (department-head approval or straight to IT)
    and notify the right people. Returns the new ticket row."""
    db = get_db()
    category = db.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()
    needs_approval = (category is not None and category["needs_approval"] and recurring_id is None
                      and not is_it(requester) and not requester["is_approver"])
    status = "pending_approval" if needs_approval else "new"
    ts = now()
    cur = db.execute(
        "INSERT INTO tickets (title, description, remarks, category_id, priority, urgent, due_date, location, "
        "status, requester_id, assigned_to, asset_id, recurring_id, attachment_name, attachment_stored, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (title, description, remarks, category_id, priority, 1 if urgent else 0, due_date, location, status,
         requester["id"], assigned_to, asset_id, recurring_id, attachment[0], attachment[1], ts, ts))
    tid = cur.lastrowid
    code = f"IT-{date.today().year}-{tid:04d}"
    db.execute("UPDATE tickets SET code = ? WHERE id = ?", (code, tid))
    add_update(tid, requester["id"], "Created from recurring maintenance schedule" if recurring_id
               else "Request submitted" + ("; waiting for department head approval" if needs_approval else ""))
    db.commit()
    ticket = db.execute("SELECT * FROM tickets WHERE id = ?", (tid,)).fetchone()
    if needs_approval:
        notify(approver_ids(requester), ticket,
               f"Approval needed: {requester['name']} ({requester['department']}) requested \"{title}\"")
    else:
        tag = "URGENT " if urgent else ""
        notify(it_recipients(ticket), ticket,
               f"New {tag}request from {requester['name']} ({requester['department'] or 'N/A'}): {title}",
               sms=bool(urgent))
    return ticket


def find_asset(tag):
    if not tag:
        return None
    return get_db().execute("SELECT * FROM assets WHERE tag = ?", (tag.strip(),)).fetchone()


@app.route("/tickets/new", methods=["GET", "POST"])
@login_required
def new_ticket():
    db = get_db()
    categories = db.execute("SELECT * FROM categories WHERE active = 1 ORDER BY name").fetchall()
    staff = it_staff()
    assets = db.execute("SELECT tag, type, name, location FROM assets WHERE status != 'retired' ORDER BY tag").fetchall()

    def form_page(code=200):
        return render_template("new_ticket.html", categories=categories, staff=staff, assets=assets), code

    if request.method == "POST":
        f = request.form
        title = f.get("title", "").strip()
        error = None
        asset = None
        try:
            priority = int(f.get("priority", "5"))
            category_id = int(f.get("category_id", ""))
            assigned_to = int(f["assigned_to"]) if f.get("assigned_to") else None
            due = valid_date(f.get("due_date"))
        except ValueError:
            error = _("Please check the form values.")
        if error is None:
            if not title:
                error = _("Please give the work a short title.")
            elif not 1 <= priority <= 10:
                error = _("Priority must be between 1 and 10.")
            elif category_id not in {c["id"] for c in categories}:
                error = _("Please choose a category.")
            elif assigned_to and assigned_to not in {s["id"] for s in staff}:
                error = _("Invalid IT staff selection.")
            elif due and due < date.today().isoformat():
                error = _("The required-by date cannot be in the past.")
            elif f.get("asset_tag", "").strip():
                asset = find_asset(f["asset_tag"])
                if asset is None:
                    error = _("No device with asset tag \"{tag}\" was found. Leave it empty if you are not sure.",
                              tag=f["asset_tag"].strip())
        if error is None:
            upload = save_upload()
            if upload is False:
                error = _("That attachment type is not allowed.")
        if error:
            flash(error, "error")
            return form_page(400)
        ticket = create_ticket(
            g.user, title, category_id, priority, bool(f.get("urgent")), due, assigned_to,
            f.get("description", "").strip(), f.get("remarks", "").strip(), f.get("location", "").strip(),
            asset["id"] if asset else None, upload)
        if ticket["status"] == "pending_approval":
            flash(_("Your request {code} has been sent to your department head for approval.", code=ticket["code"]), "success")
        else:
            flash(_("Your request {code} has been sent to the IT section.", code=ticket["code"]), "success")
        return redirect(url_for("ticket_detail", ticket_id=ticket["id"]))
    return form_page()


def can_approve(t):
    if g.user is None or t["status"] != "pending_approval":
        return False
    if is_admin():
        return True
    return bool(g.user["is_approver"] and t["requester_dept"]
                and t["requester_dept"].lower() == g.user["department"].lower()
                and t["requester_id"] != g.user["id"])


def load_ticket(ticket_id):
    t = get_db().execute(f"{TICKET_SELECT} WHERE t.id = ?", (ticket_id,)).fetchone()
    if t is None:
        abort(404)
    if is_it() or t["requester_id"] == g.user["id"]:
        return t
    if (g.user["is_approver"] and t["requester_dept"]
            and t["requester_dept"].lower() == g.user["department"].lower()):
        return t  # department heads can view their department's requests
    abort(403)


def ticket_timeline(ticket_id, include_internal):
    return get_db().execute(
        "SELECT u.*, COALESCE(us.name, 'System') AS user_name, COALESCE(us.role, 'system') AS user_role "
        "FROM ticket_updates u LEFT JOIN users us ON us.id = u.user_id WHERE u.ticket_id = ? "
        + ("" if include_internal else "AND u.internal = 0 ") + "ORDER BY u.id", (ticket_id,)).fetchall()


@app.route("/tickets/<int:ticket_id>")
@login_required
def ticket_detail(ticket_id):
    ticket = load_ticket(ticket_id)
    db = get_db()
    updates = ticket_timeline(ticket_id, is_it())
    db.execute("UPDATE notifications SET is_read = 1 WHERE user_id = ? AND ticket_id = ?", (g.user["id"], ticket_id))
    db.commit()
    return render_template("ticket.html", t=ticket, updates=updates, staff=it_staff(),
                           it_statuses=IT_SETTABLE, can_approve=can_approve(ticket))


@app.route("/tickets/<int:ticket_id>/print")
@login_required
def ticket_print(ticket_id):
    ticket = load_ticket(ticket_id)
    return render_template("job_card.html", t=ticket, updates=ticket_timeline(ticket_id, False))


@app.route("/tickets/<int:ticket_id>/approve", methods=["POST"])
@login_required
def ticket_approve(ticket_id):
    t = load_ticket(ticket_id)
    if not can_approve(t):
        abort(403)
    db = get_db()
    decision = request.form.get("decision")
    comment = request.form.get("comment", "").strip()
    ts = now()
    if decision == "approve":
        db.execute("UPDATE tickets SET status = 'new', approved_by = ?, approved_at = ?, updated_at = ? WHERE id = ?",
                   (g.user["id"], ts, ts, ticket_id))
        add_update(ticket_id, g.user["id"], "Approved by department head", comment)
        db.commit()
        t = db.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        notify([t["requester_id"]], t, f"Your request was approved by {g.user['name']} and sent to the IT section")
        notify(it_recipients(t), t, f"New approved request: {t['title']}", sms=bool(t["urgent"]))
        flash(_("Request approved and sent to the IT section."), "success")
    elif decision == "reject":
        if not comment:
            flash(_("Please write the reason for rejecting."), "error")
            return redirect(url_for("ticket_detail", ticket_id=ticket_id))
        db.execute("UPDATE tickets SET status = 'rejected', approved_by = ?, approved_at = ?, updated_at = ? "
                   "WHERE id = ?", (g.user["id"], ts, ts, ticket_id))
        add_update(ticket_id, g.user["id"], "Not approved by department head", comment)
        db.commit()
        t = db.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        notify([t["requester_id"]], t, f"Your request was not approved by {g.user['name']}: {comment}")
        flash(_("Request rejected."), "success")
    else:
        abort(400)
    return redirect(url_for("ticket_detail", ticket_id=ticket_id))


@app.route("/approvals")
@login_required
def approvals():
    if not (g.user["is_approver"] or is_admin()):
        abort(403)
    sql, args = approvals_scope()
    tickets = get_db().execute(
        f"{TICKET_SELECT} WHERE t.status = 'pending_approval' AND {sql} ORDER BY t.urgent DESC, t.id", args).fetchall()
    return render_template("approvals.html", tickets=tickets)


@app.route("/tickets/<int:ticket_id>/update", methods=["POST"])
@login_required
def ticket_update(ticket_id):
    """One endpoint for every change: comments, IT status/assignment/priority/
    asset updates, and requester actions (cancel, confirm + rate, reopen)."""
    t = load_ticket(ticket_id)
    db = get_db()
    f = request.form
    body = f.get("body", "").strip()
    events = []
    changes = {}
    is_requester = t["requester_id"] == g.user["id"]

    if is_it():
        new_status = f.get("status", t["status"])
        if new_status != t["status"] and new_status in IT_SETTABLE and t["status"] != "pending_approval":
            changes["status"] = new_status
            events.append(f"Status: {STATUSES[t['status']]} → {STATUSES[new_status]}")
        if "assigned_to" in f:
            assignee = f.get("assigned_to", "")
            new_assignee = int(assignee) if assignee.isdigit() else None
            if new_assignee != t["assigned_to"]:
                staff = {s["id"]: s["name"] for s in it_staff()}
                if new_assignee and new_assignee not in staff:
                    abort(400)
                changes["assigned_to"] = new_assignee
                events.append(f"Assigned to {staff.get(new_assignee, 'nobody (unassigned)')}")
        if f.get("priority", "").isdigit() and 1 <= int(f["priority"]) <= 10 and int(f["priority"]) != t["priority"]:
            changes["priority"] = int(f["priority"])
            events.append(f"Priority: {t['priority']} → {f['priority']}")
        if "asset_tag" in f and f["asset_tag"].strip() != (t["asset_tag"] or ""):
            asset = find_asset(f["asset_tag"])
            if f["asset_tag"].strip() and asset is None:
                flash(_("No device with asset tag \"{tag}\" was found.", tag=f["asset_tag"].strip()), "error")
                return redirect(url_for("ticket_detail", ticket_id=ticket_id))
            changes["asset_id"] = asset["id"] if asset else None
            events.append(f"Device: {asset['tag'] if asset else 'removed'}")
    if is_requester:
        action = f.get("action", "")
        if action == "cancel" and t["status"] in ("new", "pending_approval"):
            changes["status"] = "cancelled"
            events.append("Cancelled by requester")
        elif action == "confirm" and t["status"] == "resolved":
            changes["status"] = "closed"
            events.append("Requester confirmed the work is done")
        elif action == "reopen" and t["status"] in ("resolved", "closed"):
            changes["status"] = "in_progress" if t["assigned_to"] else "new"
            changes["rating"] = None
            events.append("Reopened by requester")
        elif action == "rate" and t["status"] != "closed":
            abort(400)
        if action in ("confirm", "rate") and f.get("rating", "").isdigit() and 1 <= int(f["rating"]) <= 5:
            changes["rating"] = int(f["rating"])
            changes["rating_comment"] = f.get("rating_comment", "").strip()
            events.append(f"Rated {'★' * int(f['rating'])}")
        elif not is_it() and t["status"] in ("closed", "cancelled", "rejected") and body and not events:
            flash(_("This request is closed. Reopen it or submit a new request."), "error")
            return redirect(url_for("ticket_detail", ticket_id=ticket_id))
    elif not is_it():
        abort(403)  # approvers viewing a request may only use the approval form

    upload = save_upload()
    if upload is False:
        flash(_("That attachment type is not allowed."), "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))
    if not body and not events and not upload[0]:
        flash(_("Nothing to update."), "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))

    internal = 1 if (is_it() and f.get("internal") and not events) else 0
    ts = now()
    if "status" in changes:
        changes["resolved_at"] = ts if changes["status"] in ("resolved", "closed") and not t["resolved_at"] else (
            t["resolved_at"] if changes["status"] in ("resolved", "closed") else None)
    if is_it() and not internal and not t["first_response_at"] and not is_requester:
        changes["first_response_at"] = ts
    changes["updated_at"] = ts
    db.execute(f"UPDATE tickets SET {', '.join(k + ' = ?' for k in changes)} WHERE id = ?",
               (*changes.values(), ticket_id))
    add_update(ticket_id, g.user["id"], "; ".join(events), body, internal, upload)
    db.commit()

    t = db.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    summary = "; ".join(events) or "New comment"
    if internal:
        notify(it_recipients(t), t, f"Internal note by {g.user['name']}")
    elif is_it() and not is_requester:
        notify([t["requester_id"]], t, f"IT update on your request: {summary}")
        if changes.get("assigned_to"):
            notify([changes["assigned_to"]], t, f"{g.user['name']} assigned a request to you: {t['title']}",
                   sms=bool(t["urgent"]))
    else:
        notify(it_recipients(t), t, f"{g.user['name']}: {summary}")
    flash(_("Update saved."), "success")
    return redirect(url_for("ticket_detail", ticket_id=ticket_id))


@app.route("/files/<kind>/<int:item_id>")
@login_required
def download(kind, item_id):
    db = get_db()
    if kind == "ticket":
        row = db.execute("SELECT id AS ticket_id, attachment_name, attachment_stored, 0 AS internal "
                         "FROM tickets WHERE id = ?", (item_id,)).fetchone()
    elif kind == "update":
        row = db.execute("SELECT ticket_id, attachment_name, attachment_stored, internal "
                         "FROM ticket_updates WHERE id = ?", (item_id,)).fetchone()
    else:
        abort(404)
    if row is None or not row["attachment_stored"]:
        abort(404)
    load_ticket(row["ticket_id"])  # access check
    if row["internal"] and not is_it():
        abort(403)
    return send_from_directory(app.config["UPLOAD_FOLDER"], row["attachment_stored"],
                               as_attachment=True, download_name=row["attachment_name"])


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------

@app.route("/notifications")
@login_required
def notifications():
    rows = get_db().execute(
        "SELECT n.*, t.code FROM notifications n LEFT JOIN tickets t ON t.id = n.ticket_id "
        "WHERE n.user_id = ? ORDER BY n.id DESC LIMIT 100", (g.user["id"],)).fetchall()
    return render_template("notifications.html", notes=rows)


@app.route("/notifications/read-all", methods=["POST"])
@login_required
def notifications_read_all():
    db = get_db()
    db.execute("UPDATE notifications SET is_read = 1 WHERE user_id = ?", (g.user["id"],))
    db.commit()
    return redirect(url_for("notifications"))


@app.route("/api/notifications")
@login_required
def api_notifications():
    """Polled by the page every 30 s to update the bell and show desktop alerts."""
    since = request.args.get("since", type=int, default=0)
    rows = get_db().execute(
        "SELECT id, ticket_id, message FROM notifications WHERE user_id = ? AND is_read = 0 AND id > ? "
        "ORDER BY id DESC LIMIT 5", (g.user["id"], since)).fetchall()
    return jsonify(unread=unread_count(), latest=[dict(r) for r in rows])


# --------------------------------------------------------------------------
# Help centre (knowledge base)
# --------------------------------------------------------------------------

@app.route("/help")
@login_required
def help_index():
    db = get_db()
    where, args = ["1=1"], []
    if not is_it():
        where.append("k.published = 1")
    if request.args.get("category", "").isdigit():
        where.append("k.category_id = ?")
        args.append(int(request.args["category"]))
    if request.args.get("q", "").strip():
        term = f"%{request.args['q'].strip()}%"
        where.append("(k.title LIKE ? OR k.body LIKE ?)")
        args += [term, term]
    articles = db.execute(
        f"SELECT k.*, c.name AS category FROM kb_articles k LEFT JOIN categories c ON c.id = k.category_id "
        f"WHERE {' AND '.join(where)} ORDER BY c.name, k.title", args).fetchall()
    categories = db.execute("SELECT DISTINCT c.* FROM categories c JOIN kb_articles k ON k.category_id = c.id "
                            "ORDER BY c.name").fetchall()
    return render_template("help/index.html", articles=articles, categories=categories)


@app.route("/help/<int:article_id>")
@login_required
def help_article(article_id):
    db = get_db()
    a = db.execute("SELECT k.*, c.name AS category FROM kb_articles k LEFT JOIN categories c "
                   "ON c.id = k.category_id WHERE k.id = ?", (article_id,)).fetchone()
    if a is None or (not a["published"] and not is_it()):
        abort(404)
    db.execute("UPDATE kb_articles SET views = views + 1 WHERE id = ?", (article_id,))
    db.commit()
    return render_template("help/article.html", a=a)


@app.route("/help/new", methods=["GET", "POST"])
@app.route("/help/<int:article_id>/edit", methods=["GET", "POST"])
@it_required
def help_edit(article_id=None):
    db = get_db()
    a = None
    if article_id:
        a = db.execute("SELECT * FROM kb_articles WHERE id = ?", (article_id,)).fetchone()
        if a is None:
            abort(404)
    if request.method == "POST":
        f = request.form
        if f.get("action") == "delete" and a:
            db.execute("DELETE FROM kb_articles WHERE id = ?", (article_id,))
            db.commit()
            flash(_("Article deleted."), "success")
            return redirect(url_for("help_index"))
        title = f.get("title", "").strip()
        if not title or not f.get("body", "").strip():
            flash(_("Title and content are required."), "error")
        else:
            cat = int(f["category_id"]) if f.get("category_id", "").isdigit() else None
            published = 1 if f.get("published") else 0
            if a:
                db.execute("UPDATE kb_articles SET title=?, body=?, category_id=?, published=?, updated_at=? WHERE id=?",
                           (title, f["body"].strip(), cat, published, now(), article_id))
            else:
                article_id = db.execute(
                    "INSERT INTO kb_articles (title, body, category_id, published, created_by, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (title, f["body"].strip(), cat, published, g.user["id"], now(), now())).lastrowid
            db.commit()
            flash(_("Article saved."), "success")
            return redirect(url_for("help_article", article_id=article_id))
    categories = db.execute("SELECT * FROM categories WHERE active = 1 ORDER BY name").fetchall()
    return render_template("help/edit.html", a=a, categories=categories)


@app.route("/api/help")
@login_required
def api_help():
    """Articles shown on the new-request form when a category is chosen."""
    cat = request.args.get("category", type=int)
    rows = get_db().execute("SELECT id, title FROM kb_articles WHERE published = 1 AND category_id = ? "
                            "ORDER BY views DESC LIMIT 5", (cat,)).fetchall()
    return jsonify([{"id": r["id"], "title": r["title"], "url": url_for("help_article", article_id=r["id"])}
                    for r in rows])


# --------------------------------------------------------------------------
# Assets (device inventory)
# --------------------------------------------------------------------------

ASSET_FIELDS = ("tag", "type", "name", "serial", "department", "location", "user_name",
                "purchase_date", "warranty_until", "status", "notes")


@app.route("/assets")
@it_required
def assets_index():
    q = request.args
    where, args = ["1=1"], []
    if q.get("type") in ASSET_TYPES:
        where.append("a.type = ?")
        args.append(q["type"])
    if q.get("status") in ASSET_STATUSES:
        where.append("a.status = ?")
        args.append(q["status"])
    if q.get("department"):
        where.append("a.department = ?")
        args.append(q["department"])
    if q.get("q", "").strip():
        term = f"%{q['q'].strip()}%"
        where.append("(a.tag LIKE ? OR a.name LIKE ? OR a.serial LIKE ? OR a.location LIKE ? OR a.user_name LIKE ?)")
        args += [term] * 5
    rows = get_db().execute(
        f"SELECT a.*, (SELECT COUNT(*) FROM tickets WHERE asset_id = a.id) AS requests, "
        f"(SELECT COUNT(*) FROM tickets WHERE asset_id = a.id AND status IN {OPEN_STATUSES}) AS open_requests "
        f"FROM assets a WHERE {' AND '.join(where)} ORDER BY a.tag", args).fetchall()
    if request.args.get("export"):
        return xlsx_response(f"it-assets-{date.today()}.xlsx", [(
            "Assets", ["Asset tag", "Type", "Model / name", "Serial no.", "Department", "Location", "User",
                       "Purchase date", "Warranty until", "Status", "Requests", "Notes"],
            [[a["tag"], a["type"], a["name"], a["serial"], a["department"], a["location"], a["user_name"],
              a["purchase_date"], a["warranty_until"], ASSET_STATUSES[a["status"]], a["requests"], a["notes"]]
             for a in rows])])
    return render_template("assets/index.html", assets=rows)


def asset_values(f):
    vals = {k: f.get(k, "").strip() for k in ASSET_FIELDS}
    if not vals["tag"] or vals["type"] not in ASSET_TYPES:
        raise ValueError(_("Asset tag and a valid type are required."))
    if vals["status"] not in ASSET_STATUSES:
        vals["status"] = "in_use"
    for k in ("purchase_date", "warranty_until"):
        try:
            vals[k] = valid_date(vals[k])
        except ValueError:
            raise ValueError(_("Dates must be in YYYY-MM-DD format."))
    return vals


@app.route("/assets/new", methods=["GET", "POST"])
@app.route("/assets/<int:asset_id>/edit", methods=["GET", "POST"])
@it_required
def asset_edit(asset_id=None):
    db = get_db()
    a = None
    if asset_id:
        a = db.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if a is None:
            abort(404)
    if request.method == "POST":
        try:
            vals = asset_values(request.form)
            if a:
                db.execute(f"UPDATE assets SET {', '.join(k + ' = ?' for k in vals)}, updated_at = ? WHERE id = ?",
                           (*vals.values(), now(), asset_id))
            else:
                asset_id = db.execute(
                    f"INSERT INTO assets ({', '.join(vals)}, created_at, updated_at) "
                    f"VALUES ({', '.join('?' * len(vals))}, ?, ?)", (*vals.values(), now(), now())).lastrowid
            db.commit()
            flash(_("Device saved."), "success")
            return redirect(url_for("asset_detail", asset_id=asset_id))
        except ValueError as exc:
            flash(str(exc), "error")
        except sqlite3.IntegrityError:
            flash(_("Another device already uses that asset tag."), "error")
    return render_template("assets/edit.html", a=a)


@app.route("/assets/<int:asset_id>")
@it_required
def asset_detail(asset_id):
    db = get_db()
    a = db.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if a is None:
        abort(404)
    tickets = db.execute(f"{TICKET_SELECT} WHERE t.asset_id = ? ORDER BY t.id DESC", (asset_id,)).fetchall()
    return render_template("assets/detail.html", a=a, tickets=tickets)


@app.route("/assets/import", methods=["GET", "POST"])
@it_required
def assets_import():
    if request.method == "POST":
        upload = request.files.get("file")
        if not upload or not upload.filename.lower().endswith(".csv"):
            flash(_("Please choose a .csv file."), "error")
            return redirect(request.url)
        text = upload.read().decode("utf-8-sig", errors="replace")
        db = get_db()
        added = updated = 0
        errors = []
        for i, row in enumerate(csv.DictReader(io.StringIO(text)), start=2):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            row.setdefault("user_name", row.get("user", ""))
            try:
                vals = asset_values(row)
            except ValueError as exc:
                errors.append(f"Row {i}: {exc}")
                continue
            existing = find_asset(vals["tag"])
            if existing:
                db.execute(f"UPDATE assets SET {', '.join(k + ' = ?' for k in vals)}, updated_at = ? WHERE id = ?",
                           (*vals.values(), now(), existing["id"]))
                updated += 1
            else:
                db.execute(f"INSERT INTO assets ({', '.join(vals)}, created_at, updated_at) "
                           f"VALUES ({', '.join('?' * len(vals))}, ?, ?)", (*vals.values(), now(), now()))
                added += 1
        db.commit()
        flash(_("Import finished: {added} added, {updated} updated.", added=added, updated=updated), "success")
        for e in errors[:10]:
            flash(e, "error")
        return redirect(url_for("assets_index"))
    return render_template("assets/import.html")


@app.route("/assets/template.csv")
@it_required
def assets_template():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["tag", "type", "name", "serial", "department", "location", "user",
                "purchase_date", "warranty_until", "status", "notes"])
    w.writerow(["ADUST-PC-0001", "Desktop PC", "HP ProDesk 400 G7", "CZC1234ABC", "Registrar Office",
                "Admin building, Room 201", "Nasrin Akter", "2024-01-15", "2027-01-14", "in_use", ""])
    return send_file(io.BytesIO(out.getvalue().encode("utf-8-sig")), as_attachment=True,
                     download_name="assets-template.csv", mimetype="text/csv")


# --------------------------------------------------------------------------
# Announcements
# --------------------------------------------------------------------------

@app.route("/admin/announcements", methods=["GET", "POST"])
@it_required
def admin_announcements():
    db = get_db()
    if request.method == "POST":
        f = request.form
        if f.get("toggle", "").isdigit():
            db.execute("UPDATE announcements SET active = 1 - active WHERE id = ?", (int(f["toggle"]),))
        elif f.get("message", "").strip():
            ends = f.get("ends_at", "").strip().replace("T", " ") or None
            if ends and len(ends) == 16:
                ends += ":00"
            db.execute("INSERT INTO announcements (message, level, ends_at, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
                       (f["message"].strip(), f.get("level") if f.get("level") in LEVELS else "info",
                        ends, g.user["id"], now()))
            flash(_("Announcement published."), "success")
        db.commit()
        return redirect(url_for("admin_announcements"))
    rows = db.execute("SELECT a.*, u.name AS author FROM announcements a LEFT JOIN users u ON u.id = a.created_by "
                      "ORDER BY a.id DESC LIMIT 50").fetchall()
    return render_template("admin/announcements.html", rows=rows, now_ts=now())


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------

def report_range():
    q = request.args
    today = date.today()
    preset = q.get("range", "30d" if not (q.get("from") or q.get("to")) else "custom")
    if preset == "month":
        start, end = today.replace(day=1), today
    elif preset == "lastmonth":
        end = today.replace(day=1) - timedelta(days=1)
        start = end.replace(day=1)
    elif preset == "year":
        start, end = today.replace(month=1, day=1), today
    elif preset == "all":
        start, end = date(2000, 1, 1), today
    elif preset == "custom":
        try:
            start = date.fromisoformat(q.get("from") or "2000-01-01")
            end = date.fromisoformat(q.get("to") or today.isoformat())
        except ValueError:
            start, end = today - timedelta(days=29), today
    else:
        preset, start, end = "30d", today - timedelta(days=29), today
    return preset, start.isoformat(), end.isoformat()


def report_data(start, end):
    db = get_db()
    rng = "t.created_at >= ? AND t.created_at < date(?, '+1 day')"
    a = (start, end)
    hours = "(julianday({}) - julianday(t.created_at)) * 24"
    summary = db.execute(
        f"SELECT COUNT(*) total, SUM(t.status IN {OPEN_STATUSES}) open, "
        "SUM(t.status IN ('resolved','closed')) done, SUM(t.urgent) urgent, "
        "SUM(t.status = 'pending_approval') pending, "
        f"ROUND(AVG({hours.format('t.first_response_at')}), 1) avg_response, "
        f"ROUND(AVG({hours.format('t.resolved_at')}), 1) avg_resolve, "
        "ROUND(AVG(t.rating), 2) avg_rating, COUNT(t.rating) ratings "
        f"FROM tickets t WHERE {rng}", a).fetchone()
    by_status = db.execute(f"SELECT status, COUNT(*) n FROM tickets t WHERE {rng} GROUP BY status ORDER BY n DESC", a).fetchall()
    by_staff = db.execute(
        "SELECT u.name, COUNT(t.id) total, "
        f"SUM(t.status IN {OPEN_STATUSES}) open, SUM(t.status IN ('resolved','closed')) done, "
        f"ROUND(AVG({hours.format('t.resolved_at')}), 1) avg_hours, ROUND(AVG(t.rating), 1) avg_rating "
        f"FROM users u LEFT JOIN tickets t ON t.assigned_to = u.id AND {rng} "
        "WHERE u.role IN ('it','admin') AND u.active = 1 GROUP BY u.id ORDER BY total DESC, u.name", a).fetchall()
    by_category = db.execute(
        f"SELECT COALESCE(c.name, '—') name, COUNT(*) total, SUM(t.status IN {OPEN_STATUSES}) open, "
        f"SUM(t.status IN ('resolved','closed')) done FROM tickets t LEFT JOIN categories c ON c.id = t.category_id "
        f"WHERE {rng} GROUP BY c.id ORDER BY total DESC", a).fetchall()
    by_dept = db.execute(
        f"SELECT COALESCE(NULLIF(r.department,''),'—') dept, COUNT(*) n FROM tickets t "
        f"JOIN users r ON r.id = t.requester_id WHERE {rng} GROUP BY dept ORDER BY n DESC LIMIT 15", a).fetchall()
    top_assets = db.execute(
        f"SELECT s.id, s.tag, s.type, s.name, s.location, COUNT(*) n FROM tickets t JOIN assets s ON s.id = t.asset_id "
        f"WHERE {rng} GROUP BY s.id ORDER BY n DESC LIMIT 10", a).fetchall()
    monthly = db.execute(
        "SELECT substr(created_at, 1, 7) month, COUNT(*) n, SUM(status IN ('resolved','closed')) done "
        "FROM tickets WHERE created_at >= date('now', 'start of month', '-11 months') GROUP BY month ORDER BY month").fetchall()
    return dict(summary=summary, by_status=by_status, by_staff=by_staff, by_category=by_category,
                by_dept=by_dept, top_assets=top_assets, monthly=monthly,
                max_month=max([m["n"] for m in monthly] or [1]))


@app.route("/admin/reports")
@it_required
def admin_reports():
    preset, start, end = report_range()
    data = report_data(start, end)
    if request.args.get("export") == "xlsx":
        s = data["summary"]
        return xlsx_response(f"it-report-{start}-to-{end}.xlsx", [
            ("Summary", ["Measure", "Value"], [
                ["Period", f"{start} to {end}"], ["Total requests", s["total"]], ["Open", s["open"] or 0],
                ["Resolved / closed", s["done"] or 0], ["Urgent", s["urgent"] or 0],
                ["Waiting for approval", s["pending"] or 0],
                ["Avg. first response (hours)", s["avg_response"]], ["Avg. time to resolve (hours)", s["avg_resolve"]],
                ["Avg. rating (1-5)", s["avg_rating"]], ["Number of ratings", s["ratings"]]]),
            ("IT staff", ["Staff", "Total", "Open", "Done", "Avg. resolve (h)", "Avg. rating"],
             [[r["name"], r["total"], r["open"] or 0, r["done"] or 0, r["avg_hours"], r["avg_rating"]] for r in data["by_staff"]]),
            ("Categories", ["Category", "Total", "Open", "Done"],
             [[r["name"], r["total"], r["open"], r["done"]] for r in data["by_category"]]),
            ("Departments", ["Department", "Requests"], [[r["dept"], r["n"]] for r in data["by_dept"]]),
            ("Devices", ["Asset tag", "Type", "Model", "Location", "Requests"],
             [[r["tag"], r["type"], r["name"], r["location"], r["n"]] for r in data["top_assets"]]),
            ("Monthly", ["Month", "Requests", "Resolved / closed"], [[m["month"], m["n"], m["done"]] for m in data["monthly"]]),
        ])
    template = "admin/report_print.html" if request.args.get("print") else "admin/reports.html"
    return render_template(template, preset=preset, start=start, end=end, **data)


# --------------------------------------------------------------------------
# Admin: users, departments, categories, network, recurring tasks, backups, settings
# --------------------------------------------------------------------------

@app.route("/admin/users", methods=["GET", "POST"])
@admin_required
def admin_users():
    db = get_db()
    if request.method == "POST":
        f = request.form
        uid = f.get("id", "")
        role = f.get("role", "user")
        email = f.get("email", "").strip().lower()
        if role not in ROLES or not f.get("name", "").strip() or not email:
            flash(_("Name, email and a valid role are required."), "error")
            return redirect(url_for("admin_users"))
        fields = (f.get("login_id", "").strip().lower() or None, f["name"].strip(), email,
                  f.get("phone", "").strip(), f.get("department", "").strip(),
                  f.get("designation", "").strip(), role, 1 if f.get("is_approver") else 0)
        password = f.get("password", "")
        if password and len(password) < 8:
            flash(_("Password must be at least 8 characters."), "error")
            return redirect(url_for("admin_users"))
        try:
            if uid.isdigit():
                if int(uid) == g.user["id"] and role != "admin":
                    flash(_("You cannot remove your own admin role."), "error")
                    return redirect(url_for("admin_users"))
                db.execute("UPDATE users SET login_id=?, name=?, email=?, phone=?, department=?, designation=?, "
                           "role=?, is_approver=? WHERE id=?", (*fields, int(uid)))
                if password:
                    db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(password), int(uid)))
                flash(_("User updated."), "success")
            else:
                db.execute("INSERT INTO users (login_id, name, email, phone, department, designation, role, is_approver, "
                           "password_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (*fields, generate_password_hash(password) if password else None, now()))
                flash(_("Account for {name} created.", name=fields[1]), "success")
            db.commit()
        except sqlite3.IntegrityError:
            flash(_("Email or user ID already in use."), "error")
        return redirect(url_for("admin_users"))
    edit = None
    if request.args.get("edit", "").isdigit():
        edit = db.execute("SELECT * FROM users WHERE id = ?", (int(request.args["edit"]),)).fetchone()
    where, args = "1=1", []
    if request.args.get("q", "").strip():
        where = "(name LIKE ? OR email LIKE ? OR login_id LIKE ? OR department LIKE ?)"
        args = [f"%{request.args['q'].strip()}%"] * 4
    users = db.execute(f"SELECT * FROM users WHERE {where} ORDER BY CASE role WHEN 'admin' THEN 0 "
                       f"WHEN 'it' THEN 1 ELSE 2 END, name", args).fetchall()
    return render_template("admin/users.html", users=users, edit=edit)


@app.route("/admin/users/<int:user_id>/toggle", methods=["POST"])
@admin_required
def admin_toggle_user(user_id):
    if user_id == g.user["id"]:
        flash(_("You cannot deactivate your own account."), "error")
    else:
        db = get_db()
        db.execute("UPDATE users SET active = 1 - active WHERE id = ?", (user_id,))
        db.commit()
    return redirect(url_for("admin_users"))


@app.route("/admin/departments", methods=["GET", "POST"])
@admin_required
def admin_departments():
    db = get_db()
    if request.method == "POST":
        f = request.form
        if f.get("toggle", "").isdigit():
            db.execute("UPDATE departments SET active = 1 - active WHERE id = ?", (int(f["toggle"]),))
        elif f.get("name", "").strip():
            try:
                db.execute("INSERT INTO departments (name) VALUES (?)", (f["name"].strip(),))
                flash(_("Department added."), "success")
            except sqlite3.IntegrityError:
                flash(_("That department already exists."), "error")
        db.commit()
        return redirect(url_for("admin_departments"))
    rows = db.execute(
        "SELECT d.*, (SELECT COUNT(*) FROM users u WHERE lower(u.department) = lower(d.name)) AS people, "
        "(SELECT group_concat(u.name, ', ') FROM users u WHERE u.is_approver = 1 AND u.active = 1 "
        " AND lower(u.department) = lower(d.name)) AS heads FROM departments d ORDER BY d.name").fetchall()
    return render_template("admin/departments.html", rows=rows)


@app.route("/admin/categories", methods=["GET", "POST"])
@admin_required
def admin_categories():
    db = get_db()
    if request.method == "POST":
        f = request.form
        if f.get("toggle", "").isdigit():
            db.execute("UPDATE categories SET active = 1 - active WHERE id = ?", (int(f["toggle"]),))
        elif f.get("approval", "").isdigit():
            db.execute("UPDATE categories SET needs_approval = 1 - needs_approval WHERE id = ?", (int(f["approval"]),))
        elif f.get("name", "").strip():
            try:
                db.execute("INSERT INTO categories (name, needs_approval) VALUES (?, ?)",
                           (f["name"].strip(), 1 if f.get("needs_approval") else 0))
                flash(_("Category added."), "success")
            except sqlite3.IntegrityError:
                flash(_("That category already exists."), "error")
        db.commit()
        return redirect(url_for("admin_categories"))
    cats = db.execute("SELECT c.*, (SELECT COUNT(*) FROM tickets WHERE category_id = c.id) AS n "
                      "FROM categories c ORDER BY c.name").fetchall()
    return render_template("admin/categories.html", categories=cats)


@app.route("/admin/network", methods=["GET", "POST"])
@admin_required
def admin_network():
    db = get_db()
    if request.method == "POST":
        f = request.form
        if f.get("delete", "").isdigit():
            db.execute("DELETE FROM allowed_networks WHERE id = ?", (int(f["delete"]),))
            db.commit()
            if not ip_allowed(g.client_ip):
                db.execute("INSERT INTO allowed_networks (cidr, label) VALUES (?, ?)",
                           (str(ipaddress.ip_network(g.client_ip)), "auto-added: your current IP"))
                db.commit()
                flash(_("Removing that network would have locked you out, so your current IP was kept."), "error")
            else:
                flash(_("Network removed."), "success")
        else:
            try:
                net = ipaddress.ip_network(f.get("cidr", "").strip(), strict=False)
            except ValueError:
                flash(_("Enter a valid IP or network, e.g. 103.110.10.0/24"), "error")
                return redirect(url_for("admin_network"))
            first_rule = not allowed_networks()
            db.execute("INSERT OR IGNORE INTO allowed_networks (cidr, label) VALUES (?, ?)",
                       (str(net), f.get("label", "").strip()))
            if first_rule and not ip_allowed(g.client_ip):
                # Activating the restriction from outside it would lock the admin out.
                db.execute("INSERT OR IGNORE INTO allowed_networks (cidr, label) VALUES (?, ?)",
                           (str(ipaddress.ip_network(g.client_ip)), "auto-added: your current IP"))
                flash(_("Your current IP was also added so you are not locked out."), "success")
            db.commit()
            flash(_("{net} allowed.", net=str(net)), "success")
        return redirect(url_for("admin_network"))
    rows = db.execute("SELECT * FROM allowed_networks ORDER BY id").fetchall()
    env_nets = [n.strip() for n in (env("PORTAL_ALLOWED_NETWORKS") or "").split(",") if n.strip()]
    return render_template("admin/network.html", rows=rows, env_nets=env_nets)


def advance_run(day, frequency):
    if frequency == "daily":
        return day + timedelta(days=1)
    if frequency == "weekly":
        return day + timedelta(days=7)
    months = {"monthly": 1, "quarterly": 3, "yearly": 12}[frequency]
    m = day.month - 1 + months
    y, m = day.year + m // 12, m % 12 + 1
    return date(y, m, min(day.day, calendar.monthrange(y, m)[1]))


@app.route("/admin/recurring", methods=["GET", "POST"])
@admin_required
def admin_recurring():
    db = get_db()
    categories = db.execute("SELECT * FROM categories WHERE active = 1 ORDER BY name").fetchall()
    edit = None
    if request.args.get("edit", "").isdigit():
        edit = db.execute("SELECT r.*, a.tag AS asset_tag FROM recurring_tasks r LEFT JOIN assets a ON a.id = r.asset_id "
                          "WHERE r.id = ?", (int(request.args["edit"]),)).fetchone()
    if request.method == "POST":
        f = request.form
        if f.get("toggle", "").isdigit():
            db.execute("UPDATE recurring_tasks SET active = 1 - active WHERE id = ?", (int(f["toggle"]),))
            db.commit()
            return redirect(url_for("admin_recurring"))
        if f.get("delete", "").isdigit():
            db.execute("DELETE FROM recurring_tasks WHERE id = ?", (int(f["delete"]),))
            db.commit()
            flash(_("Schedule deleted."), "success")
            return redirect(url_for("admin_recurring"))
        if f.get("run_now", "").isdigit():
            n = run_recurring(only_id=int(f["run_now"]))
            flash(_("{n} request(s) created.", n=n), "success")
            return redirect(url_for("admin_recurring"))
        try:
            title = f.get("title", "").strip()
            next_run = valid_date(f.get("next_run"))
            if not title or not next_run or f.get("frequency") not in FREQUENCIES or not f.get("category_id", "").isdigit():
                raise ValueError(_("Title, category, frequency and first date are required."))
            asset = find_asset(f.get("asset_tag", ""))
            if f.get("asset_tag", "").strip() and asset is None:
                raise ValueError(_("No device with asset tag \"{tag}\" was found.", tag=f["asset_tag"].strip()))
            vals = (title, f.get("description", "").strip(), int(f["category_id"]),
                    max(1, min(10, int(f.get("priority") or 5))),
                    int(f["assigned_to"]) if f.get("assigned_to", "").isdigit() else None,
                    asset["id"] if asset else None, f.get("location", "").strip(), f["frequency"],
                    max(0, int(f.get("lead_days") or 0)), next_run)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(request.url)
        if f.get("id", "").isdigit():
            db.execute("UPDATE recurring_tasks SET title=?, description=?, category_id=?, priority=?, assigned_to=?, "
                       "asset_id=?, location=?, frequency=?, lead_days=?, next_run=? WHERE id=?", (*vals, int(f["id"])))
        else:
            db.execute("INSERT INTO recurring_tasks (title, description, category_id, priority, assigned_to, asset_id, "
                       "location, frequency, lead_days, next_run, created_by, created_at) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (*vals, g.user["id"], now()))
        db.commit()
        flash(_("Schedule saved."), "success")
        return redirect(url_for("admin_recurring"))
    rows = db.execute(
        "SELECT r.*, c.name AS category, u.name AS assignee, a.tag AS asset_tag FROM recurring_tasks r "
        "LEFT JOIN categories c ON c.id = r.category_id LEFT JOIN users u ON u.id = r.assigned_to "
        "LEFT JOIN assets a ON a.id = r.asset_id ORDER BY r.active DESC, r.next_run").fetchall()
    return render_template("admin/recurring.html", rows=rows, edit=edit, categories=categories, staff=it_staff())


def list_backups():
    folder = app.config["BACKUP_FOLDER"]
    if not os.path.isdir(folder):
        return []
    files = []
    for name in os.listdir(folder):
        if name.startswith("adust-it-backup-") and name.endswith(".zip"):
            st = os.stat(os.path.join(folder, name))
            files.append({"name": name, "size": st.st_size,
                          "time": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")})
    return sorted(files, key=lambda f: f["name"], reverse=True)


def make_backup():
    """Zip a consistent copy of the database plus all uploaded files, then
    delete backups older than the retention period (always keeping 3)."""
    folder = app.config["BACKUP_FOLDER"]
    os.makedirs(folder, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(folder, f"adust-it-backup-{stamp}.zip")
    tmp = path + ".db"
    src, dst = sqlite3.connect(app.config["DATABASE"]), sqlite3.connect(tmp)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(tmp, "portal.db")
        uploads = app.config["UPLOAD_FOLDER"]
        for name in os.listdir(uploads):
            full = os.path.join(uploads, name)
            if os.path.isfile(full):
                z.write(full, f"uploads/{name}")
    os.remove(tmp)
    cutoff = (datetime.now() - timedelta(days=int_setting("backup_keep_days"))).strftime("%Y-%m-%d %H:%M:%S")
    for old in list_backups()[3:]:
        if old["time"] < cutoff:
            os.remove(os.path.join(folder, old["name"]))
    set_setting("last_backup", now())
    return path


@app.route("/admin/backups", methods=["GET", "POST"])
@admin_required
def admin_backups():
    if request.method == "POST":
        path = make_backup()
        flash(_("Backup created: {name}", name=os.path.basename(path)), "success")
        return redirect(url_for("admin_backups"))
    return render_template("admin/backups.html", files=list_backups(), last=get_setting("last_backup"),
                           folder=app.config["BACKUP_FOLDER"], keep=int_setting("backup_keep_days"))


@app.route("/admin/backups/<name>")
@admin_required
def admin_backup_download(name):
    if not re.fullmatch(r"adust-it-backup-\d{8}-\d{6}\.zip", name):
        abort(404)
    return send_from_directory(app.config["BACKUP_FOLDER"], name, as_attachment=True)


@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    if request.method == "POST":
        f = request.form
        if f.get("action") == "test_email":
            err = send_email_now([g.user["email"]], "ADUST IT Portal — test email",
                                 "This is a test email from the ADUST IT Service Request Portal.")
            flash(_("Test email failed: {err}", err=err) if err else
                  _("Test email sent to {email}.", email=g.user["email"]), "error" if err else "success")
        elif f.get("action") == "test_sms":
            if not g.user["phone"]:
                flash(_("Add your mobile number in your profile first."), "error")
            else:
                err = send_sms_now([g.user["phone"]], "ADUST IT Portal: test SMS")
                flash(_("Test SMS failed: {err}", err=err) if err else
                      _("Test SMS sent to {phone}.", phone=g.user["phone"]), "error" if err else "success")
        elif f.get("action") == "housekeeping":
            result = housekeeping()
            flash(_("Checks completed: {summary}", summary=", ".join(f"{k}={v}" for k, v in result.items())), "success")
        else:
            for key in ("escalate_hours", "escalate_hours_urgent", "approval_remind_hours",
                        "auto_close_days", "backup_keep_days"):
                if f.get(key, "").isdigit():
                    set_setting(key, int(f[key]))
            set_setting("sms_urgent", "1" if f.get("sms_urgent") else "0")
            flash(_("Settings saved."), "success")
        return redirect(url_for("admin_settings"))
    status = {
        "email": bool(env("SMTP_HOST")), "sms": bool(env("SMS_API_URL")),
        "webmail": bool(env("PORTAL_IMAP_HOST")), "secret": bool(env("PORTAL_SECRET_KEY")),
        "https": env("PORTAL_HTTPS") == "1", "networks": bool(allowed_networks()),
    }
    values = {k: get_setting(k) for k in DEFAULT_SETTINGS}
    return render_template("admin/settings.html", status=status, values=values)


# --------------------------------------------------------------------------
# Housekeeping: reminders, escalation, auto-close, recurring tasks, backups
# --------------------------------------------------------------------------

def run_recurring(only_id=None):
    db = get_db()
    today = date.today()
    sql = "SELECT * FROM recurring_tasks WHERE active = 1 AND " + ("id = ?" if only_id else "next_run <= ?")
    created = 0
    for task in db.execute(sql, (only_id if only_id else today.isoformat(),)).fetchall():
        owner = db.execute("SELECT * FROM users WHERE id = ?", (task["created_by"],)).fetchone()
        if owner is None:
            continue
        run_day = date.fromisoformat(task["next_run"]) if not only_id else today
        create_ticket(owner, task["title"], task["category_id"], task["priority"], False,
                      (max(run_day, today) + timedelta(days=task["lead_days"])).isoformat(),
                      task["assigned_to"], task["description"], "", task["location"], task["asset_id"],
                      recurring_id=task["id"])
        nxt = date.fromisoformat(task["next_run"])
        while nxt <= today:
            nxt = advance_run(nxt, task["frequency"])
        db.execute("UPDATE recurring_tasks SET last_run = ?, next_run = ? WHERE id = ?",
                   (now(), nxt.isoformat(), task["id"]))
        db.commit()
        created += 1
    return created


def housekeeping():
    db = get_db()
    result = {"escalated": 0, "overdue": 0, "approval_reminders": 0, "auto_closed": 0, "recurring": 0, "backup": "-"}

    # 1. New requests nobody has picked up
    for urgent, key in ((1, "escalate_hours_urgent"), (0, "escalate_hours")):
        cutoff = now(timedelta(hours=-int_setting(key)))
        for t in db.execute("SELECT * FROM tickets WHERE status = 'new' AND urgent = ? AND escalated_at IS NULL "
                            "AND first_response_at IS NULL AND updated_at < ?", (urgent, cutoff)).fetchall():
            hours = int_setting(key)
            msg = f"No response for {hours}+ hours: {t['title']}"
            db.execute("UPDATE tickets SET escalated_at = ? WHERE id = ?", (now(), t["id"]))
            add_update(t["id"], None, f"Escalated to IT admins: no response for {hours}+ hours", internal=1)
            db.commit()
            notify(set(it_recipients(t)) | set(it_team_ids(admins_only=True)), t, msg, sms=bool(urgent))
            result["escalated"] += 1

    # 2. Past the needed-by date (once per day per request)
    today = date.today().isoformat()
    for t in db.execute(f"SELECT * FROM tickets WHERE status IN {OPEN_STATUSES} AND due_date < ? "
                        "AND (overdue_notified IS NULL OR overdue_notified < ?)", (today, today)).fetchall():
        db.execute("UPDATE tickets SET overdue_notified = ? WHERE id = ?", (today, t["id"]))
        db.commit()
        notify(set(it_recipients(t)) | set(it_team_ids(admins_only=True)), t,
               f"Overdue (needed by {t['due_date']}): {t['title']}")
        result["overdue"] += 1

    # 3. Approvals waiting on a department head
    cutoff = now(timedelta(hours=-int_setting("approval_remind_hours")))
    for t in db.execute("SELECT * FROM tickets WHERE status = 'pending_approval' AND escalated_at IS NULL "
                        "AND created_at < ?", (cutoff,)).fetchall():
        requester = db.execute("SELECT * FROM users WHERE id = ?", (t["requester_id"],)).fetchone()
        db.execute("UPDATE tickets SET escalated_at = ? WHERE id = ?", (now(), t["id"]))
        db.commit()
        notify(approver_ids(requester), t, f"Reminder: request waiting for your approval: {t['title']}")
        result["approval_reminders"] += 1

    # 4. Resolved but never confirmed by the requester
    days = int_setting("auto_close_days")
    cutoff = now(timedelta(days=-days))
    for t in db.execute("SELECT * FROM tickets WHERE status = 'resolved' AND updated_at < ?", (cutoff,)).fetchall():
        db.execute("UPDATE tickets SET status = 'closed', updated_at = ? WHERE id = ?", (now(), t["id"]))
        add_update(t["id"], None, f"Closed automatically: no response from requester for {days} days")
        db.commit()
        notify([t["requester_id"]], t, f"Your request was closed automatically after {days} days. "
                                       "You can still rate it or reopen it.")
        result["auto_closed"] += 1

    # 5. Recurring maintenance tasks
    result["recurring"] = run_recurring()

    # 6. Daily backup
    last = get_setting("last_backup")
    if not last or last < now(timedelta(hours=-24)):
        result["backup"] = os.path.basename(make_backup())

    db.execute("DELETE FROM login_attempts WHERE created_at < ?", (now(timedelta(days=-1)),))
    db.commit()
    return result


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(413)
def http_error(err):
    messages = {403: "You do not have permission to view this page.",
                404: "Page not found.", 413: "The file is too large (max 25 MB)."}
    msg = err.description if err.code == 400 else _(messages[err.code])
    return render_template("error.html", code=err.code, message=msg), err.code


with app.app_context():
    init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(env("PORT", "5000")), debug=env("FLASK_DEBUG") == "1")
