"""ADUST IT Section — Work Submission Portal.

A small Flask application where the IT section (admins) assigns work to
staff/students (members), members submit their work with an attached file,
and admins review each submission (approve or request revision).
"""

import os
import secrets
import sqlite3
import uuid
from datetime import date, datetime
from functools import wraps

import click
from flask import (
    Flask, abort, flash, g, redirect, render_template, request,
    send_from_directory, session, url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

ALLOWED_EXTENSIONS = {
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv",
    "zip", "rar", "7z", "png", "jpg", "jpeg", "sql", "py", "java", "c",
    "cpp", "js", "html", "css", "json",
}

STATUS_LABELS = {
    "submitted": "Pending review",
    "approved": "Approved",
    "needs_revision": "Needs revision",
}

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("PORTAL_SECRET_KEY") or secrets.token_hex(32),
    DATABASE=os.environ.get("PORTAL_DATABASE", os.path.join(BASE_DIR, "portal.db")),
    UPLOAD_FOLDER=os.environ.get("PORTAL_UPLOADS", os.path.join(BASE_DIR, "uploads")),
    MAX_CONTENT_LENGTH=25 * 1024 * 1024,  # 25 MB per upload
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
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
    db.commit()


@app.cli.command("init-db")
def init_db_command():
    """Create the database tables."""
    init_db()
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    click.echo("Database initialised.")


@app.cli.command("create-admin")
@click.option("--name", prompt=True)
@click.option("--email", prompt=True)
@click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
def create_admin_command(name, email, password):
    """Create an IT-section administrator account."""
    init_db()
    db = get_db()
    try:
        db.execute(
            "INSERT INTO users (name, email, department, role, password_hash) "
            "VALUES (?, ?, 'IT Section', 'admin', ?)",
            (name.strip(), email.strip().lower(), generate_password_hash(password)),
        )
        db.commit()
    except sqlite3.IntegrityError:
        raise click.ClickException("A user with that email already exists.")
    click.echo(f"Admin {email} created.")


# --------------------------------------------------------------------------
# Auth helpers & CSRF
# --------------------------------------------------------------------------

@app.before_request
def load_user():
    g.user = None
    uid = session.get("user_id")
    if uid is not None:
        user = get_db().execute(
            "SELECT * FROM users WHERE id = ? AND active = 1", (uid,)
        ).fetchone()
        if user is None:
            session.clear()
        g.user = user


@app.before_request
def csrf_protect():
    if request.method == "POST":
        token = session.get("csrf_token")
        if not token or token != request.form.get("csrf_token"):
            abort(400, "Invalid or missing CSRF token. Please reload the page.")


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    return session["csrf_token"]


app.jinja_env.globals["csrf_token"] = csrf_token
app.jinja_env.globals["STATUS_LABELS"] = STATUS_LABELS


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login", next=request.path))
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


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


@app.template_filter("due_state")
def due_state(due_date):
    """Return 'overdue', 'soon' or '' for a YYYY-MM-DD due date."""
    if not due_date:
        return ""
    days = (date.fromisoformat(due_date) - date.today()).days
    if days < 0:
        return "overdue"
    if days <= 2:
        return "soon"
    return ""


@app.template_filter("dt")
def format_dt(value):
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value).strftime("%d %b %Y, %H:%M")
    except ValueError:
        return value


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------

@app.route("/")
def index():
    if g.user is None:
        return redirect(url_for("login"))
    if g.user["role"] == "admin":
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("dashboard"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Incorrect email or password.", "error")
        elif not user["active"]:
            flash("Your account has been deactivated. Contact the IT section.", "error")
        else:
            session.clear()
            session["user_id"] = user["id"]
            nxt = request.args.get("next", "")
            if nxt.startswith("/") and not nxt.startswith("//"):
                return redirect(nxt)
            return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        department = request.form.get("department", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        error = None
        if not name or not email or not department:
            error = "Name, email and department are required."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        elif password != confirm:
            error = "Passwords do not match."
        if error is None:
            try:
                db = get_db()
                db.execute(
                    "INSERT INTO users (name, email, department, role, password_hash) "
                    "VALUES (?, ?, ?, 'member', ?)",
                    (name, email, department, generate_password_hash(password)),
                )
                db.commit()
                flash("Account created. You can now sign in.", "success")
                return redirect(url_for("login"))
            except sqlite3.IntegrityError:
                error = "An account with that email already exists."
        flash(error, "error")
    return render_template("register.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been signed out.", "success")
    return redirect(url_for("login"))


@app.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current = request.form.get("current", "")
        new = request.form.get("new", "")
        if not check_password_hash(g.user["password_hash"], current):
            flash("Current password is incorrect.", "error")
        elif len(new) < 8:
            flash("New password must be at least 8 characters.", "error")
        elif new != request.form.get("confirm", ""):
            flash("New passwords do not match.", "error")
        else:
            db = get_db()
            db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                       (generate_password_hash(new), g.user["id"]))
            db.commit()
            flash("Password updated.", "success")
            return redirect(url_for("index"))
    return render_template("change_password.html")


# --------------------------------------------------------------------------
# Member views
# --------------------------------------------------------------------------

LATEST_SUBMISSION_SQL = """
    SELECT s.* FROM submissions s
    WHERE s.id = (SELECT MAX(id) FROM submissions
                  WHERE assignment_id = ? AND user_id = ?)
"""


@app.route("/dashboard")
@login_required
def dashboard():
    if g.user["role"] == "admin":
        return redirect(url_for("admin_dashboard"))
    rows = get_db().execute(
        """
        SELECT a.*, s.status AS latest_status, s.submitted_at AS latest_at
        FROM assignments a
        JOIN assignment_users au ON au.assignment_id = a.id AND au.user_id = ?
        LEFT JOIN submissions s ON s.id = (
            SELECT MAX(id) FROM submissions
            WHERE assignment_id = a.id AND user_id = au.user_id)
        ORDER BY (a.due_date IS NULL), a.due_date, a.id DESC
        """,
        (g.user["id"],),
    ).fetchall()
    counts = {"total": len(rows), "pending": 0, "submitted": 0, "approved": 0, "needs_revision": 0}
    for r in rows:
        counts[r["latest_status"] or "pending"] += 1
    return render_template("dashboard.html", assignments=rows, counts=counts)


def get_assignment_for_member(assignment_id):
    a = get_db().execute(
        """SELECT a.*, u.name AS creator_name FROM assignments a
           JOIN assignment_users au ON au.assignment_id = a.id
           JOIN users u ON u.id = a.created_by
           WHERE a.id = ? AND au.user_id = ?""",
        (assignment_id, g.user["id"]),
    ).fetchone()
    if a is None:
        abort(404)
    return a


@app.route("/assignments/<int:assignment_id>", methods=["GET", "POST"])
@login_required
def assignment_detail(assignment_id):
    if g.user["role"] == "admin":
        return redirect(url_for("admin_assignment", assignment_id=assignment_id))
    assignment = get_assignment_for_member(assignment_id)
    db = get_db()
    latest = db.execute(LATEST_SUBMISSION_SQL, (assignment_id, g.user["id"])).fetchone()

    if request.method == "POST":
        if latest is not None and latest["status"] == "approved":
            flash("This work has already been approved; no further submissions are needed.", "error")
            return redirect(request.url)
        note = request.form.get("note", "").strip()
        upload = request.files.get("file")
        original = stored = None
        if upload and upload.filename:
            if not allowed_file(upload.filename):
                flash("That file type is not allowed.", "error")
                return redirect(request.url)
            original = secure_filename(upload.filename) or "file"
            stored = f"{uuid.uuid4().hex}_{original}"
            os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
            upload.save(os.path.join(app.config["UPLOAD_FOLDER"], stored))
        if not note and not stored:
            flash("Attach a file or write a note describing your work.", "error")
            return redirect(request.url)
        db.execute(
            "INSERT INTO submissions (assignment_id, user_id, note, original_filename, stored_filename) "
            "VALUES (?, ?, ?, ?, ?)",
            (assignment_id, g.user["id"], note, original, stored),
        )
        db.commit()
        flash("Your work has been submitted to the IT section.", "success")
        return redirect(request.url)

    history = db.execute(
        "SELECT * FROM submissions WHERE assignment_id = ? AND user_id = ? ORDER BY id DESC",
        (assignment_id, g.user["id"]),
    ).fetchall()
    return render_template("assignment.html", assignment=assignment,
                           latest=latest, history=history,
                           allowed=", ".join(sorted(ALLOWED_EXTENSIONS)))


@app.route("/files/<int:submission_id>")
@login_required
def download(submission_id):
    sub = get_db().execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone()
    if sub is None or not sub["stored_filename"]:
        abort(404)
    if g.user["role"] != "admin" and sub["user_id"] != g.user["id"]:
        abort(403)
    return send_from_directory(app.config["UPLOAD_FOLDER"], sub["stored_filename"],
                               as_attachment=True, download_name=sub["original_filename"])


# --------------------------------------------------------------------------
# Admin (IT section) views
# --------------------------------------------------------------------------

@app.route("/admin")
@admin_required
def admin_dashboard():
    db = get_db()
    assignments = db.execute(
        """
        SELECT a.*,
            (SELECT COUNT(*) FROM assignment_users WHERE assignment_id = a.id) AS assigned,
            (SELECT COUNT(DISTINCT user_id) FROM submissions WHERE assignment_id = a.id) AS submitted,
            (SELECT COUNT(*) FROM submissions s WHERE s.assignment_id = a.id
                AND s.status = 'submitted'
                AND s.id = (SELECT MAX(id) FROM submissions
                            WHERE assignment_id = a.id AND user_id = s.user_id)) AS to_review
        FROM assignments a ORDER BY a.id DESC
        """
    ).fetchall()
    stats = {
        "members": db.execute("SELECT COUNT(*) FROM users WHERE role='member' AND active=1").fetchone()[0],
        "assignments": len(assignments),
        "to_review": sum(a["to_review"] for a in assignments),
    }
    recent = db.execute(
        """SELECT s.*, u.name AS user_name, a.title FROM submissions s
           JOIN users u ON u.id = s.user_id JOIN assignments a ON a.id = s.assignment_id
           ORDER BY s.id DESC LIMIT 8"""
    ).fetchall()
    return render_template("admin/dashboard.html", assignments=assignments,
                           stats=stats, recent=recent)


def active_members():
    return get_db().execute(
        "SELECT * FROM users WHERE role = 'member' AND active = 1 ORDER BY department, name"
    ).fetchall()


def save_assignment_form(assignment_id=None):
    """Validate and persist the create/edit assignment form. Returns id or None."""
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    due_date = request.form.get("due_date", "").strip() or None
    member_ids = {int(x) for x in request.form.getlist("members") if x.isdigit()}
    if not title:
        flash("Title is required.", "error")
        return None
    if due_date:
        try:
            date.fromisoformat(due_date)
        except ValueError:
            flash("Invalid due date.", "error")
            return None
    if not member_ids:
        flash("Assign the work to at least one person.", "error")
        return None
    db = get_db()
    if assignment_id is None:
        cur = db.execute(
            "INSERT INTO assignments (title, description, due_date, created_by) VALUES (?, ?, ?, ?)",
            (title, description, due_date, g.user["id"]),
        )
        assignment_id = cur.lastrowid
    else:
        db.execute("UPDATE assignments SET title=?, description=?, due_date=? WHERE id=?",
                   (title, description, due_date, assignment_id))
        db.execute("DELETE FROM assignment_users WHERE assignment_id = ?", (assignment_id,))
    db.executemany("INSERT INTO assignment_users (assignment_id, user_id) VALUES (?, ?)",
                   [(assignment_id, m) for m in member_ids])
    db.commit()
    return assignment_id


@app.route("/admin/assignments/new", methods=["GET", "POST"])
@admin_required
def admin_new_assignment():
    if request.method == "POST":
        new_id = save_assignment_form()
        if new_id:
            flash("Work assigned.", "success")
            return redirect(url_for("admin_assignment", assignment_id=new_id))
    return render_template("admin/assignment_form.html", assignment=None,
                           members=active_members(), selected=set())


@app.route("/admin/assignments/<int:assignment_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_edit_assignment(assignment_id):
    db = get_db()
    assignment = db.execute("SELECT * FROM assignments WHERE id = ?", (assignment_id,)).fetchone()
    if assignment is None:
        abort(404)
    if request.method == "POST":
        if save_assignment_form(assignment_id):
            flash("Assignment updated.", "success")
            return redirect(url_for("admin_assignment", assignment_id=assignment_id))
    selected = {r[0] for r in db.execute(
        "SELECT user_id FROM assignment_users WHERE assignment_id = ?", (assignment_id,))}
    return render_template("admin/assignment_form.html", assignment=assignment,
                           members=active_members(), selected=selected)


@app.route("/admin/assignments/<int:assignment_id>/delete", methods=["POST"])
@admin_required
def admin_delete_assignment(assignment_id):
    db = get_db()
    files = db.execute(
        "SELECT stored_filename FROM submissions WHERE assignment_id = ? AND stored_filename IS NOT NULL",
        (assignment_id,),
    ).fetchall()
    db.execute("DELETE FROM assignments WHERE id = ?", (assignment_id,))
    db.commit()
    for f in files:
        try:
            os.remove(os.path.join(app.config["UPLOAD_FOLDER"], f[0]))
        except OSError:
            pass
    flash("Assignment deleted.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/assignments/<int:assignment_id>")
@admin_required
def admin_assignment(assignment_id):
    db = get_db()
    assignment = db.execute(
        "SELECT a.*, u.name AS creator_name FROM assignments a "
        "JOIN users u ON u.id = a.created_by WHERE a.id = ?", (assignment_id,)
    ).fetchone()
    if assignment is None:
        abort(404)
    people = db.execute(
        """
        SELECT u.id, u.name, u.email, u.department,
               s.id AS sub_id, s.status, s.submitted_at,
               (SELECT COUNT(*) FROM submissions WHERE assignment_id = ? AND user_id = u.id) AS attempts
        FROM assignment_users au
        JOIN users u ON u.id = au.user_id
        LEFT JOIN submissions s ON s.id = (
            SELECT MAX(id) FROM submissions WHERE assignment_id = ? AND user_id = u.id)
        WHERE au.assignment_id = ?
        ORDER BY (s.status = 'submitted') DESC, u.name
        """,
        (assignment_id, assignment_id, assignment_id),
    ).fetchall()
    return render_template("admin/assignment.html", assignment=assignment, people=people)


@app.route("/admin/submissions/<int:submission_id>", methods=["GET", "POST"])
@admin_required
def admin_review(submission_id):
    db = get_db()
    sub = db.execute(
        """SELECT s.*, u.name AS user_name, u.email, u.department, a.title, a.id AS a_id
           FROM submissions s JOIN users u ON u.id = s.user_id
           JOIN assignments a ON a.id = s.assignment_id WHERE s.id = ?""",
        (submission_id,),
    ).fetchone()
    if sub is None:
        abort(404)
    if request.method == "POST":
        status = request.form.get("status")
        if status not in ("approved", "needs_revision"):
            abort(400)
        db.execute(
            "UPDATE submissions SET status=?, feedback=?, reviewed_at=datetime('now'), reviewed_by=? "
            "WHERE id=?",
            (status, request.form.get("feedback", "").strip(), g.user["id"], submission_id),
        )
        db.commit()
        flash(f"Submission marked as {STATUS_LABELS[status].lower()}.", "success")
        return redirect(url_for("admin_assignment", assignment_id=sub["a_id"]))
    history = db.execute(
        "SELECT * FROM submissions WHERE assignment_id = ? AND user_id = ? ORDER BY id DESC",
        (sub["assignment_id"], sub["user_id"]),
    ).fetchall()
    return render_template("admin/review.html", sub=sub, history=history)


@app.route("/admin/users", methods=["GET", "POST"])
@admin_required
def admin_users():
    db = get_db()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        department = request.form.get("department", "").strip()
        role = request.form.get("role", "member")
        password = request.form.get("password", "")
        if role not in ("admin", "member") or not name or not email or len(password) < 8:
            flash("Fill in every field; password must be at least 8 characters.", "error")
        else:
            try:
                db.execute(
                    "INSERT INTO users (name, email, department, role, password_hash) VALUES (?, ?, ?, ?, ?)",
                    (name, email, department, role, generate_password_hash(password)),
                )
                db.commit()
                flash(f"Account for {name} created.", "success")
            except sqlite3.IntegrityError:
                flash("An account with that email already exists.", "error")
        return redirect(url_for("admin_users"))
    users = db.execute("SELECT * FROM users ORDER BY role, department, name").fetchall()
    return render_template("admin/users.html", users=users)


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


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(400)
@app.errorhandler(413)
def http_error(err):
    messages = {
        400: "Bad request.",
        403: "You do not have permission to view this page.",
        404: "Page not found.",
        413: "The file is too large (max 25 MB).",
    }
    return render_template("error.html", code=err.code,
                           message=getattr(err, "description", None) if err.code == 400
                           else messages[err.code]), err.code


with app.app_context():
    init_db()

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
