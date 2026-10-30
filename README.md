# ADUST IT Section — Work Submission Portal

A web portal where the **IT section** of Atish Dipankar University of Science and
Technology assigns work to staff/students, and they **submit the completed work**
(files and notes) for review.

## Features

**Members (staff / students)**
- Register and sign in
- Dashboard of assigned work with due dates, overdue warnings and status counts
- Submit work: file upload (up to 25 MB) and/or a written note
- See IT feedback and resubmit when a revision is requested; full version history

**IT section (admins)**
- Assign work with a title, instructions and due date to one, several or all members
- Per-assignment progress: who submitted, who hasn't, and how many need review
- Review each submission: download the file, then **Approve** or **Request revision** with feedback
- Manage users: create accounts (members or admins), deactivate or reactivate them

**Security:** hashed passwords, CSRF protection on every form, role-based access,
file-type allow-list, and members can only download their own files.

## Run it

```bash
pip install -r requirements.txt
flask --app app create-admin          # creates the database + first IT admin
flask --app app run                   # http://127.0.0.1:5000
```

Members register at `/register`, or an admin creates their accounts under **Users**.

### Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `PORTAL_SECRET_KEY` | random per start | Session signing key. **Set this in production** or users are logged out on every restart. |
| `PORTAL_DATABASE` | `./portal.db` | SQLite database file |
| `PORTAL_UPLOADS` | `./uploads` | Where submitted files are stored |

### Production

Run behind a WSGI server and a reverse proxy over HTTPS, for example:

```bash
pip install gunicorn
PORTAL_SECRET_KEY=change-me gunicorn -w 4 -b 0.0.0.0:8000 app:app
```

Back up `portal.db` and the `uploads/` folder together.

## Project layout

```
app.py              routes, auth, database helpers, CLI commands
schema.sql          database tables
templates/          member pages and templates/admin/ IT pages
static/style.css    responsive styling
```
