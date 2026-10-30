"""ADUST IT Section — Service Request Portal.

University staff (requesters) assign work to the IT section: hardware,
network, software, email and other jobs, each with a category, priority,
urgency, required-by date, the IT person it goes to, and remarks. IT staff
update the request and both sides are notified in the portal (and by email
when SMTP is configured).

The portal only answers requests coming from the university's own networks
(see "Allowed networks" in the admin area or PORTAL_ALLOWED_NETWORKS).
"""

import imaplib
import ipaddress
import os
import secrets
import smtplib
import sqlite3
import ssl
import threading
import uuid
from datetime import date, datetime
from email.message import EmailMessage
from functools import wraps

import click
from flask import (
    Flask, abort, flash, g, jsonify, redirect, render_template, request,
    send_from_directory, session, url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
env = os.environ.get

ALLOWED_EXTENSIONS = {
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv", "zip",
    "rar", "7z", "png", "jpg", "jpeg", "gif", "webp", "log",
}

DEFAULT_CATEGORIES = [
    "Hardware / Computer", "Network / Internet / Wi-Fi", "Software installation",
    "Email / Webmail", "University website", "Printer / Scanner / Photocopier",
    "CCTV / Security system", "Account / Password", "Projector / Multimedia",
    "Lab setup", "Server / Database", "Data / Report request", "Other",
]

STATUSES = {
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

app = Flask(__name__)
app.config.update(
    SECRET_KEY=env("PORTAL_SECRET_KEY") or secrets.token_hex(32),
    DATABASE=env("PORTAL_DATABASE", os.path.join(BASE_DIR, "portal.db")),
    UPLOAD_FOLDER=env("PORTAL_UPLOADS", os.path.join(BASE_DIR, "uploads")),
    MAX_CONTENT_LENGTH=25 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=env("PORTAL_HTTPS") == "1",
    EMAIL_DOMAIN=env("PORTAL_EMAIL_DOMAIN", "adust.edu.bd").lower(),
    BASE_URL=env("PORTAL_BASE_URL", "").rstrip("/"),
)

if env("PORTAL_TRUST_PROXY") == "1":
    # Behind nginx/Apache: trust one X-Forwarded-For hop for the client IP.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"], timeout=10)
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
        db.executemany("INSERT INTO categories (name) VALUES (?)",
                       [(c,) for c in DEFAULT_CATEGORIES])
    db.commit()
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)


@app.cli.command("init-db")
def init_db_command():
    """Create the database tables and default categories."""
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


@app.before_request
def csrf_protect():
    if request.method == "POST":
        token = session.get("csrf_token")
        if not token or token != request.form.get("csrf_token"):
            abort(400, "Your session expired. Please reload the page and try again.")


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    return session["csrf_token"]


def is_it(user=None):
    user = user or g.user
    return user is not None and user["role"] in ("it", "admin")


def unread_count():
    if g.user is None:
        return 0
    return get_db().execute("SELECT COUNT(*) FROM notifications WHERE user_id = ? AND is_read = 0",
                            (g.user["id"],)).fetchone()[0]


app.jinja_env.globals.update(csrf_token=csrf_token, STATUSES=STATUSES, ROLES=ROLES,
                             is_it=is_it, unread_count=unread_count,
                             OPEN_STATUSES=OPEN_STATUSES)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login", next=request.path))
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
        if g.user["role"] != "admin":
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
        return date.fromisoformat(value).strftime("%d %b %Y")
    except ValueError:
        return value


@app.template_filter("due_state")
def due_state(ticket):
    if not ticket["due_date"] or ticket["status"] not in OPEN_STATUSES:
        return ""
    days = (date.fromisoformat(ticket["due_date"]) - date.today()).days
    return "overdue" if days < 0 else "soon" if days <= 1 else ""


@app.template_filter("prio")
def prio_band(p):
    return "high" if p >= 8 else "medium" if p >= 5 else "low"


# --------------------------------------------------------------------------
# Notifications (in-portal + optional email)
# --------------------------------------------------------------------------

def _send_email(to_addrs, subject, body):
    host = env("SMTP_HOST")
    if not host or not to_addrs:
        return
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
    except Exception as exc:  # never break a request because mail failed
        app.logger.warning("Email to %s failed: %s", to_addrs, exc)


def notify(user_ids, ticket, message):
    """Create in-portal notifications and email the recipients (in the background)."""
    user_ids = {u for u in user_ids if u and (g.user is None or u != g.user["id"])}
    if not user_ids:
        return
    db = get_db()
    ts = now()
    db.executemany(
        "INSERT INTO notifications (user_id, ticket_id, message, created_at) VALUES (?, ?, ?, ?)",
        [(u, ticket["id"], message, ts) for u in user_ids],
    )
    db.commit()
    marks = ",".join("?" * len(user_ids))
    emails = [r["email"] for r in db.execute(
        f"SELECT email FROM users WHERE active = 1 AND id IN ({marks})", tuple(user_ids))]
    link = f"{app.config['BASE_URL']}/tickets/{ticket['id']}" if app.config["BASE_URL"] else ""
    body = f"{message}\n\nRequest: {ticket['code']} — {ticket['title']}\n{link}\n\n— ADUST IT Section Portal"
    threading.Thread(target=_send_email, args=(emails, f"[{ticket['code']}] {message}", body),
                     daemon=True).start()


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


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("index"))
    if request.method == "POST":
        ident = request.form.get("identifier", "").strip().lower()
        password = request.form.get("password", "")
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE email = ? OR login_id = ?", (ident, ident)).fetchone()
        ok = bool(user and user["password_hash"] and check_password_hash(user["password_hash"], password))
        if not ok:
            # Fall back to the university webmail password.
            email = user["email"] if user else ident
            if in_university_domain(email) and webmail_login(email, password):
                ok = True
                if user is None:
                    db.execute(
                        "INSERT INTO users (name, email, role, created_at) VALUES (?, ?, 'user', ?)",
                        (email.split("@")[0].replace(".", " ").title(), email, now()))
                    db.commit()
                    user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
                    session["complete_profile"] = True
        if not ok:
            flash("Incorrect email / user ID or password.", "error")
        elif not user["active"]:
            flash("Your account is deactivated. Please contact the IT section.", "error")
        else:
            session.pop("csrf_token", None)
            complete = session.pop("complete_profile", False)
            session["user_id"] = user["id"]
            session.permanent = True
            db.execute("UPDATE users SET last_login = ? WHERE id = ?", (now(), user["id"]))
            db.commit()
            if complete:
                flash("Welcome! Please complete your profile.", "success")
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
            error = "Name, email and department are required."
        elif not in_university_domain(email):
            error = f"Please use your university email (@{app.config['EMAIL_DOMAIN']})."
        elif len(form.get("password", "")) < 8:
            error = "Password must be at least 8 characters."
        elif form.get("password") != form.get("confirm"):
            error = "Passwords do not match."
        if error is None:
            try:
                db = get_db()
                db.execute(
                    "INSERT INTO users (login_id, name, email, phone, department, designation, role, password_hash, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'user', ?, ?)",
                    (login_id, form["name"].strip(), email, form.get("phone", "").strip(),
                     form["department"].strip(), form.get("designation", "").strip(),
                     generate_password_hash(form["password"]), now()))
                db.commit()
                flash("Account created. Please sign in.", "success")
                return redirect(url_for("login"))
            except sqlite3.IntegrityError:
                error = "An account with that email or user ID already exists."
        flash(error, "error")
    return render_template("register.html", domain=app.config["EMAIL_DOMAIN"])


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been signed out.", "success")
    return redirect(url_for("login"))


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    db = get_db()
    if request.method == "POST":
        f = request.form
        if f.get("action") == "password":
            if g.user["password_hash"] and not check_password_hash(g.user["password_hash"], f.get("current", "")):
                flash("Current password is incorrect.", "error")
            elif len(f.get("new", "")) < 8 or f.get("new") != f.get("confirm"):
                flash("New password must be 8+ characters and match the confirmation.", "error")
            else:
                db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                           (generate_password_hash(f["new"]), g.user["id"]))
                db.commit()
                flash("Password updated.", "success")
        else:
            if not f.get("name", "").strip():
                flash("Name is required.", "error")
            else:
                try:
                    db.execute("UPDATE users SET name=?, login_id=?, phone=?, department=?, designation=? WHERE id=?",
                               (f["name"].strip(), f.get("login_id", "").strip().lower() or None,
                                f.get("phone", "").strip(), f.get("department", "").strip(),
                                f.get("designation", "").strip(), g.user["id"]))
                    db.commit()
                    flash("Profile saved.", "success")
                except sqlite3.IntegrityError:
                    flash("That user ID is already taken.", "error")
        return redirect(url_for("profile"))
    return render_template("profile.html")


# --------------------------------------------------------------------------
# Dashboard & ticket list
# --------------------------------------------------------------------------

TICKET_SELECT = """
    SELECT t.*, c.name AS category, r.name AS requester_name, r.department AS requester_dept,
           a.name AS assignee_name
    FROM tickets t
    LEFT JOIN categories c ON c.id = t.category_id
    JOIN users r ON r.id = t.requester_id
    LEFT JOIN users a ON a.id = t.assigned_to
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
        "in_progress": counts["in_progress"] + counts["on_hold"],
        "resolved": counts["resolved"] + counts["closed"],
    }
    if is_it():
        stats["mine"] = db.execute(
            f"SELECT COUNT(*) FROM tickets WHERE assigned_to = ? AND status IN {OPEN_STATUSES}",
            (g.user["id"],)).fetchone()[0]
        stats["unassigned"] = db.execute(
            f"SELECT COUNT(*) FROM tickets WHERE assigned_to IS NULL AND status IN {OPEN_STATUSES}"
        ).fetchone()[0]
        stats["overdue"] = db.execute(
            f"SELECT COUNT(*) FROM tickets WHERE status IN {OPEN_STATUSES} AND due_date < ?",
            (date.today().isoformat(),)).fetchone()[0]
    tickets = db.execute(
        f"{TICKET_SELECT} WHERE {scope} AND t.status IN {OPEN_STATUSES} {TICKET_ORDER} LIMIT 15", args
    ).fetchall()
    return render_template("dashboard.html", stats=stats, tickets=tickets)


@app.route("/tickets")
@login_required
def ticket_list():
    db = get_db()
    where, args = [], []
    if not is_it():
        where.append("t.requester_id = ?")
        args.append(g.user["id"])
    q = request.args
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
    if q.get("q", "").strip():
        term = f"%{q['q'].strip()}%"
        where.append("(t.code LIKE ? OR t.title LIKE ? OR t.description LIKE ? OR r.name LIKE ? OR r.department LIKE ?)")
        args += [term] * 5
    sql = f"{TICKET_SELECT} {'WHERE ' + ' AND '.join(where) if where else ''} {TICKET_ORDER} LIMIT 500"
    tickets = db.execute(sql, args).fetchall()
    return render_template("tickets.html", tickets=tickets, status=status,
                           categories=db.execute("SELECT * FROM categories ORDER BY name").fetchall(),
                           it_staff=it_staff())


def it_staff():
    return get_db().execute(
        "SELECT id, name, designation FROM users WHERE active = 1 AND role IN ('it','admin') ORDER BY name"
    ).fetchall()


# --------------------------------------------------------------------------
# Create & view tickets
# --------------------------------------------------------------------------

@app.route("/tickets/new", methods=["GET", "POST"])
@login_required
def new_ticket():
    db = get_db()
    categories = db.execute("SELECT * FROM categories WHERE active = 1 ORDER BY name").fetchall()
    staff = it_staff()
    if request.method == "POST":
        f = request.form
        title = f.get("title", "").strip()
        error = None
        try:
            priority = int(f.get("priority", "5"))
            category_id = int(f.get("category_id", ""))
            assigned_to = int(f["assigned_to"]) if f.get("assigned_to") else None
            due = f.get("due_date") or None
            if due:
                date.fromisoformat(due)
        except ValueError:
            priority = category_id = None
            error = "Please check the form values."
        if error is None:
            if not title:
                error = "Please give the work a short title."
            elif not 1 <= priority <= 10:
                error = "Priority must be between 1 and 10."
            elif category_id not in {c["id"] for c in categories}:
                error = "Please choose a category."
            elif assigned_to and assigned_to not in {s["id"] for s in staff}:
                error = "Invalid IT staff selection."
            elif due and due < date.today().isoformat():
                error = "The required-by date cannot be in the past."
        if error is None:
            upload = save_upload()
            if upload is False:
                error = "That attachment type is not allowed."
        if error:
            flash(error, "error")
            return render_template("new_ticket.html", categories=categories, staff=staff), 400
        ts = now()
        cur = db.execute(
            "INSERT INTO tickets (title, description, remarks, category_id, priority, urgent, due_date, location, "
            "requester_id, assigned_to, attachment_name, attachment_stored, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (title, f.get("description", "").strip(), f.get("remarks", "").strip(), category_id, priority,
             1 if f.get("urgent") else 0, due, f.get("location", "").strip(), g.user["id"], assigned_to,
             upload[0], upload[1], ts, ts))
        tid = cur.lastrowid
        code = f"IT-{date.today().year}-{tid:04d}"
        db.execute("UPDATE tickets SET code = ? WHERE id = ?", (code, tid))
        db.execute("INSERT INTO ticket_updates (ticket_id, user_id, event, created_at) VALUES (?, ?, ?, ?)",
                   (tid, g.user["id"], "Request submitted", ts))
        db.commit()
        ticket = db.execute("SELECT * FROM tickets WHERE id = ?", (tid,)).fetchone()
        tag = "URGENT " if ticket["urgent"] else ""
        notify(it_recipients(ticket), ticket,
               f"New {tag}request from {g.user['name']} ({g.user['department'] or 'N/A'}): {title}")
        flash(f"Your request {code} has been sent to the IT section.", "success")
        return redirect(url_for("ticket_detail", ticket_id=tid))
    return render_template("new_ticket.html", categories=categories, staff=staff)


def load_ticket(ticket_id):
    t = get_db().execute(f"{TICKET_SELECT} WHERE t.id = ?", (ticket_id,)).fetchone()
    if t is None:
        abort(404)
    if not is_it() and t["requester_id"] != g.user["id"]:
        abort(403)
    return t


@app.route("/tickets/<int:ticket_id>")
@login_required
def ticket_detail(ticket_id):
    ticket = load_ticket(ticket_id)
    db = get_db()
    updates = db.execute(
        "SELECT u.*, us.name AS user_name, us.role AS user_role FROM ticket_updates u "
        "JOIN users us ON us.id = u.user_id WHERE u.ticket_id = ? "
        + ("" if is_it() else "AND u.internal = 0 ") + "ORDER BY u.id", (ticket_id,)).fetchall()
    db.execute("UPDATE notifications SET is_read = 1 WHERE user_id = ? AND ticket_id = ?",
               (g.user["id"], ticket_id))
    db.commit()
    return render_template("ticket.html", t=ticket, updates=updates, staff=it_staff(),
                           it_statuses=IT_SETTABLE)


@app.route("/tickets/<int:ticket_id>/update", methods=["POST"])
@login_required
def ticket_update(ticket_id):
    """One endpoint for every change: comments, IT status/assignment/priority
    updates, and requester actions (cancel, confirm resolved, reopen)."""
    t = load_ticket(ticket_id)
    db = get_db()
    f = request.form
    body = f.get("body", "").strip()
    events = []
    changes = {}

    if is_it():
        new_status = f.get("status", t["status"])
        if new_status != t["status"] and new_status in IT_SETTABLE:
            changes["status"] = new_status
            events.append(f"Status: {STATUSES[t['status']]} → {STATUSES[new_status]}")
        assignee = f.get("assigned_to", "")
        new_assignee = int(assignee) if assignee.isdigit() else None
        if "assigned_to" in f and new_assignee != t["assigned_to"]:
            if new_assignee and new_assignee not in {s["id"] for s in it_staff()}:
                abort(400)
            changes["assigned_to"] = new_assignee
            name = next((s["name"] for s in it_staff() if s["id"] == new_assignee), "Unassigned")
            events.append(f"Assigned to {name}")
        if f.get("priority", "").isdigit() and 1 <= int(f["priority"]) <= 10 and int(f["priority"]) != t["priority"]:
            changes["priority"] = int(f["priority"])
            events.append(f"Priority: {t['priority']} → {f['priority']}")
    else:
        action = f.get("action", "")
        if action == "cancel" and t["status"] == "new":
            changes["status"] = "cancelled"
            events.append("Cancelled by requester")
        elif action == "confirm" and t["status"] == "resolved":
            changes["status"] = "closed"
            events.append("Requester confirmed the work is done")
        elif action == "reopen" and t["status"] in ("resolved", "closed"):
            changes["status"] = "in_progress" if t["assigned_to"] else "new"
            events.append("Reopened by requester")
        elif t["status"] in ("closed", "cancelled", "rejected") and body:
            flash("This request is closed. Reopen it or submit a new request.", "error")
            return redirect(url_for("ticket_detail", ticket_id=ticket_id))

    upload = save_upload()
    if upload is False:
        flash("That attachment type is not allowed.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))
    if not body and not events and not upload[0]:
        flash("Nothing to update.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))

    internal = 1 if (is_it() and f.get("internal") and not events) else 0
    ts = now()
    if "status" in changes:
        changes["resolved_at"] = ts if changes["status"] in ("resolved", "closed") else None
    changes["updated_at"] = ts
    db.execute(f"UPDATE tickets SET {', '.join(k + ' = ?' for k in changes)} WHERE id = ?",
               (*changes.values(), ticket_id))
    db.execute(
        "INSERT INTO ticket_updates (ticket_id, user_id, body, event, internal, attachment_name, attachment_stored, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ticket_id, g.user["id"], body, "; ".join(events), internal, upload[0], upload[1], ts))
    db.commit()

    t = db.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    summary = "; ".join(events) or "New comment"
    if internal:
        notify(it_recipients(t), t, f"Internal note by {g.user['name']}")
    elif is_it():
        notify([t["requester_id"]], t, f"IT update on your request: {summary}")
        if "assigned_to" in changes and changes["assigned_to"]:
            notify([changes["assigned_to"]], t, f"{g.user['name']} assigned a request to you: {t['title']}")
    else:
        notify(it_recipients(t), t, f"{g.user['name']}: {summary}")
    flash("Update saved.", "success")
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
# Admin
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
            flash("Name, email and a valid role are required.", "error")
            return redirect(url_for("admin_users"))
        fields = (f.get("login_id", "").strip().lower() or None, f["name"].strip(), email,
                  f.get("phone", "").strip(), f.get("department", "").strip(),
                  f.get("designation", "").strip(), role)
        password = f.get("password", "")
        if password and len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
            return redirect(url_for("admin_users"))
        try:
            if uid.isdigit():
                if int(uid) == g.user["id"] and role != "admin":
                    flash("You cannot remove your own admin role.", "error")
                    return redirect(url_for("admin_users"))
                db.execute("UPDATE users SET login_id=?, name=?, email=?, phone=?, department=?, designation=?, role=? "
                           "WHERE id=?", (*fields, int(uid)))
                if password:
                    db.execute("UPDATE users SET password_hash=? WHERE id=?",
                               (generate_password_hash(password), int(uid)))
                flash("User updated.", "success")
            else:
                db.execute("INSERT INTO users (login_id, name, email, phone, department, designation, role, "
                           "password_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (*fields, generate_password_hash(password) if password else None, now()))
                flash(f"Account for {fields[1]} created.", "success")
            db.commit()
        except sqlite3.IntegrityError:
            flash("Email or user ID already in use.", "error")
        return redirect(url_for("admin_users"))
    edit = None
    if request.args.get("edit", "").isdigit():
        edit = db.execute("SELECT * FROM users WHERE id = ?", (int(request.args["edit"]),)).fetchone()
    users = db.execute("SELECT * FROM users ORDER BY CASE role WHEN 'admin' THEN 0 WHEN 'it' THEN 1 ELSE 2 END, name").fetchall()
    return render_template("admin/users.html", users=users, edit=edit)


@app.route("/admin/users/<int:user_id>/toggle", methods=["POST"])
@admin_required
def admin_toggle_user(user_id):
    if user_id == g.user["id"]:
        flash("You cannot deactivate your own account.", "error")
    else:
        db = get_db()
        db.execute("UPDATE users SET active = 1 - active WHERE id = ?", (user_id,))
        db.commit()
    return redirect(url_for("admin_users"))


@app.route("/admin/categories", methods=["GET", "POST"])
@admin_required
def admin_categories():
    db = get_db()
    if request.method == "POST":
        f = request.form
        if f.get("toggle", "").isdigit():
            db.execute("UPDATE categories SET active = 1 - active WHERE id = ?", (int(f["toggle"]),))
        elif f.get("name", "").strip():
            try:
                db.execute("INSERT INTO categories (name) VALUES (?)", (f["name"].strip(),))
                flash("Category added.", "success")
            except sqlite3.IntegrityError:
                flash("That category already exists.", "error")
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
                flash("Removing that network would have locked you out, so your current IP was kept.", "error")
            else:
                flash("Network removed.", "success")
        else:
            try:
                net = ipaddress.ip_network(f.get("cidr", "").strip(), strict=False)
            except ValueError:
                flash("Enter a valid IP or network, e.g. 103.110.10.0/24", "error")
                return redirect(url_for("admin_network"))
            first_rule = not allowed_networks()
            db.execute("INSERT OR IGNORE INTO allowed_networks (cidr, label) VALUES (?, ?)",
                       (str(net), f.get("label", "").strip()))
            if first_rule and not ip_allowed(g.client_ip):
                # Activating the restriction from outside it would lock the admin out.
                db.execute("INSERT OR IGNORE INTO allowed_networks (cidr, label) VALUES (?, ?)",
                           (str(ipaddress.ip_network(g.client_ip)), "auto-added: your current IP"))
                flash("Your current IP was also added so you are not locked out.", "success")
            db.commit()
            flash(f"{net} allowed.", "success")
        return redirect(url_for("admin_network"))
    rows = db.execute("SELECT * FROM allowed_networks ORDER BY id").fetchall()
    env_nets = [n.strip() for n in (env("PORTAL_ALLOWED_NETWORKS") or "").split(",") if n.strip()]
    return render_template("admin/network.html", rows=rows, env_nets=env_nets)


@app.route("/admin/reports")
@it_required
def admin_reports():
    db = get_db()
    by_status = db.execute("SELECT status, COUNT(*) n FROM tickets GROUP BY status").fetchall()
    by_category = db.execute(
        "SELECT c.name, COUNT(t.id) total, "
        f"SUM(t.status IN {OPEN_STATUSES}) open, SUM(t.status IN ('resolved','closed')) done "
        "FROM categories c LEFT JOIN tickets t ON t.category_id = c.id GROUP BY c.id HAVING total > 0 ORDER BY total DESC"
    ).fetchall()
    by_staff = db.execute(
        "SELECT u.name, COUNT(t.id) total, "
        f"SUM(t.status IN {OPEN_STATUSES}) open, SUM(t.status IN ('resolved','closed')) done, "
        "ROUND(AVG(CASE WHEN t.resolved_at IS NOT NULL THEN "
        "(julianday(t.resolved_at) - julianday(t.created_at)) * 24 END), 1) avg_hours "
        "FROM users u LEFT JOIN tickets t ON t.assigned_to = u.id "
        "WHERE u.role IN ('it','admin') GROUP BY u.id ORDER BY open DESC, total DESC"
    ).fetchall()
    by_dept = db.execute(
        "SELECT COALESCE(NULLIF(r.department,''),'—') dept, COUNT(*) n FROM tickets t "
        "JOIN users r ON r.id = t.requester_id GROUP BY dept ORDER BY n DESC LIMIT 10").fetchall()
    return render_template("admin/reports.html", by_status=by_status, by_category=by_category,
                           by_staff=by_staff, by_dept=by_dept)


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
    msg = err.description if err.code == 400 else messages[err.code]
    return render_template("error.html", code=err.code, message=msg), err.code


with app.app_context():
    init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(env("PORT", "5000")), debug=env("FLASK_DEBUG") == "1")
