from __future__ import annotations

import os
import sqlite3
from datetime import datetime, date, UTC
from functools import wraps
from pathlib import Path
from typing import Optional

from flask import (
    Flask,
    flash,
    g,
    redirect,
    render_template_string,
    request,
    session,
    url_for,
    send_from_directory,
)
from jinja2 import DictLoader
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename


BASE_DIR = Path(__file__).resolve().parent
DATABASE = BASE_DIR / "equipment_manager.db"
UPLOAD_FOLDER = BASE_DIR / "uploads"
IMAGE_FOLDER = UPLOAD_FOLDER / "images"
MANUAL_FOLDER = UPLOAD_FOLDER / "manuals"

ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
ALLOWED_MANUAL_EXTENSIONS = {"pdf", "doc", "docx", "txt"}

IMAGE_FOLDER.mkdir(parents=True, exist_ok=True)
MANUAL_FOLDER.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["SECRET_KEY"] = "change-this-secret-key-before-deploying"
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def allowed_file(filename: str, allowed_extensions: set[str]) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in allowed_extensions


def save_uploaded_file(file_storage, folder: Path, prefix: str) -> Optional[str]:
    if not file_storage or not file_storage.filename:
        return None

    filename = secure_filename(file_storage.filename)
    if not filename:
        return None

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    final_name = f"{prefix}_{timestamp}_{filename}"
    filepath = folder / final_name
    file_storage.save(filepath)
    return final_name


# =========================
# Database helpers
# =========================
def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exception: Optional[BaseException]) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    full_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin', 'user')) DEFAULT 'user',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS equipment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    serial_number TEXT,
    location TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('available', 'in_use', 'reserved', 'maintenance')) DEFAULT 'available',
    notes TEXT,
    manual_link TEXT,
    image_filename TEXT,
    manual_filename TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checkouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    date_taken TEXT NOT NULL,
    expected_return_date TEXT,
    actual_return_date TEXT,
    note TEXT,
    status TEXT NOT NULL CHECK(status IN ('active', 'returned')) DEFAULT 'active',
    FOREIGN KEY (equipment_id) REFERENCES equipment(id),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    note TEXT,
    status TEXT NOT NULL CHECK(status IN ('active', 'cancelled', 'completed')) DEFAULT 'active',
    FOREIGN KEY (equipment_id) REFERENCES equipment(id),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_id INTEGER NOT NULL,
    user_id INTEGER,
    action TEXT NOT NULL,
    action_date TEXT NOT NULL,
    note TEXT,
    FOREIGN KEY (equipment_id) REFERENCES equipment(id),
    FOREIGN KEY (user_id) REFERENCES users(id)
);
"""


def ensure_equipment_columns(db: sqlite3.Connection) -> None:
    columns = [row["name"] for row in db.execute("PRAGMA table_info(equipment)").fetchall()]
    if "image_filename" not in columns:
        db.execute("ALTER TABLE equipment ADD COLUMN image_filename TEXT")
    if "manual_filename" not in columns:
        db.execute("ALTER TABLE equipment ADD COLUMN manual_filename TEXT")
    db.commit()


def init_db() -> None:
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    ensure_equipment_columns(db)
    db.commit()

    existing_admin = db.execute(
        "SELECT id FROM users WHERE username = ?",
        ("admin",),
    ).fetchone()

    if not existing_admin:
        now = utc_now_iso()
        db.execute(
            """
            INSERT INTO users (username, full_name, password_hash, role, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "admin",
                "Lab Admin",
                generate_password_hash("admin123"),
                "admin",
                now,
            ),
        )

    equipment_count = db.execute(
        "SELECT COUNT(*) AS count FROM equipment"
    ).fetchone()["count"]

    if equipment_count == 0:
        now = utc_now_iso()
        demo_items = [
            (
                "Turbopump 1",
                "Vacuum",
                "TP-001",
                "Cryo Lab Room 210",
                "available",
                "Use with valve set A only.",
                "",
                None,
                None,
                now,
                now,
            ),
            (
                "Turbopump 2",
                "Vacuum",
                "TP-002",
                "Cryo Lab Room 211",
                "in_use",
                "Needs warm-up before use.",
                "",
                None,
                None,
                now,
                now,
            ),
            (
                "Pressure Gauge 1",
                "Measurement",
                "PG-101",
                "Instrument Cabinet",
                "reserved",
                "Handle carefully.",
                "",
                None,
                None,
                now,
                now,
            ),
            (
                "Helium Regulator",
                "Gas Handling",
                "HR-220",
                "Gas Rack",
                "maintenance",
                "O-ring replacement pending.",
                "",
                None,
                None,
                now,
                now,
            ),
        ]

        db.executemany(
            """
            INSERT INTO equipment
            (name, category, serial_number, location, status, notes, manual_link, image_filename, manual_filename, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            demo_items,
        )

        admin = db.execute(
            "SELECT id FROM users WHERE username = ?",
            ("admin",),
        ).fetchone()

        if admin:
            admin_id = admin["id"]
            turbopump_2 = db.execute(
                "SELECT id FROM equipment WHERE name = ?",
                ("Turbopump 2",),
            ).fetchone()["id"]
            gauge_1 = db.execute(
                "SELECT id FROM equipment WHERE name = ?",
                ("Pressure Gauge 1",),
            ).fetchone()["id"]

            db.execute(
                """
                INSERT INTO checkouts (equipment_id, user_id, date_taken, expected_return_date, note, status)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    turbopump_2,
                    admin_id,
                    str(date.today()),
                    str(date.today()),
                    "Demo active checkout",
                    "active",
                ),
            )

            db.execute(
                """
                INSERT INTO reservations (equipment_id, user_id, start_date, end_date, note, status)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    gauge_1,
                    admin_id,
                    str(date.today()),
                    str(date.today()),
                    "Demo reservation",
                    "active",
                ),
            )

            db.execute(
                """
                INSERT INTO history (equipment_id, user_id, action, action_date, note)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    turbopump_2,
                    admin_id,
                    "Borrowed",
                    str(date.today()),
                    "Demo active checkout",
                ),
            )

            db.execute(
                """
                INSERT INTO history (equipment_id, user_id, action, action_date, note)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    gauge_1,
                    admin_id,
                    "Reserved",
                    str(date.today()),
                    "Demo reservation",
                ),
            )

    db.commit()
    db.close()


# =========================
# Auth helpers
# =========================
def login_required(view_func):
    @wraps(view_func)
    def wrapped_view(*args, **kwargs):
        if session.get("user_id") is None:
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)

    return wrapped_view


def admin_required(view_func):
    @wraps(view_func)
    def wrapped_view(*args, **kwargs):
        if session.get("user_id") is None:
            return redirect(url_for("login"))
        if session.get("role") != "admin":
            flash("Admin access required.", "danger")
            return redirect(url_for("dashboard"))
        return view_func(*args, **kwargs)

    return wrapped_view


@app.before_request
def load_logged_in_user() -> None:
    g.user = None
    user_id = session.get("user_id")
    if user_id is not None:
        g.user = get_db().execute(
            "SELECT * FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()


# =========================
# Utility functions
# =========================
def get_equipment_or_404(equipment_id: int) -> sqlite3.Row:
    item = get_db().execute(
        "SELECT * FROM equipment WHERE id = ?",
        (equipment_id,),
    ).fetchone()
    if item is None:
        raise ValueError("Equipment not found")
    return item


def get_user_or_404(user_id: int) -> sqlite3.Row:
    item = get_db().execute(
        "SELECT * FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if item is None:
        raise ValueError("User not found")
    return item


def add_history(
    equipment_id: int,
    user_id: Optional[int],
    action: str,
    note: str = "",
) -> None:
    db = get_db()
    db.execute(
        "INSERT INTO history (equipment_id, user_id, action, action_date, note) VALUES (?, ?, ?, ?, ?)",
        (equipment_id, user_id, action, str(date.today()), note),
    )
    db.commit()


def status_badge_class(status: str) -> str:
    return {
        "available": "status-available",
        "in_use": "status-in_use",
        "reserved": "status-reserved",
        "maintenance": "status-maintenance",
    }.get(status, "")


app.jinja_env.globals["status_badge_class"] = status_badge_class


# =========================
# Templates
# =========================
BASE_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ title or 'Lab Equipment Manager' }}</title>
  <style>
    :root {
      --blue: #1d4ed8;
      --blue-dark: #1e40af;
      --bg: #f8fafc;
      --card: #ffffff;
      --border: #dbe4f0;
      --text: #0f172a;
      --muted: #475569;
      --green-bg: #dcfce7;
      --green-text: #166534;
      --red-bg: #fee2e2;
      --red-text: #991b1b;
      --amber-bg: #fef3c7;
      --amber-text: #92400e;
      --gray-bg: #e2e8f0;
      --gray-text: #334155;
    }

    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Arial, Helvetica, sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    a { color: inherit; text-decoration: none; }
    .container { max-width: 1180px; margin: 0 auto; padding: 24px; }
    .topbar {
      background: linear-gradient(135deg, var(--blue-dark), var(--blue));
      color: white;
      border-radius: 18px;
      padding: 20px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 20px;
    }
    .topbar h1 { margin: 0; font-size: 28px; }
    .topbar p { margin: 4px 0 0; color: #dbeafe; }
    .nav {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-bottom: 20px;
    }
    .nav a {
      background: white;
      border: 1px solid var(--border);
      padding: 10px 14px;
      border-radius: 12px;
      font-weight: 600;
      color: var(--muted);
    }
    .nav a.active, .nav a:hover {
      border-color: var(--blue);
      color: var(--blue);
      background: #eff6ff;
    }
    .flash-wrap { margin-bottom: 16px; }
    .flash {
      padding: 12px 14px;
      border-radius: 12px;
      margin-bottom: 10px;
      border: 1px solid var(--border);
      background: white;
    }
    .flash.success { background: #ecfdf5; color: #065f46; border-color: #a7f3d0; }
    .flash.danger { background: #fef2f2; color: #991b1b; border-color: #fecaca; }
    .flash.warning { background: #fffbeb; color: #92400e; border-color: #fde68a; }
    .grid {
      display: grid;
      gap: 20px;
    }
    .grid-2 { grid-template-columns: 1fr 1.2fr; }
    .grid-4 { grid-template-columns: repeat(4, 1fr); }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 18px;
      padding: 18px;
      box-shadow: 0 1px 3px rgba(15, 23, 42, 0.04);
    }
    .card h2, .card h3 { margin-top: 0; }
    .stats .number { font-size: 30px; font-weight: 700; }
    .stats .label { color: var(--muted); font-size: 14px; }
    .muted { color: var(--muted); }
    .small { font-size: 13px; }
    .status-badge {
      display: inline-block;
      padding: 6px 10px;
      border-radius: 999px;
      font-size: 13px;
      font-weight: 700;
    }
    .status-available { background: var(--green-bg); color: var(--green-text); }
    .status-in_use { background: var(--red-bg); color: var(--red-text); }
    .status-reserved { background: var(--amber-bg); color: var(--amber-text); }
    .status-maintenance { background: var(--gray-bg); color: var(--gray-text); }
    .equipment-list { display: grid; gap: 12px; }
    .equipment-item {
      display: block;
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 14px;
      background: white;
    }
    .equipment-item.active {
      border-color: var(--blue);
      background: #eff6ff;
    }
    .row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }
    .stack { display: grid; gap: 12px; }
    .equipment-photo {
      width: 100%;
      height: 220px;
      object-fit: cover;
      border-radius: 16px;
      border: 1px solid var(--border);
      background: #f8fafc;
    }
    .placeholder-photo {
      height: 220px;
      border-radius: 16px;
      border: 1px dashed var(--border);
      background: #f1f5f9;
      display: flex;
      align-items: center;
      justify-content: center;
      color: #64748b;
      font-weight: 700;
    }
    .button, button {
      border: none;
      background: var(--blue);
      color: white;
      padding: 10px 14px;
      border-radius: 12px;
      font-weight: 700;
      cursor: pointer;
    }
    .button:hover, button:hover { background: var(--blue-dark); }
    .button.secondary {
      background: white;
      color: var(--text);
      border: 1px solid var(--border);
    }
    .button.secondary:hover { background: #f8fafc; }
    .button.green { background: #16a34a; }
    .button.green:hover { background: #15803d; }
    .button.red { background: #dc2626; }
    .button.red:hover { background: #b91c1c; }
    .actions { display: flex; gap: 10px; flex-wrap: wrap; }
    input, select, textarea {
      width: 100%;
      padding: 10px 12px;
      border-radius: 12px;
      border: 1px solid var(--border);
      background: white;
      color: var(--text);
    }
    input[type=file] {
      padding: 8px;
    }
    textarea { min-height: 90px; resize: vertical; }
    label { font-weight: 700; font-size: 14px; display: block; margin-bottom: 6px; }
    .form-grid { display: grid; gap: 14px; grid-template-columns: repeat(2, 1fr); }
    .form-grid .full { grid-column: 1 / -1; }
    table {
      width: 100%;
      border-collapse: collapse;
      background: white;
      overflow: hidden;
      border-radius: 16px;
      border: 1px solid var(--border);
    }
    th, td {
      text-align: left;
      padding: 12px 14px;
      border-bottom: 1px solid #eef2f7;
      font-size: 14px;
      vertical-align: top;
    }
    th { background: #f8fafc; }
    .login-wrap {
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
    }
    .login-card {
      width: 100%;
      max-width: 460px;
      background: white;
      border: 1px solid var(--border);
      border-radius: 20px;
      padding: 26px;
      box-shadow: 0 8px 30px rgba(15, 23, 42, 0.08);
    }
    form.inline { display: inline; }
    @media (max-width: 900px) {
      .grid-2, .grid-4 { grid-template-columns: 1fr; }
      .form-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  {% block body %}{% endblock %}
</body>
</html>
"""

LOGIN_TEMPLATE = """
{% extends 'base.html' %}
{% block body %}
<div class="login-wrap">
  <div class="login-card">
    <h1>Lab Equipment Manager</h1>
    <p class="muted">Sign in to view, reserve, and borrow equipment.</p>

    <div class="flash-wrap">
      {% with messages = get_flashed_messages(with_categories=true) %}
        {% for category, message in messages %}
          <div class="flash {{ category }}">{{ message }}</div>
        {% endfor %}
      {% endwith %}
    </div>

    <form method="post" class="stack">
      <div>
        <label for="username">Username</label>
        <input id="username" name="username" required>
      </div>
      <div>
        <label for="password">Password</label>
        <input id="password" type="password" name="password" required>
      </div>
      <button type="submit">Login</button>
    </form>

    <hr style="margin: 18px 0; border: none; border-top: 1px solid #e5e7eb;">
    <div class="small muted">
      Default admin login for first run:<br>
      <strong>username:</strong> admin<br>
      <strong>password:</strong> admin123
    </div>
  </div>
</div>
{% endblock %}
"""

DASHBOARD_TEMPLATE = """
{% extends 'base.html' %}
{% block body %}
<div class="container">
  <div class="topbar">
    <div>
      <h1>Lab Equipment Manager</h1>
      <p>Track availability, reservations, borrowing, and history.</p>
    </div>
    <div>
      <strong>{{ g.user['full_name'] }}</strong> ({{ session['role'] }})
      <div style="margin-top: 8px;"><a class="button secondary" href="{{ url_for('logout') }}">Logout</a></div>
    </div>
  </div>

  <div class="nav">
    <a href="{{ url_for('dashboard') }}" class="active">Dashboard</a>
    <a href="{{ url_for('history_page') }}">History</a>
    {% if session['role'] == 'admin' %}
      <a href="{{ url_for('admin_page') }}">Admin</a>
    {% endif %}
  </div>

  <div class="flash-wrap">
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% for category, message in messages %}
        <div class="flash {{ category }}">{{ message }}</div>
      {% endfor %}
    {% endwith %}
  </div>

  <div class="grid grid-4 stats" style="margin-bottom: 20px;">
    <div class="card"><div class="label">Total</div><div class="number">{{ stats.total }}</div></div>
    <div class="card"><div class="label">Available</div><div class="number" style="color:#166534;">{{ stats.available }}</div></div>
    <div class="card"><div class="label">In Use</div><div class="number" style="color:#991b1b;">{{ stats.in_use }}</div></div>
    <div class="card"><div class="label">Reserved</div><div class="number" style="color:#92400e;">{{ stats.reserved }}</div></div>
  </div>

  <div class="grid grid-2">
    <div class="card">
      <div class="row" style="margin-bottom: 16px; align-items: end;">
        <div>
          <h2 style="margin-bottom: 4px;">Equipment List</h2>
          <div class="muted">Click any item to see details.</div>
        </div>
      </div>

      <form method="get" action="{{ url_for('dashboard') }}" class="form-grid" style="margin-bottom: 16px;">
        <div>
          <label>Search</label>
          <input name="search" value="{{ search }}" placeholder="Search by equipment or location">
        </div>
        <div>
          <label>Category</label>
          <select name="category">
            <option value="all">All Categories</option>
            {% for cat in categories %}
              <option value="{{ cat }}" {% if cat == category %}selected{% endif %}>{{ cat }}</option>
            {% endfor %}
          </select>
        </div>
        <div class="full">
          <button type="submit">Filter</button>
        </div>
      </form>

      <div class="equipment-list">
        {% for item in equipment %}
          <a class="equipment-item {% if selected and selected['id'] == item['id'] %}active{% endif %}" href="{{ url_for('dashboard', selected_id=item['id'], search=search, category=category) }}">
            <div class="row">
              <div>
                <div style="font-size: 18px; font-weight: 700;">{{ item['name'] }}</div>
                <div class="muted small">{{ item['category'] }} • {{ item['location'] }}</div>
                <div class="muted small">Serial: {{ item['serial_number'] or '—' }}</div>
              </div>
              <span class="status-badge {{ status_badge_class(item['status']) }}">{{ item['status'].replace('_', ' ').title() }}</span>
            </div>
          </a>
        {% else %}
          <div class="muted">No equipment found.</div>
        {% endfor %}
      </div>
    </div>

    <div class="card">
      {% if selected %}
        <h2>{{ selected['name'] }}</h2>
        <div class="grid" style="grid-template-columns: 240px 1fr; gap: 18px;">
          <div>
            {% if selected['image_filename'] %}
              <img class="equipment-photo" src="{{ url_for('uploaded_image', filename=selected['image_filename']) }}" alt="Equipment photo">
            {% else %}
              <div class="placeholder-photo">Equipment Photo</div>
            {% endif %}
          </div>
          <div class="stack">
            <div><strong>Status:</strong> <span class="status-badge {{ status_badge_class(selected['status']) }}">{{ selected['status'].replace('_', ' ').title() }}</span></div>
            <div><strong>Category:</strong> {{ selected['category'] }}</div>
            <div><strong>Location:</strong> {{ selected['location'] }}</div>
            <div><strong>Serial Number:</strong> {{ selected['serial_number'] or '—' }}</div>
            <div>
              <strong>Manual:</strong>
              {% if selected['manual_filename'] %}
                <a href="{{ url_for('uploaded_manual', filename=selected['manual_filename']) }}" target="_blank" style="color:#1d4ed8;">Open uploaded manual</a>
              {% elif selected['manual_link'] %}
                <a href="{{ selected['manual_link'] }}" target="_blank" style="color:#1d4ed8;">Open manual link</a>
              {% else %}
                Not set
              {% endif %}
            </div>
            <div><strong>Notes:</strong> {{ selected['notes'] or 'No notes.' }}</div>
            {% if active_checkout %}
              <div><strong>Current User:</strong> {{ active_checkout['full_name'] }}</div>
              <div><strong>Date Taken:</strong> {{ active_checkout['date_taken'] }}</div>
              <div><strong>Expected Return:</strong> {{ active_checkout['expected_return_date'] or '—' }}</div>
            {% endif %}
            {% if active_reservation %}
              <div><strong>Reserved By:</strong> {{ active_reservation['full_name'] }}</div>
              <div><strong>Reservation Window:</strong> {{ active_reservation['start_date'] }} to {{ active_reservation['end_date'] }}</div>
            {% endif %}
          </div>
        </div>

        <div class="actions" style="margin-top: 18px;">
          {% if selected['status'] == 'available' %}
            <a class="button green" href="{{ url_for('take_equipment', equipment_id=selected['id']) }}">Take Equipment</a>
          {% endif %}
          {% if selected['status'] != 'maintenance' %}
            <a class="button" href="{{ url_for('reserve_equipment', equipment_id=selected['id']) }}">Reserve</a>
          {% endif %}
          {% if selected['status'] in ['in_use', 'reserved'] %}
            <a class="button red" href="{{ url_for('return_equipment', equipment_id=selected['id']) }}">Return / Release</a>
          {% endif %}
          {% if session['role'] == 'admin' %}
            <a class="button secondary" href="{{ url_for('edit_equipment', equipment_id=selected['id']) }}">Edit Equipment</a>
          {% endif %}
        </div>
      {% else %}
        <div class="muted">Select equipment from the list.</div>
      {% endif %}
    </div>
  </div>
</div>
{% endblock %}
"""

FORM_TEMPLATE = """
{% extends 'base.html' %}
{% block body %}
<div class="container">
  <div class="topbar">
    <div>
      <h1>{{ heading }}</h1>
      <p>{{ subtitle }}</p>
    </div>
    <div><a class="button secondary" href="{{ back_url }}">Back</a></div>
  </div>

  <div class="flash-wrap">
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% for category, message in messages %}
        <div class="flash {{ category }}">{{ message }}</div>
      {% endfor %}
    {% endwith %}
  </div>

  <div class="card" style="max-width: 860px;">
    {% if item_name %}
      <h2>{{ item_name }}</h2>
    {% endif %}
    {% if item_subtitle %}
      <div class="muted" style="margin-bottom: 16px;">{{ item_subtitle }}</div>
    {% endif %}
    <form method="post" class="form-grid" enctype="multipart/form-data">
      {{ form_body|safe }}
      <div class="full actions" style="margin-top: 6px;">
        <button type="submit">{{ submit_label }}</button>
        <a class="button secondary" href="{{ back_url }}">Cancel</a>
      </div>
    </form>
  </div>
</div>
{% endblock %}
"""

HISTORY_TEMPLATE = """
{% extends 'base.html' %}
{% block body %}
<div class="container">
  <div class="topbar">
    <div>
      <h1>Equipment History</h1>
      <p>Recent activity across your lab equipment.</p>
    </div>
    <div><a class="button secondary" href="{{ url_for('dashboard') }}">Dashboard</a></div>
  </div>

  <div class="nav">
    <a href="{{ url_for('dashboard') }}">Dashboard</a>
    <a href="{{ url_for('history_page') }}" class="active">History</a>
    {% if session['role'] == 'admin' %}
      <a href="{{ url_for('admin_page') }}">Admin</a>
    {% endif %}
  </div>

  <div class="card">
    <table>
      <thead>
        <tr>
          <th>Date</th>
          <th>Equipment</th>
          <th>User</th>
          <th>Action</th>
          <th>Note</th>
        </tr>
      </thead>
      <tbody>
        {% for row in history_rows %}
          <tr>
            <td>{{ row['action_date'] }}</td>
            <td>{{ row['equipment_name'] }}</td>
            <td>{{ row['full_name'] or '—' }}</td>
            <td>{{ row['action'] }}</td>
            <td>{{ row['note'] or '—' }}</td>
          </tr>
        {% else %}
          <tr><td colspan="5" class="muted">No history found.</td></tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</div>
{% endblock %}
"""

ADMIN_TEMPLATE = """
{% extends 'base.html' %}
{% block body %}
<div class="container">
  <div class="topbar">
    <div>
      <h1>Admin Panel</h1>
      <p>Add, edit, or remove equipment and users.</p>
    </div>
    <div><a class="button secondary" href="{{ url_for('dashboard') }}">Dashboard</a></div>
  </div>

  <div class="nav">
    <a href="{{ url_for('dashboard') }}">Dashboard</a>
    <a href="{{ url_for('history_page') }}">History</a>
    <a href="{{ url_for('admin_page') }}" class="active">Admin</a>
  </div>

  <div class="flash-wrap">
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% for category, message in messages %}
        <div class="flash {{ category }}">{{ message }}</div>
      {% endfor %}
    {% endwith %}
  </div>

  <div class="grid grid-2" style="margin-bottom:20px;">
    <div class="card">
      <h2>Add Equipment</h2>
      <form method="post" action="{{ url_for('add_equipment') }}" class="form-grid" enctype="multipart/form-data">
        <div class="full"><label>Name</label><input name="name" required></div>
        <div><label>Category</label><input name="category" placeholder="Vacuum" required></div>
        <div><label>Serial Number</label><input name="serial_number"></div>
        <div class="full"><label>Location</label><input name="location" required></div>
        <div class="full"><label>Manual Link</label><input name="manual_link" placeholder="Optional URL"></div>
        <div><label>Equipment Photo</label><input type="file" name="image_file" accept=".png,.jpg,.jpeg,.gif,.webp"></div>
        <div><label>Manual File</label><input type="file" name="manual_file" accept=".pdf,.doc,.docx,.txt"></div>
        <div class="full"><label>Notes</label><textarea name="notes"></textarea></div>
        <div class="full"><button type="submit">Add Equipment</button></div>
      </form>
    </div>

    <div class="card">
      <h2>Create User</h2>
      <form method="post" action="{{ url_for('add_user') }}" class="form-grid">
        <div><label>Username</label><input name="username" required></div>
        <div><label>Full Name</label><input name="full_name" required></div>
        <div><label>Password</label><input name="password" type="password" required></div>
        <div>
          <label>Role</label>
          <select name="role">
            <option value="user">User</option>
            <option value="admin">Admin</option>
          </select>
        </div>
        <div class="full"><button type="submit">Create User</button></div>
      </form>
    </div>
  </div>

  <div class="card" style="margin-bottom:20px;">
    <h2>All Equipment</h2>
    <table>
      <thead>
        <tr>
          <th>Name</th>
          <th>Category</th>
          <th>Status</th>
          <th>Location</th>
          <th>Photo</th>
          <th>Manual</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody>
        {% for item in equipment %}
          <tr>
            <td>{{ item['name'] }}</td>
            <td>{{ item['category'] }}</td>
            <td><span class="status-badge {{ status_badge_class(item['status']) }}">{{ item['status'].replace('_', ' ').title() }}</span></td>
            <td>{{ item['location'] }}</td>
            <td>{{ 'Yes' if item['image_filename'] else 'No' }}</td>
            <td>{{ 'Yes' if item['manual_filename'] or item['manual_link'] else 'No' }}</td>
            <td class="actions">
              <a class="button secondary" href="{{ url_for('edit_equipment', equipment_id=item['id']) }}">Edit</a>
              <form method="post" action="{{ url_for('delete_equipment', equipment_id=item['id']) }}" class="inline" onsubmit="return confirm('Delete this equipment?');">
                <button type="submit" class="button red">Delete</button>
              </form>
            </td>
          </tr>
        {% else %}
          <tr><td colspan="7" class="muted">No equipment found.</td></tr>
        {% endfor %}
      </tbody>
    </table>
  </div>

  <div class="card">
    <h2>Existing Users</h2>
    <table>
      <thead>
        <tr><th>Username</th><th>Full Name</th><th>Role</th><th>Actions</th></tr>
      </thead>
      <tbody>
        {% for user in users %}
          <tr>
            <td>{{ user['username'] }}</td>
            <td>{{ user['full_name'] }}</td>
            <td>{{ user['role'] }}</td>
            <td class="actions">
              <a class="button secondary" href="{{ url_for('edit_user', user_id=user['id']) }}">Edit</a>
              {% if user['username'] != 'admin' %}
              <form method="post" action="{{ url_for('delete_user', user_id=user['id']) }}" class="inline" onsubmit="return confirm('Delete this user?');">
                <button type="submit" class="button red">Delete</button>
              </form>
              {% endif %}
            </td>
          </tr>
        {% else %}
          <tr><td colspan="4" class="muted">No users found.</td></tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</div>
{% endblock %}
"""

app.jinja_loader = DictLoader(
    {
        "base.html": BASE_TEMPLATE,
        "login.html": LOGIN_TEMPLATE,
        "dashboard.html": DASHBOARD_TEMPLATE,
        "form.html": FORM_TEMPLATE,
        "history.html": HISTORY_TEMPLATE,
        "admin.html": ADMIN_TEMPLATE,
    }
)


# =========================
# File routes
# =========================
@app.route("/uploads/images/<path:filename>")
@login_required
def uploaded_image(filename: str):
    return send_from_directory(IMAGE_FOLDER, filename)


@app.route("/uploads/manuals/<path:filename>")
@login_required
def uploaded_manual(filename: str):
    return send_from_directory(MANUAL_FOLDER, filename)


# =========================
# Routes
# =========================
@app.route("/")
def home():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = get_db().execute(
            "SELECT * FROM users WHERE username = ?",
            (username,),
        ).fetchone()

        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Invalid username or password.", "danger")
        else:
            session.clear()
            session["user_id"] = user["id"]
            session["role"] = user["role"]
            flash("Login successful.", "success")
            return redirect(url_for("dashboard"))

    return render_template_string(LOGIN_TEMPLATE)


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out successfully.", "success")
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    search = request.args.get("search", "").strip()
    category = request.args.get("category", "all").strip()
    selected_id = request.args.get("selected_id", type=int)

    sql = "SELECT * FROM equipment WHERE 1=1"
    params = []

    if search:
        sql += " AND (name LIKE ? OR location LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])

    if category and category != "all":
        sql += " AND category = ?"
        params.append(category)

    sql += " ORDER BY name ASC"
    equipment = db.execute(sql, params).fetchall()

    categories = [
        row["category"]
        for row in db.execute(
            "SELECT DISTINCT category FROM equipment ORDER BY category ASC"
        ).fetchall()
    ]

    selected = None
    if equipment:
        if selected_id:
            selected = next((row for row in equipment if row["id"] == selected_id), None)
        if selected is None:
            selected = equipment[0]

    active_checkout = None
    active_reservation = None
    if selected:
        active_checkout = db.execute(
            """
            SELECT c.*, u.full_name
            FROM checkouts c
            JOIN users u ON u.id = c.user_id
            WHERE c.equipment_id = ? AND c.status = 'active'
            ORDER BY c.id DESC LIMIT 1
            """,
            (selected["id"],),
        ).fetchone()

        active_reservation = db.execute(
            """
            SELECT r.*, u.full_name
            FROM reservations r
            JOIN users u ON u.id = r.user_id
            WHERE r.equipment_id = ? AND r.status = 'active'
            ORDER BY r.id DESC LIMIT 1
            """,
            (selected["id"],),
        ).fetchone()

    stats = {
        "total": db.execute("SELECT COUNT(*) AS count FROM equipment").fetchone()["count"],
        "available": db.execute("SELECT COUNT(*) AS count FROM equipment WHERE status = 'available'").fetchone()["count"],
        "in_use": db.execute("SELECT COUNT(*) AS count FROM equipment WHERE status = 'in_use'").fetchone()["count"],
        "reserved": db.execute("SELECT COUNT(*) AS count FROM equipment WHERE status = 'reserved'").fetchone()["count"],
    }

    return render_template_string(
        DASHBOARD_TEMPLATE,
        equipment=equipment,
        selected=selected,
        active_checkout=active_checkout,
        active_reservation=active_reservation,
        stats=stats,
        search=search,
        category=category,
        categories=categories,
    )


@app.route("/equipment/<int:equipment_id>/take", methods=["GET", "POST"])
@login_required
def take_equipment(equipment_id: int):
    db = get_db()
    equipment = get_equipment_or_404(equipment_id)

    if equipment["status"] != "available":
        flash("This equipment is not currently available.", "warning")
        return redirect(url_for("dashboard", selected_id=equipment_id))

    if request.method == "POST":
        date_taken = request.form.get("date_taken", str(date.today()))
        expected_return_date = request.form.get("expected_return_date", "").strip()
        note = request.form.get("note", "").strip()

        db.execute(
            """
            INSERT INTO checkouts (equipment_id, user_id, date_taken, expected_return_date, note, status)
            VALUES (?, ?, ?, ?, ?, 'active')
            """,
            (equipment_id, session["user_id"], date_taken, expected_return_date or None, note),
        )
        db.execute(
            "UPDATE equipment SET status = 'in_use', updated_at = ? WHERE id = ?",
            (utc_now_iso(), equipment_id),
        )
        db.commit()
        add_history(equipment_id, session["user_id"], "Borrowed", note)
        flash("Equipment checked out successfully.", "success")
        return redirect(url_for("dashboard", selected_id=equipment_id))

    form_body = """
    <div><label>Date Taken</label><input type='date' name='date_taken' value='{}' required></div>
    <div><label>Expected Return Date</label><input type='date' name='expected_return_date'></div>
    <div class='full'><label>Notes</label><textarea name='note' placeholder='Example: using for leak test'></textarea></div>
    """.format(str(date.today()))

    return render_template_string(
        FORM_TEMPLATE,
        heading="Take Equipment",
        subtitle="Fill out the checkout information below.",
        item_name=equipment["name"],
        item_subtitle=f"{equipment['location']} • {equipment['category']}",
        form_body=form_body,
        submit_label="Confirm Checkout",
        back_url=url_for("dashboard", selected_id=equipment["id"]),
    )


@app.route("/equipment/<int:equipment_id>/reserve", methods=["GET", "POST"])
@login_required
def reserve_equipment(equipment_id: int):
    db = get_db()
    equipment = get_equipment_or_404(equipment_id)

    if equipment["status"] == "maintenance":
        flash("Equipment under maintenance cannot be reserved.", "warning")
        return redirect(url_for("dashboard", selected_id=equipment_id))

    if request.method == "POST":
        start_date = request.form.get("start_date", str(date.today()))
        end_date = request.form.get("end_date", str(date.today()))
        note = request.form.get("note", "").strip()

        db.execute(
            """
            INSERT INTO reservations (equipment_id, user_id, start_date, end_date, note, status)
            VALUES (?, ?, ?, ?, ?, 'active')
            """,
            (equipment_id, session["user_id"], start_date, end_date, note),
        )
        db.execute(
            "UPDATE equipment SET status = 'reserved', updated_at = ? WHERE id = ?",
            (utc_now_iso(), equipment_id),
        )
        db.commit()
        add_history(equipment_id, session["user_id"], "Reserved", note)
        flash("Reservation created successfully.", "success")
        return redirect(url_for("dashboard", selected_id=equipment_id))

    form_body = """
    <div><label>Start Date</label><input type='date' name='start_date' value='{}' required></div>
    <div><label>End Date</label><input type='date' name='end_date' value='{}' required></div>
    <div class='full'><label>Notes</label><textarea name='note' placeholder='Example: calibration or weekend use'></textarea></div>
    """.format(str(date.today()), str(date.today()))

    return render_template_string(
        FORM_TEMPLATE,
        heading="Reserve Equipment",
        subtitle="Choose the reservation window below.",
        item_name=equipment["name"],
        item_subtitle=f"{equipment['location']} • {equipment['category']}",
        form_body=form_body,
        submit_label="Confirm Reservation",
        back_url=url_for("dashboard", selected_id=equipment["id"]),
    )


@app.route("/equipment/<int:equipment_id>/return", methods=["GET", "POST"])
@login_required
def return_equipment(equipment_id: int):
    db = get_db()
    equipment = get_equipment_or_404(equipment_id)

    if equipment["status"] not in {"in_use", "reserved"}:
        flash("This equipment is already available or not releasable.", "warning")
        return redirect(url_for("dashboard", selected_id=equipment_id))

    if request.method == "POST":
        note = request.form.get("note", "").strip()

        active_checkout = db.execute(
            "SELECT * FROM checkouts WHERE equipment_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
            (equipment_id,),
        ).fetchone()

        active_reservation = db.execute(
            "SELECT * FROM reservations WHERE equipment_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
            (equipment_id,),
        ).fetchone()

        if active_checkout:
            db.execute(
                "UPDATE checkouts SET status = 'returned', actual_return_date = ? WHERE id = ?",
                (str(date.today()), active_checkout["id"]),
            )

        if active_reservation:
            db.execute(
                "UPDATE reservations SET status = 'completed' WHERE id = ?",
                (active_reservation["id"],),
            )

        db.execute(
            "UPDATE equipment SET status = 'available', updated_at = ? WHERE id = ?",
            (utc_now_iso(), equipment_id),
        )
        db.commit()
        add_history(equipment_id, session["user_id"], "Returned", note or "Returned to lab")
        flash("Equipment marked as available.", "success")
        return redirect(url_for("dashboard", selected_id=equipment_id))

    form_body = """
    <div class='full'><label>Return Notes</label><textarea name='note' placeholder='Example: returned in good condition'></textarea></div>
    """

    return render_template_string(
        FORM_TEMPLATE,
        heading="Return / Release Equipment",
        subtitle="Add an optional note before marking this item available.",
        item_name=equipment["name"],
        item_subtitle=f"{equipment['location']} • {equipment['category']}",
        form_body=form_body,
        submit_label="Confirm Return",
        back_url=url_for("dashboard", selected_id=equipment["id"]),
    )


@app.route("/history")
@login_required
def history_page():
    rows = get_db().execute(
        """
        SELECT h.*, e.name AS equipment_name, u.full_name
        FROM history h
        JOIN equipment e ON e.id = h.equipment_id
        LEFT JOIN users u ON u.id = h.user_id
        ORDER BY h.id DESC
        LIMIT 100
        """
    ).fetchall()
    return render_template_string(HISTORY_TEMPLATE, history_rows=rows)


@app.route("/admin")
@admin_required
def admin_page():
    db = get_db()
    users = db.execute(
        "SELECT id, username, full_name, role FROM users ORDER BY username ASC"
    ).fetchall()
    equipment = db.execute(
        "SELECT * FROM equipment ORDER BY name ASC"
    ).fetchall()
    return render_template_string(ADMIN_TEMPLATE, users=users, equipment=equipment)


@app.route("/admin/equipment/add", methods=["POST"])
@admin_required
def add_equipment():
    name = request.form.get("name", "").strip()
    category = request.form.get("category", "").strip()
    serial_number = request.form.get("serial_number", "").strip()
    location = request.form.get("location", "").strip()
    manual_link = request.form.get("manual_link", "").strip()
    notes = request.form.get("notes", "").strip()

    image_file = request.files.get("image_file")
    manual_file = request.files.get("manual_file")

    if not name or not category or not location:
        flash("Name, category, and location are required.", "danger")
        return redirect(url_for("admin_page"))

    image_filename = None
    manual_filename = None

    if image_file and image_file.filename:
        if not allowed_file(image_file.filename, ALLOWED_IMAGE_EXTENSIONS):
            flash("Invalid image type.", "danger")
            return redirect(url_for("admin_page"))
        image_filename = save_uploaded_file(image_file, IMAGE_FOLDER, "img")

    if manual_file and manual_file.filename:
        if not allowed_file(manual_file.filename, ALLOWED_MANUAL_EXTENSIONS):
            flash("Invalid manual file type.", "danger")
            return redirect(url_for("admin_page"))
        manual_filename = save_uploaded_file(manual_file, MANUAL_FOLDER, "manual")

    now = utc_now_iso()
    try:
        db = get_db()
        db.execute(
            """
            INSERT INTO equipment (name, category, serial_number, location, status, notes, manual_link, image_filename, manual_filename, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'available', ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                category,
                serial_number or None,
                location,
                notes or None,
                manual_link or None,
                image_filename,
                manual_filename,
                now,
                now,
            ),
        )
        db.commit()
        flash("Equipment added successfully.", "success")
    except sqlite3.IntegrityError:
        flash("Equipment name must be unique.", "danger")

    return redirect(url_for("admin_page"))


@app.route("/admin/equipment/<int:equipment_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_equipment(equipment_id: int):
    db = get_db()
    equipment = get_equipment_or_404(equipment_id)

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        category = request.form.get("category", "").strip()
        serial_number = request.form.get("serial_number", "").strip()
        location = request.form.get("location", "").strip()
        status = request.form.get("status", "available").strip()
        manual_link = request.form.get("manual_link", "").strip()
        notes = request.form.get("notes", "").strip()

        remove_image = request.form.get("remove_image") == "yes"
        remove_manual = request.form.get("remove_manual") == "yes"

        image_file = request.files.get("image_file")
        manual_file = request.files.get("manual_file")

        if not name or not category or not location:
            flash("Name, category, and location are required.", "danger")
            return redirect(url_for("edit_equipment", equipment_id=equipment_id))

        if status not in {"available", "in_use", "reserved", "maintenance"}:
            status = "available"

        current_image = equipment["image_filename"]
        current_manual = equipment["manual_filename"]

        if remove_image and current_image:
            image_path = IMAGE_FOLDER / current_image
            if image_path.exists():
                image_path.unlink()
            current_image = None

        if remove_manual and current_manual:
            manual_path = MANUAL_FOLDER / current_manual
            if manual_path.exists():
                manual_path.unlink()
            current_manual = None

        if image_file and image_file.filename:
            if not allowed_file(image_file.filename, ALLOWED_IMAGE_EXTENSIONS):
                flash("Invalid image type.", "danger")
                return redirect(url_for("edit_equipment", equipment_id=equipment_id))
            if current_image:
                old_path = IMAGE_FOLDER / current_image
                if old_path.exists():
                    old_path.unlink()
            current_image = save_uploaded_file(image_file, IMAGE_FOLDER, "img")

        if manual_file and manual_file.filename:
            if not allowed_file(manual_file.filename, ALLOWED_MANUAL_EXTENSIONS):
                flash("Invalid manual file type.", "danger")
                return redirect(url_for("edit_equipment", equipment_id=equipment_id))
            if current_manual:
                old_path = MANUAL_FOLDER / current_manual
                if old_path.exists():
                    old_path.unlink()
            current_manual = save_uploaded_file(manual_file, MANUAL_FOLDER, "manual")

        try:
            db.execute(
                """
                UPDATE equipment
                SET name = ?, category = ?, serial_number = ?, location = ?, status = ?, manual_link = ?, notes = ?, image_filename = ?, manual_filename = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    name,
                    category,
                    serial_number or None,
                    location,
                    status,
                    manual_link or None,
                    notes or None,
                    current_image,
                    current_manual,
                    utc_now_iso(),
                    equipment_id,
                ),
            )
            db.commit()
            add_history(equipment_id, session["user_id"], "Edited", "Equipment details updated")
            flash("Equipment updated successfully.", "success")
            return redirect(url_for("admin_page"))
        except sqlite3.IntegrityError:
            flash("Equipment name must be unique.", "danger")

    selected_status = equipment["status"]

    current_photo_html = (
        f"<div class='full'><label>Current Photo</label><img class='equipment-photo' src='{url_for('uploaded_image', filename=equipment['image_filename'])}' alt='Equipment photo'></div>"
        if equipment["image_filename"]
        else "<div class='full muted'>No current photo uploaded.</div>"
    )

    current_manual_html = (
        f"<div class='full'><label>Current Manual</label><a href='{url_for('uploaded_manual', filename=equipment['manual_filename'])}' target='_blank' style='color:#1d4ed8;'>Open current uploaded manual</a></div>"
        if equipment["manual_filename"]
        else "<div class='full muted'>No uploaded manual file.</div>"
    )

    form_body = f"""
    <div class='full'><label>Name</label><input name='name' value="{equipment['name']}" required></div>
    <div><label>Category</label><input name='category' value="{equipment['category']}" required></div>
    <div><label>Serial Number</label><input name='serial_number' value="{equipment['serial_number'] or ''}"></div>
    <div class='full'><label>Location</label><input name='location' value="{equipment['location']}" required></div>
    <div><label>Status</label>
      <select name='status'>
        <option value='available' {'selected' if selected_status == 'available' else ''}>Available</option>
        <option value='in_use' {'selected' if selected_status == 'in_use' else ''}>In Use</option>
        <option value='reserved' {'selected' if selected_status == 'reserved' else ''}>Reserved</option>
        <option value='maintenance' {'selected' if selected_status == 'maintenance' else ''}>Maintenance</option>
      </select>
    </div>
    <div><label>Manual Link</label><input name='manual_link' value="{equipment['manual_link'] or ''}"></div>
    {current_photo_html}
    <div><label>New Equipment Photo</label><input type='file' name='image_file' accept='.png,.jpg,.jpeg,.gif,.webp'></div>
    <div><label>Remove Current Photo</label>
      <select name='remove_image'>
        <option value='no'>No</option>
        <option value='yes'>Yes</option>
      </select>
    </div>
    {current_manual_html}
    <div><label>New Manual File</label><input type='file' name='manual_file' accept='.pdf,.doc,.docx,.txt'></div>
    <div><label>Remove Current Manual</label>
      <select name='remove_manual'>
        <option value='no'>No</option>
        <option value='yes'>Yes</option>
      </select>
    </div>
    <div class='full'><label>Notes</label><textarea name='notes'>{equipment['notes'] or ''}</textarea></div>
    """

    return render_template_string(
        FORM_TEMPLATE,
        heading="Edit Equipment",
        subtitle="Update this equipment record.",
        item_name=equipment["name"],
        item_subtitle=f"{equipment['location']} • {equipment['category']}",
        form_body=form_body,
        submit_label="Save Changes",
        back_url=url_for("admin_page"),
    )


@app.route("/admin/equipment/<int:equipment_id>/delete", methods=["POST"])
@admin_required
def delete_equipment(equipment_id: int):
    db = get_db()
    equipment = get_equipment_or_404(equipment_id)

    active_checkout = db.execute(
        "SELECT id FROM checkouts WHERE equipment_id = ? AND status = 'active' LIMIT 1",
        (equipment_id,),
    ).fetchone()
    active_reservation = db.execute(
        "SELECT id FROM reservations WHERE equipment_id = ? AND status = 'active' LIMIT 1",
        (equipment_id,),
    ).fetchone()

    if active_checkout or active_reservation:
        flash("Cannot delete equipment with active checkout or reservation.", "danger")
        return redirect(url_for("admin_page"))

    if equipment["image_filename"]:
        path = IMAGE_FOLDER / equipment["image_filename"]
        if path.exists():
            path.unlink()

    if equipment["manual_filename"]:
        path = MANUAL_FOLDER / equipment["manual_filename"]
        if path.exists():
            path.unlink()

    db.execute("DELETE FROM history WHERE equipment_id = ?", (equipment_id,))
    db.execute("DELETE FROM checkouts WHERE equipment_id = ?", (equipment_id,))
    db.execute("DELETE FROM reservations WHERE equipment_id = ?", (equipment_id,))
    db.execute("DELETE FROM equipment WHERE id = ?", (equipment_id,))
    db.commit()

    flash(f"Equipment '{equipment['name']}' deleted.", "success")
    return redirect(url_for("admin_page"))


@app.route("/admin/user/add", methods=["POST"])
@admin_required
def add_user():
    username = request.form.get("username", "").strip()
    full_name = request.form.get("full_name", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "user").strip()

    if not username or not full_name or not password:
        flash("All user fields are required.", "danger")
        return redirect(url_for("admin_page"))

    if role not in {"admin", "user"}:
        role = "user"

    try:
        db = get_db()
        db.execute(
            """
            INSERT INTO users (username, full_name, password_hash, role, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (username, full_name, generate_password_hash(password), role, utc_now_iso()),
        )
        db.commit()
        flash("User created successfully.", "success")
    except sqlite3.IntegrityError:
        flash("Username already exists.", "danger")

    return redirect(url_for("admin_page"))


@app.route("/admin/user/<int:user_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_user(user_id: int):
    db = get_db()
    user = get_user_or_404(user_id)

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        full_name = request.form.get("full_name", "").strip()
        role = request.form.get("role", "user").strip()
        password = request.form.get("password", "").strip()

        if not username or not full_name:
            flash("Username and full name are required.", "danger")
            return redirect(url_for("edit_user", user_id=user_id))

        if role not in {"admin", "user"}:
            role = "user"

        try:
            if password:
                db.execute(
                    """
                    UPDATE users
                    SET username = ?, full_name = ?, role = ?, password_hash = ?
                    WHERE id = ?
                    """,
                    (
                        username,
                        full_name,
                        role,
                        generate_password_hash(password),
                        user_id,
                    ),
                )
            else:
                db.execute(
                    """
                    UPDATE users
                    SET username = ?, full_name = ?, role = ?
                    WHERE id = ?
                    """,
                    (
                        username,
                        full_name,
                        role,
                        user_id,
                    ),
                )
            db.commit()
            flash("User updated successfully.", "success")
            return redirect(url_for("admin_page"))
        except sqlite3.IntegrityError:
            flash("Username already exists.", "danger")

    selected_role = user["role"]
    form_body = f"""
    <div><label>Username</label><input name='username' value="{user['username']}" required></div>
    <div><label>Full Name</label><input name='full_name' value="{user['full_name']}" required></div>
    <div><label>Role</label>
      <select name='role'>
        <option value='user' {'selected' if selected_role == 'user' else ''}>User</option>
        <option value='admin' {'selected' if selected_role == 'admin' else ''}>Admin</option>
      </select>
    </div>
    <div><label>New Password</label><input type='password' name='password' placeholder='Leave blank to keep current password'></div>
    """

    return render_template_string(
        FORM_TEMPLATE,
        heading="Edit User",
        subtitle="Update user information.",
        item_name=user["full_name"],
        item_subtitle=f"Username: {user['username']}",
        form_body=form_body,
        submit_label="Save Changes",
        back_url=url_for("admin_page"),
    )


@app.route("/admin/user/<int:user_id>/delete", methods=["POST"])
@admin_required
def delete_user(user_id: int):
    db = get_db()
    user = get_user_or_404(user_id)

    if user["username"] == "admin":
        flash("Default admin user cannot be deleted.", "danger")
        return redirect(url_for("admin_page"))

    if session.get("user_id") == user_id:
        flash("You cannot delete your own logged-in account.", "danger")
        return redirect(url_for("admin_page"))

    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()

    flash(f"User '{user['username']}' deleted.", "success")
    return redirect(url_for("admin_page"))


if __name__ == "__main__":
    init_db()
    print("\\nLab Equipment Manager is ready.")
    print("Open: http://127.0.0.1:5000")
    print("Default admin login -> username: admin | password: admin123\\n")
    app.run(host="0.0.0.0", port=5000, debug=True)