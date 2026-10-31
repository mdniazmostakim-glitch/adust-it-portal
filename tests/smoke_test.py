"""End-to-end check of every portal feature using Flask's test client.

Run from the project folder:  python3 tests/smoke_test.py
Uses a throw-away database; email/SMS/webmail are faked.
"""
import io, os, sys, tempfile, time, zipfile
from datetime import date, timedelta
d = tempfile.mkdtemp()
os.environ.update(PORTAL_DATABASE=os.path.join(d, "t.db"), PORTAL_UPLOADS=os.path.join(d, "up"),
                  PORTAL_BACKUPS=os.path.join(d, "bk"), PORTAL_IMAP_HOST="imap.example",
                  SMTP_HOST="smtp.example", SMS_API_URL="http://sms.example/api?to={phone}&msg={message}",
                  PORTAL_AUTO_HOUSEKEEPING="0")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A
from app import app
emails, sms = [], []
A.send_email = lambda to, subj, body: emails.append((sorted(to), subj, body))
A.send_sms = lambda phones, text: sms.append((sorted(p for p in phones if p), text))
A.send_email_now = lambda to, subj, body: emails.append((sorted(to), subj, body)) or None
A.webmail_login = lambda e, p: p == "webmailpw"
app.test_cli_runner().invoke(args=["create-admin", "--name", "Kamal Hossain", "--email", "itadmin@adust.edu.bd", "--login-id", "it001", "--password", "adminpass1"])

def client(ip="10.20.0.5"):
    c = app.test_client(); c.environ_base["REMOTE_ADDR"] = ip; return c
def tok(c):
    with c.session_transaction() as s: s.setdefault("csrf_token", "t"); return s["csrf_token"]
def post(c, url, data=None, **kw):
    data = dict(data or {}); data["csrf_token"] = tok(c); return c.post(url, data=data, follow_redirects=True, **kw)
def login(c, ident, pw):
    return post(c, "/login", {"identifier": ident, "password": pw})
def ok(r, *needles):
    for n in needles:
        assert (n.encode() if isinstance(n, str) else n) in r.data, (n, r.status_code, r.data[:3000].decode(errors="replace"))
    return r
def q(sql, *a):
    with app.app_context(): return A.get_db().execute(sql, a).fetchall()

admin = client(); login(admin, "it001", "adminpass1")
# ---------- users: IT staff, department head, requesters
post(admin, "/admin/users", {"name": "Rafiq Islam", "email": "rafiq@adust.edu.bd", "role": "it", "department": "IT Section", "phone": "01711000001", "password": "itstaff123"})
post(admin, "/admin/users", {"name": "Dr. Head CSE", "email": "head@adust.edu.bd", "role": "user", "department": "Computer Science & Engineering (CSE)", "approver_title": "chairman", "password": "headpass1"})
post(admin, "/admin/users", {"name": "Admin Phone", "email": "itadmin@adust.edu.bd", "id": "1", "role": "admin", "phone": "01711000000", "department": "IT Section"})
it = client(); login(it, "rafiq@adust.edu.bd", "itstaff123")
head = client(); login(head, "head@adust.edu.bd", "headpass1")
u = client()
ok(post(u, "/register", {"name": "Mim Akter", "email": "mim@adust.edu.bd", "department": "Computer Science & Engineering (CSE)", "phone": "01811000000", "password": "secret123", "confirm": "secret123"}), "will verify")

# ---------- new accounts wait for admin verification
def uid(email): return q("SELECT id FROM users WHERE email=?", email)[0][0]
def approve(email, **extra):
    return post(admin, f"/admin/users/{uid(email)}/verify", {"decision": "approve", "role": "user", **extra})
ok(login(u, "mim@adust.edu.bd", "secret123"), "waiting for verification")
assert u.get("/").status_code == 302                                     # still signed out
ok(admin.get("/"), "1 new account(s) waiting for verification")
ok(admin.get("/admin/users"), "New accounts waiting for verification", "Mim Akter")
ok(admin.get("/notifications"), "New account to verify: Mim Akter")
assert any(e[0] == ["itadmin@adust.edu.bd"] and "verification" in e[1] for e in emails), emails
emails.clear()
ok(approve("mim@adust.edu.bd"), "Mim Akter can now sign in")
assert any(e[0] == ["mim@adust.edu.bd"] and "approved" in e[1] for e in emails), emails
bad = client(); post(bad, "/register", {"name": "Fake Person", "email": "fake@gmail.com", "department": "Library", "password": "secret123", "confirm": "secret123"})
ok(post(admin, f"/admin/users/{uid('fake@gmail.com')}/verify", {"decision": "reject"}), "rejected and removed")
assert not q("SELECT 1 FROM users WHERE email='fake@gmail.com'")
assert it.post(f"/admin/users/1/verify", data={"csrf_token": tok(it), "decision": "approve"}).status_code == 403
print("account verification ok")
ok(login(u, "mim@adust.edu.bd", "secret123"), "Welcome, Mim")

# ---------- any email (personal or institutional) can register and sign in
g = client()
ok(post(g, "/register", {"name": "Personal Mail", "email": "someone.personal@gmail.com", "department": "Library", "password": "secret123", "confirm": "secret123"}), "Account created")
approve("someone.personal@gmail.com")
ok(login(g, "someone.personal@gmail.com", "secret123"), "Welcome, Personal")
ok(post(client(), "/register", {"name": "Bad", "email": "not-an-email", "department": "Library", "password": "secret123", "confirm": "secret123"}), "valid email")
app.config["ALLOWED_EMAIL_DOMAINS"] = ["adust.edu.bd"]
ok(post(client(), "/register", {"name": "Y", "email": "y@yahoo.com", "department": "Library", "password": "secret123", "confirm": "secret123"}), "@adust.edu.bd")
app.config["ALLOWED_EMAIL_DOMAINS"] = []
print("any-email ok")

# ---------- 1. lock-out after 5 failures
x = client("10.20.0.77")
for i in range(5): login(x, "mim@adust.edu.bd", "wrong")
r = x.post("/login", data={"identifier": "mim@adust.edu.bd", "password": "secret123", "csrf_token": tok(x)})
assert r.status_code == 429 and b"Too many failed attempts" in r.data
print("lockout ok")
r = app.test_cli_runner().invoke(args=["reset-password", "--user", "MIM@adust.edu.bd", "--password", "fresh12345"])
assert "Password updated" in r.output, r.output                         # also clears the lock-out
ok(login(client("10.20.0.77"), "mim@adust.edu.bd", "fresh12345"), "Welcome, Mim")
assert "No account" in app.test_cli_runner().invoke(args=["reset-password", "--user", "nobody", "--password", "fresh12345"]).output
post(client("10.20.0.77"), "/forgot", {"identifier": "x"})                # keep the rest of the test independent
r = app.test_cli_runner().invoke(args=["reset-password", "--user", "mim@adust.edu.bd", "--password", "secret123"])
print("reset-password command ok")

# ---------- 2. password reset
emails.clear()
ok(post(client(), "/forgot", {"identifier": "mim@adust.edu.bd"}), "reset link has been sent")
link = [e for e in emails if e[0] == ["mim@adust.edu.bd"]][0][2]
path = link[link.index("/reset/"):].split()[0]
r2 = client()
ok(post(r2, path, {"new": "newpass123", "confirm": "newpass123"}), "Password updated")
ok(post(r2, path, {"new": "x" * 9, "confirm": "x" * 9}), "invalid or has expired")   # one-time link
ok(login(client(), "mim@adust.edu.bd", "newpass123"), "Welcome, Mim")
ok(post(client(), "/forgot", {"identifier": "nobody@adust.edu.bd"}), "reset link has been sent")  # no account leak
print("reset ok")

# ---------- assets + CSV import
ok(post(admin, "/assets/new", {"tag": "ADUST-PC-0001", "type": "Desktop PC", "name": "HP ProDesk", "department": "Registrar Office", "location": "Room 201", "status": "in_use", "warranty_until": "2025-01-01"}), "Device saved")
csv_data = "tag,type,name,serial,department,location,user,purchase_date,warranty_until,status,notes\nADUST-PR-0001,Printer,HP LaserJet,S1,Accounts,Accounts room,,2023-01-01,2026-01-01,in_use,\nADUST-PC-0001,Desktop PC,HP ProDesk 400,S2,Registrar Office,Room 202,,,,in_use,\nBAD,Spaceship,,,,,,,,,\n"
ok(post(admin, "/assets/import", {"file": (io.BytesIO(csv_data.encode()), "a.csv")}, content_type="multipart/form-data"), "1 added, 1 updated", "Row 4")
assert q("SELECT location FROM assets WHERE tag='ADUST-PC-0001'")[0][0] == "Room 202"
assert admin.get("/assets?export=1").headers["Content-Type"].startswith("application/vnd.openxml")
assert u.get("/assets").status_code == 403
print("assets ok")

# ---------- help articles + suggestion API
ok(post(admin, "/help/new", {"title": "How to connect to campus Wi-Fi", "category_id": "2", "body": "## Steps\n1. Open Wi-Fi\n2. Choose ADUST\n- tip one\nSee https://adust.edu.bd", "published": "1"}), "Article saved", "<h3>Steps</h3>", "<ol>", 'href="https://adust.edu.bd"')
assert u.get("/api/help?category=2").json[0]["title"].startswith("How to connect")
ok(u.get("/help?q=wi-fi"), "How to connect")
assert u.post("/help/new", data={"csrf_token": tok(u)}).status_code == 403
print("help ok")

# ---------- announcements
ok(post(admin, "/admin/announcements", {"message": "Campus internet down, working on it", "level": "danger"}), "Announcement published")
ok(u.get("/"), "Campus internet down")
ok(client().get("/login"), "Campus internet down")
print("announce ok")

# ---------- urgent request -> SMS to IT
emails.clear(); sms.clear()
r = post(u, "/tickets/new", {"title": "Lab PC not booting", "category_id": "1", "priority": "9", "urgent": "1", "description": "PC dead", "asset_tag": "ADUST-PC-0001", "location": "Lab 3"}, content_type="multipart/form-data")
ok(r, "IT-%d-0001" % date.today().year, "has been sent to the IT section", "ADUST-PC-0001")
assert any("01711000001" in s[0] for s in sms) and any("01711000000" in s[0] for s in sms), sms
post(u, "/tickets/new", {"title": "x", "category_id": "1", "priority": "5", "description": "x"}, content_type="multipart/form-data")
ok(post(u, "/tickets/new", {"title": "x", "category_id": "1", "priority": "5", "description": "x", "asset_tag": "NOPE"}, content_type="multipart/form-data"), "was found")
print("urgent+sms ok")

# ---------- approval flow (category 13 = New equipment / Purchase): chairman -> Registrar -> IT
post(admin, "/admin/users", {"name": "Registrar Sir", "email": "registrar@adust.edu.bd", "role": "user", "department": "Office of the Registrar", "approver_title": "registrar", "password": "registrar1"})
reg = client(); login(reg, "registrar@adust.edu.bd", "registrar1")
png = (b"\x89PNG\r\n\x1a\n" + b"0" * 64)
ok(post(reg, "/profile", {"action": "signature", "signature": (io.BytesIO(png), "sig.png")}, content_type="multipart/form-data"), "Signature saved")
assert reg.get("/profile/signature").status_code == 200
emails.clear()
ok(post(u, "/tickets/new", {"title": "Need a new laptop", "category_id": "13", "priority": "5", "description": "old one broken"}, content_type="multipart/form-data"), "department head for approval", "Waiting for approval", "Department approval")
tid = q("SELECT id FROM tickets WHERE title='Need a new laptop'")[0][0]
assert any(e[0] == ["head@adust.edu.bd"] for e in emails), emails
ok(head.get("/"), "waiting for your approval")
ok(head.get("/approvals"), "Need a new laptop")
assert it.get("/tickets?status=pending_approval").status_code == 200
ok(post(head, f"/tickets/{tid}/approve", {"decision": "reject"}), "reason for rejecting")
assert b"Need a new laptop" not in reg.get("/approvals").data and reg.get(f"/tickets/{tid}").status_code == 403
emails.clear()
ok(post(head, f"/tickets/{tid}/approve", {"decision": "approve", "comment": "OK, budget available"}), "goes to the Registrar")
assert q("SELECT status, approval_stage FROM tickets WHERE id=?", tid)[0][:] == ("pending_approval", "registrar")
assert any(e[0] == ["registrar@adust.edu.bd"] and "Registrar approval needed" in e[1] for e in emails), emails
assert not any("New approved request" in e[1] for e in emails)            # IT not yet involved
assert head.post(f"/tickets/{tid}/approve", data={"csrf_token": tok(head), "decision": "approve"}).status_code == 403
ok(u.get(f"/tickets/{tid}"), "Registrar approval", "Registrar Sir")
ok(reg.get("/approvals"), "Need a new laptop")
emails.clear()
ok(post(reg, f"/tickets/{tid}/approve", {"decision": "approve", "comment": "Purchase approved",
        "file": (io.BytesIO(b"%PDF-1.4 demand"), "demand-letter.pdf")}, content_type="multipart/form-data"), "approved and sent")
assert q("SELECT status, approval_stage, approved_by FROM tickets WHERE id=?", tid)[0][:] == ("new", None, uid("registrar@adust.edu.bd"))
assert any("New approved request" in e[1] for e in emails)
card = ok(it.get(f"/tickets/{tid}/print"), "Approvals", "Dr. Head CSE", "Chairman, Computer Science", "Registrar Sir", "demand-letter.pdf")
a_ids = [r[0] for r in q("SELECT id FROM approvals WHERE ticket_id=? ORDER BY id", tid)]
assert f"/approvals/{a_ids[1]}/signature".encode() in card.data and f"/approvals/{a_ids[0]}/signature".encode() not in card.data
assert it.get(f"/approvals/{a_ids[1]}/signature").data == png
assert it.get(f"/approvals/{a_ids[1]}/file").data == b"%PDF-1.4 demand"
ok(reg.get(f"/tickets/{tid}"), "Need a new laptop")                       # registrar keeps access afterwards
print("two-stage approval ok")
assert head.post(f"/tickets/{tid}/update", data={"csrf_token": tok(head), "body": "hi"}).status_code == 403
other = client(); post(other, "/register", {"name": "Other", "email": "o@adust.edu.bd", "department": "Law", "password": "secret123", "confirm": "secret123"}); approve("o@adust.edu.bd"); login(other, "o@adust.edu.bd", "secret123")
assert other.get(f"/tickets/{tid}").status_code == 403 and other.get("/approvals").status_code == 403
print("approval ok")

# ---------- chairman first, coordinator when there is no chairman / chairman is overdue
CSE, EEE = "Computer Science & Engineering (CSE)", "Electrical & Electronic Engineering (EEE)"
post(admin, "/admin/users", {"name": "CSE Coordinator", "email": "coord@adust.edu.bd", "role": "user", "department": CSE, "approver_title": "coordinator", "password": "coordpass1"})
post(admin, "/admin/users", {"name": "EEE Coordinator", "email": "eeecoord@adust.edu.bd", "role": "user", "department": EEE, "approver_title": "coordinator", "password": "coordpass1"})
post(admin, "/admin/users", {"name": "EEE Teacher", "email": "eeeteacher@adust.edu.bd", "role": "user", "department": EEE, "password": "teacher123"})
coord = client(); login(coord, "coord@adust.edu.bd", "coordpass1")
eeec = client(); login(eeec, "eeecoord@adust.edu.bd", "coordpass1")
eeet = client(); login(eeet, "eeeteacher@adust.edu.bd", "teacher123")
def new_purchase(c, title):
    post(c, "/tickets/new", {"title": title, "category_id": "13", "priority": "5", "description": "need"}, content_type="multipart/form-data")
    return q("SELECT id FROM tickets WHERE title=?", title)[0][0]
emails.clear()
k1 = new_purchase(u, "New keyboard for CSE lab")                     # CSE has chairman + coordinator
to = [e[0][0] for e in emails if "Approval needed" in e[1]]
assert to == ["head@adust.edu.bd"], to                                 # only the chairman
assert b"New keyboard" not in coord.get("/approvals").data
assert coord.post(f"/tickets/{k1}/approve", data={"csrf_token": tok(coord), "decision": "approve"}).status_code == 403
ok(u.get(f"/tickets/{k1}"), "Waiting for approval from", "Dr. Head CSE", "Chairman")
with app.app_context():                                                # chairman does not respond in time
    A.get_db().execute("UPDATE tickets SET created_at=?, updated_at=? WHERE id=?", (A.now(timedelta(hours=-30)), A.now(timedelta(hours=-30)), k1)); A.get_db().commit()
    A.housekeeping()
    A.set_setting("last_backup", "")
assert any(e[0] == ["coord@adust.edu.bd"] and "chairman has not responded" in e[1] for e in emails), emails
ok(coord.get("/approvals"), "New keyboard")
ok(post(coord, f"/tickets/{k1}/approve", {"decision": "approve"}), "goes to the Registrar")
ok(post(reg, f"/tickets/{k1}/approve", {"decision": "approve"}), "approved and sent")
ok(u.get(f"/tickets/{k1}"), "Approved by Coordinator, Computer Science &amp; Engineering (CSE)")
emails.clear()
k2 = new_purchase(eeet, "Projector lamp for EEE")                      # EEE has only a coordinator
assert [e[0][0] for e in emails if "Approval needed" in e[1]] == ["eeecoord@adust.edu.bd"]
ok(post(eeec, f"/tickets/{k2}/approve", {"decision": "approve"}), "goes to the Registrar")
k3 = new_purchase(coord, "Coordinator asks for a monitor")             # coordinator -> chairman approves
assert q("SELECT status FROM tickets WHERE id=?", k3)[0][0] == "pending_approval"
ok(post(head, f"/tickets/{k3}/approve", {"decision": "approve"}), "goes to the Registrar")
k4 = new_purchase(eeec, "EEE coordinator asks for a UPS")              # top approver of EEE -> straight to Registrar
k5 = new_purchase(head, "Chairman asks for a printer")                 # chairman -> straight to Registrar
assert [tuple(r) for r in q("SELECT status, approval_stage FROM tickets WHERE id IN (?, ?)", k4, k5)] == [("pending_approval", "registrar")] * 2
for k in (k2, k3, k4, k5):
    post(reg, f"/tickets/{k}/approve", {"decision": "approve"})
k6 = new_purchase(reg, "Registrar asks for a scanner")                 # registrar's own purchase -> no approval
post(u, "/tickets/new", {"title": "Keyboard keys stuck", "category_id": "1", "priority": "5", "description": "repair"}, content_type="multipart/form-data")
assert [r[0] for r in q("SELECT status FROM tickets WHERE title IN ('Registrar asks for a scanner', 'Keyboard keys stuck')")] == ["new", "new"]
ok(admin.get("/admin/departments"), "Dr. Head CSE (Chairman)", "CSE Coordinator (Coordinator)")
print("chairman/coordinator routing ok")

# ---------- university offices from adust.edu.bd: parent-office fallback, office heads, BoT
FET_DEAN = "Dean's Office — Faculty of Engineering & Technology"
ok(admin.get("/admin/departments"), "Registrar Office — Store Section", "Office of the Controller of Examinations",
   "Board of Trustees (BoT)", "Robotics and Automation Engineering", "Faculty of Health Science")
ok(admin.get("/admin/users"), '<optgroup label="Faculty of Engineering &amp; Technology">')
for n, e, dep, title in [("Store Officer", "store@adust.edu.bd", "Registrar Office — Store Section", ""),
                       ("FET Dean", "dean.fet@adust.edu.bd", FET_DEAN, "head"),
                       ("Civil Teacher", "civil@adust.edu.bd", "Civil Engineering", ""),
                       ("Controller", "controller@adust.edu.bd", "Office of the Controller of Examinations", "head"),
                       ("Exam Staff", "exam@adust.edu.bd", "Office of the Controller of Examinations", ""),
                       ("BoT Member", "bot@adust.edu.bd", "Board of Trustees (BoT)", "")]:
    post(admin, "/admin/users", {"name": n, "email": e, "role": "user", "department": dep, "approver_title": title, "password": "officepass1"})
def office(email):
    c = client(); login(c, email, "officepass1"); return c
store, dean, civil, ctrl, exam, bot = (office(e) for e in ["store@adust.edu.bd", "dean.fet@adust.edu.bd", "civil@adust.edu.bd",
                                                            "controller@adust.edu.bd", "exam@adust.edu.bd", "bot@adust.edu.bd"])
s1 = new_purchase(store, "Store needs a barcode printer")              # no head in Store Section -> Registrar (parent)
ok(store.get(f"/tickets/{s1}"), "Waiting for approval from", "Registrar Sir")
ok(post(reg, f"/tickets/{s1}/approve", {"decision": "approve"}), "approved and sent")   # registrar = final, no 2nd step
assert q("SELECT status FROM tickets WHERE id=?", s1)[0][0] == "new"
s2 = new_purchase(civil, "Civil lab needs a total station")            # no chairman/coordinator -> Dean of FET
ok(civil.get(f"/tickets/{s2}"), "FET Dean")
ok(post(dean, f"/tickets/{s2}/approve", {"decision": "approve"}), "goes to the Registrar")
s3 = new_purchase(exam, "Exam office needs a scanner")                 # Controller (head of office) -> Registrar
ok(exam.get(f"/tickets/{s3}"), "Controller")
assert reg.post(f"/tickets/{s3}/approve", data={"csrf_token": tok(reg), "decision": "approve"}).status_code == 403
ok(post(ctrl, f"/tickets/{s3}/approve", {"decision": "approve"}), "goes to the Registrar")
s4 = new_purchase(bot, "BoT meeting room display")                     # BoT: no department approval -> Registrar
assert q("SELECT approval_stage FROM tickets WHERE id=?", s4)[0][0] == "registrar"
for k in (s2, s3, s4):
    ok(post(reg, f"/tickets/{k}/approve", {"decision": "approve"}), "approved and sent")
print("university offices ok")

# ---------- IT workflow, first response, rating
t1 = q("SELECT id FROM tickets WHERE title='Lab PC not booting'")[0][0]
ok(post(it, f"/tickets/{t1}/update", {"status": "in_progress", "assigned_to": "2", "priority": "9", "asset_tag": "ADUST-PC-0001", "body": "On my way"}), "Update saved")
assert q("SELECT first_response_at FROM tickets WHERE id=?", t1)[0][0]
ok(post(it, f"/tickets/{t1}/update", {"status": "resolved", "assigned_to": "2", "priority": "9", "asset_tag": "ADUST-PC-0001", "body": "Replaced PSU"}), "Update saved")
ok(u.get(f"/tickets/{t1}"), "Is the work done?", "rating-input")
ok(post(u, f"/tickets/{t1}/update", {"action": "confirm", "rating": "4", "rating_comment": "Quick work"}), "Closed", "★★★★")
assert q("SELECT rating FROM tickets WHERE id=?", t1)[0][0] == 4
ok(it.get(f"/tickets/{t1}/print"), "Job card", "Technician signature", "Lab PC not booting")
print("workflow+rating ok")

# ---------- housekeeping: escalation, overdue, auto-close, approval reminder, recurring, backup
with app.app_context():
    db = A.get_db()
    old = A.now(timedelta(hours=-6)); older = A.now(timedelta(days=-10)); yest = (date.today() - timedelta(days=1)).isoformat()
    db.execute("UPDATE tickets SET updated_at=?, created_at=? WHERE title='x'", (old, old))   # normal new ticket, 6h old
    db.execute("INSERT INTO tickets (code,title,status,requester_id,priority,due_date,created_at,updated_at,assigned_to) VALUES ('IT-T-1','Overdue job','in_progress',4,5,?,?,?,2)", (yest, old, old))
    db.execute("INSERT INTO tickets (code,title,status,requester_id,priority,created_at,updated_at) VALUES ('IT-T-2','Old resolved','resolved',4,5,?,?)", (older, older))
    db.execute("INSERT INTO tickets (code,title,status,requester_id,priority,created_at,updated_at) VALUES ('IT-T-3','Old approval','pending_approval',4,5,?,?)", (older, older))
    db.commit()
post(admin, "/admin/recurring", {"title": "Monthly server check", "category_id": "11", "frequency": "monthly", "next_run": date.today().isoformat(), "lead_days": "2", "assigned_to": "2", "priority": "6"})
emails.clear()
with app.app_context():
    res = A.housekeeping()
print("housekeeping:", res)
assert res["escalated"] == 1 and res["overdue"] == 1 and res["auto_closed"] == 1 and res["approval_reminders"] == 1 and res["recurring"] == 1 and res["backup"] != "-"
assert q("SELECT status FROM tickets WHERE code='IT-T-2'")[0][0] == "closed"
nr = q("SELECT next_run FROM recurring_tasks")[0][0]; assert nr > date.today().isoformat(), nr
assert q("SELECT COUNT(*) FROM tickets WHERE title='Monthly server check' AND assigned_to=2")[0][0] == 1
with app.app_context():
    res2 = A.housekeeping()
assert res2["escalated"] == 0 and res2["overdue"] == 0 and res2["recurring"] == 0 and res2["backup"] == "-", res2   # idempotent
bk = sorted(os.listdir(os.path.join(d, "bk")), reverse=True); assert len(bk) >= 1
z = zipfile.ZipFile(os.path.join(d, "bk", bk[0])); assert "portal.db" in z.namelist()
assert admin.get(f"/admin/backups/{bk[0]}").status_code == 200 and admin.get("/admin/backups/../t.db").status_code == 404
print("housekeeping ok")

# ---------- backups: second copy + monthly backups kept forever
copy_dir = os.path.join(d, "offsite")
app.config["BACKUP_COPY_FOLDER"] = copy_dir
bkdir = os.path.join(d, "bk")
fake = []
for stamp in ["20250105-020000", "20250120-020000", "20250203-020000", "20250228-020000", "20250310-020000"]:
    pth = os.path.join(bkdir, f"adust-it-backup-{stamp}.zip")
    open(pth, "wb").write(b"old")
    t = time.mktime(time.strptime(stamp, "%Y%m%d-%H%M%S")); os.utime(pth, (t, t)); fake.append(os.path.basename(pth))
with app.app_context():
    newest = os.path.basename(A.make_backup())
left = set(os.listdir(bkdir))
assert {"adust-it-backup-20250105-020000.zip", "adust-it-backup-20250203-020000.zip", "adust-it-backup-20250310-020000.zip"} <= left, left
assert "adust-it-backup-20250120-020000.zip" not in left, left   # 0228 survives: newest 3 are always kept
assert newest in os.listdir(copy_dir)
ok(admin.get("/admin/backups"), "Backups (copy 2)", copy_dir)
app.config["BACKUP_COPY_FOLDER"] = "/proc/forbidden/place"
with app.app_context():
    A.make_backup()
ok(admin.get("/admin/backups"), "failed")
app.config["BACKUP_COPY_FOLDER"] = ""
print("backup retention + copy ok")

# ---------- reports & exports
ok(admin.get("/admin/reports?range=all"), "IT staff performance", "ADUST-PC-0001", "4.0")
ok(admin.get("/admin/reports?range=all&print=1"), "Print / Save as PDF", "IT Section report")
assert admin.get("/admin/reports?range=all&export=xlsx").headers["Content-Type"].startswith("application/vnd.openxml")
assert admin.get("/tickets/export.xlsx?status=all").status_code == 200
from openpyxl import load_workbook
wb = load_workbook(io.BytesIO(admin.get("/tickets/export.xlsx?status=all").data)); assert wb.active.max_row >= 6
assert u.get("/admin/reports").status_code == 403
mine = load_workbook(io.BytesIO(u.get("/tickets/export.xlsx?status=all").data)).active
assert all(row[7].value == "Mim Akter" for row in mine.iter_rows(min_row=2)), "requester export must only contain own tickets"
print("reports ok")

# ---------- settings
ok(post(admin, "/admin/settings", {"escalate_hours": "6", "escalate_hours_urgent": "1", "approval_remind_hours": "24", "auto_close_days": "5", "backup_keep_days": "30", "sms_urgent": "1"}), "Settings saved")
assert q("SELECT value FROM settings WHERE key='auto_close_days'")[0][0] == "5"
ok(post(admin, "/admin/settings", {"action": "test_email"}), "Test email sent")
print("settings ok")

# ---------- language toggle & every page renders in both languages for every role
for lang in ("en", "bn"):
    for c in (admin, it, head, u):
        c.get(f"/lang/{lang}", headers={"Referer": "http://localhost/"})
    pages = {admin: ["/", "/tickets?status=all", "/tickets/new", f"/tickets/{t1}", f"/tickets/{tid}", "/help", "/help/1", "/help/1/edit", "/help/new",
                     "/assets", "/assets/1", "/assets/1/edit", "/assets/new", "/assets/import", "/admin/reports", "/admin/reports?print=1",
                     "/admin/users", "/admin/users?edit=2", "/admin/departments", "/admin/categories", "/admin/recurring", "/admin/recurring?edit=1",
                     "/admin/network", "/admin/backups", "/admin/settings", "/admin/announcements", "/notifications", "/profile", "/approvals", f"/tickets/{t1}/print"],
             it: ["/", "/tickets", f"/tickets/{t1}", "/admin/reports", "/assets"],
             head: ["/", "/approvals", "/help", "/profile"],
             u: ["/", "/tickets?status=all", "/tickets/new", f"/tickets/{t1}", "/help", "/help/1", "/notifications", "/profile"]}
    for c, urls in pages.items():
        for url in urls:
            r = c.get(url)
            assert r.status_code == 200, (lang, url, r.status_code, r.data[:500])
    if lang == "bn":
        ok(u.get("/"), "স্বাগতম", "নতুন অনুরোধ"); ok(admin.get("/admin/settings"), "সেটিংস")
for c in (admin, it, head, u):
    c.get("/lang/en")
ok(client().get("/lang/bn", follow_redirects=True), "সাইন ইন")
for p in ["/login", "/register", "/forgot"]:
    assert client().get(p).status_code == 200
print("pages ok (both languages)")

# ---------- network block still works
post(admin, "/admin/network", {"cidr": "10.20.0.0/16"})
assert client("8.8.8.8").get("/login").status_code == 403
assert client("8.8.8.8").get("/static/img/adust-logo.png").status_code == 200
print("ALL OK")
