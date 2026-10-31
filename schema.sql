-- role: 'user' = requester (faculty/staff), 'it' = IT section staff, 'admin' = IT head
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    login_id TEXT UNIQUE,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    phone TEXT NOT NULL DEFAULT '',
    department TEXT NOT NULL DEFAULT '',
    designation TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'it', 'admin')),
    is_approver INTEGER NOT NULL DEFAULT 0,      -- 1 when approver_title is set (kept for quick checks)
    approver_title TEXT NOT NULL DEFAULT '',
    signature_stored TEXT,                       -- uploaded signature image, used on approvals and job cards     -- '', 'chairman', 'head' (head of office) or 'coordinator'
    lang TEXT NOT NULL DEFAULT 'en',
    password_hash TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    approved INTEGER NOT NULL DEFAULT 1,         -- 0 = self-registered, waiting for an admin to verify
    created_at TEXT NOT NULL,
    last_login TEXT
);

CREATE TABLE IF NOT EXISTS departments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    grp TEXT NOT NULL DEFAULT 'Other',           -- heading in the department list (faculty, offices, ...)
    parent TEXT NOT NULL DEFAULT '',             -- approvals fall back to this office when no head is set here
    skip_approval INTEGER NOT NULL DEFAULT 0,    -- top offices (BoT, VC) need no department approval
    sort INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    needs_approval INTEGER NOT NULL DEFAULT 0,  -- requests go to the department chairman first
    needs_registrar INTEGER NOT NULL DEFAULT 0, -- new purchases: the Registrar gives final approval
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag TEXT NOT NULL UNIQUE COLLATE NOCASE,
    type TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    serial TEXT NOT NULL DEFAULT '',
    department TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    user_name TEXT NOT NULL DEFAULT '',
    purchase_date TEXT,
    warranty_until TEXT,
    status TEXT NOT NULL DEFAULT 'in_use' CHECK (status IN ('in_use', 'spare', 'in_repair', 'retired')),
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recurring_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    category_id INTEGER REFERENCES categories(id),
    priority INTEGER NOT NULL DEFAULT 5,
    assigned_to INTEGER REFERENCES users(id),
    asset_id INTEGER REFERENCES assets(id) ON DELETE SET NULL,
    location TEXT NOT NULL DEFAULT '',
    frequency TEXT NOT NULL CHECK (frequency IN ('daily', 'weekly', 'monthly', 'quarterly', 'yearly')),
    lead_days INTEGER NOT NULL DEFAULT 1,
    next_run TEXT NOT NULL,
    last_run TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    remarks TEXT NOT NULL DEFAULT '',
    category_id INTEGER REFERENCES categories(id),
    priority INTEGER NOT NULL DEFAULT 5 CHECK (priority BETWEEN 1 AND 10),
    urgent INTEGER NOT NULL DEFAULT 0,
    due_date TEXT,
    location TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new'
        CHECK (status IN ('pending_approval', 'new', 'in_progress', 'on_hold', 'resolved',
                          'closed', 'rejected', 'cancelled')),
    requester_id INTEGER NOT NULL REFERENCES users(id),
    assigned_to INTEGER REFERENCES users(id),
    asset_id INTEGER REFERENCES assets(id) ON DELETE SET NULL,
    recurring_id INTEGER REFERENCES recurring_tasks(id) ON DELETE SET NULL,
    attachment_name TEXT,
    attachment_stored TEXT,
    approval_stage TEXT,                         -- while pending_approval: 'department' or 'registrar'
    approved_by INTEGER REFERENCES users(id),
    approved_at TEXT,
    rating INTEGER CHECK (rating BETWEEN 1 AND 5),
    rating_comment TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    first_response_at TEXT,
    resolved_at TEXT,
    escalated_at TEXT,
    overdue_notified TEXT
);

-- Timeline of a ticket. user_id NULL = done automatically by the system.
-- internal = 1 means an IT-only note the requester cannot see.
CREATE TABLE IF NOT EXISTS ticket_updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id),
    body TEXT NOT NULL DEFAULT '',
    event TEXT NOT NULL DEFAULT '',
    internal INTEGER NOT NULL DEFAULT 0,
    attachment_name TEXT,
    attachment_stored TEXT,
    created_at TEXT NOT NULL
);

-- Every approval decision, with the approver's title and signature at that moment.
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    stage TEXT NOT NULL,                         -- 'department' or 'registrar'
    user_id INTEGER REFERENCES users(id),
    approver_name TEXT NOT NULL,
    approver_title TEXT NOT NULL DEFAULT '',
    decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
    comment TEXT NOT NULL DEFAULT '',
    signature_stored TEXT,
    attachment_name TEXT,
    attachment_stored TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ticket_id INTEGER REFERENCES tickets(id) ON DELETE CASCADE,
    message TEXT NOT NULL,
    is_read INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS announcements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info' CHECK (level IN ('info', 'warning', 'danger')),
    ends_at TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kb_articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    published INTEGER NOT NULL DEFAULT 1,
    views INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- University networks allowed to open the portal (CIDR, e.g. 103.x.x.0/24)
CREATE TABLE IF NOT EXISTS allowed_networks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cidr TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL DEFAULT ''
);

-- Failed sign-in / reset attempts, for lock-out after repeated failures
CREATE TABLE IF NOT EXISTS login_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ident TEXT NOT NULL,
    ip TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tickets_requester ON tickets(requester_id);
CREATE INDEX IF NOT EXISTS idx_tickets_assigned ON tickets(assigned_to);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_tickets_asset ON tickets(asset_id);
CREATE INDEX IF NOT EXISTS idx_updates_ticket ON ticket_updates(ticket_id);
CREATE INDEX IF NOT EXISTS idx_notif_user ON notifications(user_id, is_read);
CREATE INDEX IF NOT EXISTS idx_attempts ON login_attempts(ident, created_at);
