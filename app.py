import os
import mimetypes
from datetime import datetime, timedelta, timezone
from flask.sessions import SessionInterface, SessionMixin
from werkzeug.datastructures import CallbackDict
import json
import base64

from flask import Flask, render_template, request, redirect, jsonify, session, send_file, abort, flash
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
import threading
import time

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


class DatabaseSession(CallbackDict, SessionMixin):
    def __init__(self, initial=None, sid=None):
        if initial is None:
            initial = {}
        CallbackDict.__init__(self, initial, self.on_update)
        self.sid = sid
        self.modified = False

    def on_update(self, *args):
        self.modified = True


class DatabaseSessionInterface(SessionInterface):
    def __init__(self):
        pass

    def get_db(self):
        db_path = os.path.join(BASE_DIR, "database.db")
        conn = sqlite3.connect(db_path)
        return conn

    def open_session(self, app, request):
        sid = request.cookies.get(self.get_cookie_name(app))
        if not sid:
            sid = base64.b64encode(os.urandom(24)).decode('utf-8')
            return DatabaseSession(sid=sid)

        conn = self.get_db()
        cur = conn.cursor()
        cur.execute("SELECT data, expiry FROM sessions WHERE session_id = ?", (sid,))
        row = cur.fetchone()
        conn.close()

        if row:
            try:
                expiry = row[1]
                if isinstance(expiry, str):
                    expiry = datetime.fromisoformat(expiry)
                if expiry and expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                if expiry and expiry > datetime.now(timezone.utc):
                    return DatabaseSession(json.loads(row[0]), sid=sid)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass

        # Never reuse an expired or invalid session identifier.
        new_sid = base64.b64encode(os.urandom(24)).decode("utf-8")
        return DatabaseSession(sid=new_sid)

    def save_session(self, app, session, response):
        domain = self.get_cookie_domain(app)
        path = self.get_cookie_path(app)
        name = self.get_cookie_name(app)

        if not session:
            if session.sid:
                response.delete_cookie(name, domain=domain, path=path)
            return

        if not session.modified:
            return

        conn = self.get_db()
        cur = conn.cursor()

        expiry = datetime.now(timezone.utc) + timedelta(days=30)
        now = datetime.now(timezone.utc)
        cur.execute("SELECT session_id, expiry FROM sessions")
        for existing_sid, existing_expiry in cur.fetchall():
            try:
                parsed = datetime.fromisoformat(existing_expiry) if isinstance(existing_expiry, str) else existing_expiry
                if parsed and parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                if not parsed or parsed <= now:
                    cur.execute("DELETE FROM sessions WHERE session_id=?", (existing_sid,))
            except (TypeError, ValueError):
                cur.execute("DELETE FROM sessions WHERE session_id=?", (existing_sid,))

        data = json.dumps(dict(session))
        cur.execute("""
            INSERT OR REPLACE INTO sessions (session_id, data, expiry)
            VALUES (?, ?, ?)
        """, (session.sid, data, expiry.isoformat()))

        conn.commit()
        conn.close()

        response.set_cookie(
            name, session.sid, expires=expiry, httponly=True,
            secure=os.environ.get("COOKIE_SECURE", "").lower() in {"1", "true", "yes"},
            samesite="Lax", domain=domain, path=path
        )


app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-this-secret-before-deployment")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.session_interface = DatabaseSessionInterface()

UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)


def get_db():
    db_path = os.path.join(BASE_DIR, "database.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def normalize_image_path(path, db=None):
    if not path:
        return path
    s = str(path).replace('\\', '/')
    if s.startswith('/image/') or s.startswith('/static/') or s.startswith('http'):
        return s

    if db is not None:
        row = db.execute("SELECT id FROM images WHERE file_path = ?", (s,)).fetchone()
        if row:
            return f"/image/{row['id']}"

        fname = os.path.basename(s)
        row = db.execute("SELECT id FROM images WHERE filename = ? ORDER BY id DESC LIMIT 1", (fname,)).fetchone()
        if row:
            return f"/image/{row['id']}"

        row = db.execute("SELECT id FROM images WHERE file_path LIKE ? ORDER BY id DESC LIMIT 1", (f"%{fname}%",)).fetchone()
        if row:
            return f"/image/{row['id']}"

    return s


def save_uploaded_image(file, manager_id, db=None, column_id=None):
    filename = secure_filename(file.filename)
    if column_id is not None:
        filename = f"{manager_id}_{column_id}_{datetime.now(timezone.utc).timestamp()}_{filename}"
    else:
        filename = f"{manager_id}_{datetime.now(timezone.utc).timestamp()}_{filename}"
    file_path = os.path.join(UPLOAD_FOLDER, filename)
    file.save(file_path)
    relative_path = f"/static/uploads/{filename}"

    if db is not None:
        cursor = db.cursor()
        cursor.execute(
            "INSERT INTO images (manager_id, filename, file_path) VALUES (?, ?, ?)",
            (manager_id, file.filename, relative_path)
        )
        db.commit()
        image_id = cursor.lastrowid
        return relative_path, image_id

    return relative_path, None


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


from database_setup import main as setup_db
setup_db()

# Verify database has required tables
db = get_db()
managers_table = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='managers'").fetchone()
if not managers_table:
    app.logger.error("Managers table not found! Database setup may have failed.")
else:
    user_count = db.execute("SELECT COUNT(*) as count FROM managers").fetchone()["count"]
    app.logger.info(f"Database initialized successfully. {user_count} users found.")

background_thread_started = False


def get_cell_values(db, table_id):
    rows = db.execute("SELECT id FROM rows WHERE table_id=?", (table_id,)).fetchall()
    if not rows:
        return {}
    row_ids = [r["id"] for r in rows]
    placeholders = ",".join("?" for _ in row_ids)
    values = db.execute(
        f"SELECT * FROM cell_values WHERE row_id IN ({placeholders})",
        row_ids,
    ).fetchall()

    nested = {}
    for v in values:
        nested.setdefault(v["row_id"], {})[v["column_id"]] = v

    try:
        cols = db.execute("SELECT id, type FROM columns WHERE table_id=?", (table_id,)).fetchall()
        col_type_map = {c["id"]: c["type"] for c in cols}

        for rid, cols_dict in nested.items():
            for cid, cell in list(cols_dict.items()):
                try:
                    ctype = col_type_map.get(cid)
                except Exception:
                    ctype = None
                if ctype == 'image' and cell and cell.get('value'):
                    cols_dict[cid]['value'] = normalize_image_path(cell.get('value'), db)
    except Exception:
        pass

    return nested


def apply_auto_updates():
    db = get_db()
    cells = db.execute(
        "SELECT * FROM cell_values WHERE auto_change=1 AND change_amount IS NOT NULL"
    ).fetchall()

    if not cells:
        return

    now = datetime.now(timezone.utc)
    interval_seconds = {
        "hour": 60 * 60,
        "day": 60 * 60 * 24,
        "week": 60 * 60 * 24 * 7,
        "month": 60 * 60 * 24 * 30,
    }

    for cell in cells:
        if not cell["time_interval"] or not cell["change_amount"]:
            continue

        last_updated = cell["last_updated"]
        if last_updated:
            try:
                last_updated = datetime.fromisoformat(last_updated)
            except Exception:
                last_updated = None

        if not last_updated:
            db.execute(
                "UPDATE cell_values SET last_updated=? WHERE id=?",
                (now.isoformat(), cell["id"]),
            )
            continue

        delta = (now - last_updated).total_seconds()
        interval = interval_seconds.get(cell["time_interval"], 0)
        if interval <= 0:
            continue

        steps = int(delta // interval)
        if steps <= 0:
            continue

        multiplier = steps * (cell["change_amount"] or 0)
        if cell["change_type"] == "decrease":
            multiplier = -multiplier

        current_val = 0.0
        if cell["value"] is not None:
            try:
                current_val = float(cell["value"])
            except Exception:
                current_val = 0.0

        new_val = current_val + multiplier

        db.execute(
            "UPDATE cell_values SET value=?, last_updated=? WHERE id=?",
            (str(new_val), now.isoformat(), cell["id"]),
        )

    db.commit()


def start_auto_update_thread():
    def runner():
        while True:
            try:
                apply_auto_updates()
            except Exception:
                pass
            time.sleep(60)

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()


@app.route("/")
def index():
    return render_template("index.html")


def get_current_manager_id():
    return session.get("manager_id")


def require_login():
    if not get_current_manager_id():
        return redirect("/manager")
    return None


@app.route("/manager")
def manager():
    if get_current_manager_id():
        return redirect("/dashboard")
    return render_template("manager_login.html")


@app.route("/viewer")
def viewer_login_page():
    return render_template("viewer_login.html")


@app.route("/viewer_login", methods=["POST"])
def viewer_login():
    viewer_id = request.form.get("viewer_id")
    password = request.form.get("password")

    if not viewer_id or not password:
        flash("Viewer ID and password are required.", "error")
        return redirect("/viewer")

    db = get_db()
    viewer_id = viewer_id.strip()
    viewer = db.execute(
        "SELECT * FROM viewers WHERE viewer_id=?",
        (viewer_id,),
    ).fetchone()
    db.close()

    if viewer:
        stored_password = viewer["password"] or ""
        valid_password = False
        try:
            valid_password = check_password_hash(stored_password, password)
        except (ValueError, TypeError):
            valid_password = False

        # Upgrade legacy plaintext passwords after a successful login.
        if not valid_password and stored_password == password:
            valid_password = True
            db = get_db()
            db.execute(
                "UPDATE viewers SET password=? WHERE viewer_id=? AND password=?",
                (generate_password_hash(password), viewer_id, stored_password),
            )
            db.commit()
            db.close()

        if valid_password:
            session.clear()
            session.sid = base64.b64encode(os.urandom(24)).decode("utf-8")
            session["viewer_id"] = viewer_id
            session.permanent = True
            return redirect(f"/viewer/{viewer_id}")

    flash("Invalid viewer ID or password.", "error")
    return redirect("/viewer")


@app.route("/viewer/<viewer_id>")
def viewer_view(viewer_id):
    if session.get("viewer_id") != viewer_id:
        return redirect("/viewer")

    db = get_db()
    viewer = db.execute(
        "SELECT * FROM viewers WHERE viewer_id=?",
        (viewer_id,),
    ).fetchone()

    if not viewer:
        session.pop("viewer_id", None)
        db.close()
        return redirect("/viewer")

    table = db.execute(
        "SELECT * FROM tables WHERE id=?",
        (viewer["table_id"],),
    ).fetchone()

    row = db.execute(
        "SELECT * FROM rows WHERE id=? AND table_id=?",
        (viewer["row_id"], viewer["table_id"]),
    ).fetchone()

    columns = db.execute(
        "SELECT * FROM columns WHERE table_id=?",
        (viewer["table_id"],),
    ).fetchall()

    if not table or not row:
        db.close()
        return "Viewer data is no longer available.", 404

    cell_values = get_cell_values(db, viewer["table_id"])
    db.close()

    return render_template(
        "viewer_view.html",
        viewer=viewer,
        table=table,
        row=row,
        columns=columns,
        cell_values=cell_values,
    )


@app.route("/login", methods=["POST"])
def login():
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""

    if not username or not password:
        flash("Username and password are required.", "error")
        return redirect("/manager")

    db = None
    try:
        db = get_db()
        user = db.execute(
            "SELECT id, username, password FROM managers WHERE username=?",
            (username,),
        ).fetchone()

        if not user:
            flash("Invalid username or password.", "error")
            return redirect("/manager")

        stored_password = user["password"] or ""
        valid_password = False
        try:
            valid_password = check_password_hash(stored_password, password)
        except (ValueError, TypeError):
            valid_password = False

        # Support accounts created by older versions that stored plaintext passwords,
        # and upgrade the password to a hash after the owner signs in successfully.
        if not valid_password and stored_password == password:
            valid_password = True
            db.execute(
                "UPDATE managers SET password=? WHERE id=?",
                (generate_password_hash(password), user["id"]),
            )
            db.commit()

        if not valid_password:
            app.logger.warning("Failed manager login attempt for username: %s", username)
            flash("Invalid username or password.", "error")
            return redirect("/manager")

        session.clear()
        session.sid = base64.b64encode(os.urandom(24)).decode("utf-8")
        session["manager_id"] = user["id"]
        session["username"] = user["username"]
        session.permanent = True
        app.logger.info("Manager logged in: %s", username)
        return redirect("/dashboard")

    except sqlite3.Error:
        app.logger.exception("Database error during manager login")
        flash("Sign-in is temporarily unavailable. Please try again.", "error")
        return redirect("/manager")
    except Exception:
        app.logger.exception("Unexpected error during manager login")
        flash("Something went wrong while signing in. Please try again.", "error")
        return redirect("/manager")
    finally:
        if db is not None:
            db.close()


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("register.html")

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()

    if not username or not password:
        flash("Username and password are required.", "error")
        return redirect("/register")

    if len(username) < 3:
        flash("Username must be at least 3 characters long.", "error")
        return redirect("/register")

    if len(password) < 6:
        flash("Password must be at least 6 characters long.", "error")
        return redirect("/register")

    hashed_password = generate_password_hash(password)
    db = get_db()

    try:
        existing_user = db.execute(
            "SELECT id FROM managers WHERE username = ?",
            (username,)
        ).fetchone()

        if existing_user:
            flash("Username already exists. Please choose a different username.", "error")
            return redirect("/register")

        db.execute(
            "INSERT INTO managers (username, password) VALUES (?, ?)",
            (username, hashed_password)
        )
        db.commit()

        app.logger.info(f"New user registered: {username}")
        flash("Account created successfully. Please sign in.", "success")
        return redirect("/manager")

    except Exception as e:
        app.logger.error(f"Error registering user: {e}")
        flash("Registration failed. Please try again.", "error")
        return redirect("/register")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been signed out.", "success")
    return redirect("/")


@app.route("/viewer_logout")
def viewer_logout():
    session.clear()
    flash("You have been signed out.", "success")
    return redirect("/viewer")


@app.route("/debug")
def debug():
    db = get_db()
    users = db.execute("SELECT id, username FROM managers").fetchall()
    user_list = "".join(f"<li>{user['username']} (ID: {user['id']})</li>" for user in users)
    return f"""
    <h1>Debug Info</h1>
    <p>Session: {dict(session)}</p>
    <p>Users in DB: {len(users)}</p>
    <ul>{user_list}</ul>
    """


@app.route("/dashboard")
def dashboard():
    login_check = require_login()
    if login_check:
        return login_check

    manager_id = get_current_manager_id()
    q = request.args.get("q", "")

    db = get_db()

    if q:
        tables = db.execute(
            "SELECT * FROM tables WHERE manager_id=? AND name LIKE ?",
            (manager_id, f"%{q}%"),
        ).fetchall()
    else:
        tables = db.execute(
            "SELECT * FROM tables WHERE manager_id=?",
            (manager_id,),
        ).fetchall()

    return render_template("dashboard.html", tables=tables, q=q)


@app.route("/table/<int:table_id>")
def open_table(table_id):
    login_check = require_login()
    if login_check:
        return login_check

    manager_id = get_current_manager_id()
    q = request.args.get("q", "").strip()

    db = get_db()

    table = db.execute(
        "SELECT * FROM tables WHERE id=? AND manager_id=?",
        (table_id, manager_id),
    ).fetchone()

    if not table:
        return "Table not found or access denied", 404

    columns = db.execute(
        "SELECT * FROM columns WHERE table_id=?",
        (table_id,),
    ).fetchall()

    rows = db.execute(
        "SELECT * FROM rows WHERE table_id=? ORDER BY id",
        (table_id,),
    ).fetchall()

    cell_values = get_cell_values(db, table_id)

    if q:
        q_lower = q.lower()

        def row_matches(row):
            if q_lower in str(row["id"]).lower():
                return True
            values = cell_values.get(row["id"], {})
            for v in values.values():
                if v is not None and q_lower in str(v).lower():
                    return True
            return False

        rows = [r for r in rows if row_matches(r)]

    return render_template(
        "table.html",
        table=table,
        columns=columns,
        rows=rows,
        cell_values=cell_values,
        q=q,
    )


@app.route("/delete_table/<int:table_id>", methods=["POST"])
def delete_table(table_id):
    login_check = require_login()
    if login_check:
        return login_check

    manager_id = get_current_manager_id()
    db = get_db()

    table = db.execute(
        "SELECT * FROM tables WHERE id=? AND manager_id=?",
        (table_id, manager_id),
    ).fetchone()

    if not table:
        return "Table not found or access denied", 404

    db.execute("DELETE FROM viewers WHERE table_id=?", (table_id,))
    rows = db.execute("SELECT id FROM rows WHERE table_id=?", (table_id,)).fetchall()
    row_ids = [r["id"] for r in rows]
    if row_ids:
        placeholders = ",".join("?" for _ in row_ids)
        db.execute(f"DELETE FROM cell_values WHERE row_id IN ({placeholders})", row_ids)
        db.execute(f"DELETE FROM viewers WHERE row_id IN ({placeholders})", row_ids)
        db.execute(f"DELETE FROM rows WHERE id IN ({placeholders})", row_ids)

    db.execute("DELETE FROM columns WHERE table_id=?", (table_id,))
    db.execute("DELETE FROM tables WHERE id=?", (table_id,))
    db.commit()

    return redirect("/dashboard")


@app.route("/delete_row/<int:row_id>", methods=["POST"])
def delete_row(row_id):
    login_check = require_login()
    if login_check:
        return login_check

    manager_id = get_current_manager_id()
    db = get_db()

    table = db.execute(
        "SELECT t.id, t.manager_id FROM tables t JOIN rows r ON r.table_id=t.id WHERE r.id=?",
        (row_id,),
    ).fetchone()

    if not table or table["manager_id"] != manager_id:
        return "Row not found or access denied", 404

    table_id = table["id"]

    db.execute("DELETE FROM cell_values WHERE row_id=?", (row_id,))
    db.execute("DELETE FROM viewers WHERE row_id=?", (row_id,))
    db.execute("DELETE FROM rows WHERE id=?", (row_id,))
    db.commit()

    if 'X-Requested-With' in request.headers:
        return jsonify({"success": True})
    if table_id:
        return redirect(f"/table/{table_id}")
    return redirect("/dashboard")


@app.route("/create_column/<int:table_id>", methods=["POST"])
def create_column(table_id):
    login_check = require_login()
    if login_check:
        return login_check

    manager_id = get_current_manager_id()
    db = get_db()
    table = db.execute(
        "SELECT * FROM tables WHERE id=? AND manager_id=?",
        (table_id, manager_id),
    ).fetchone()

    if not table:
        return "Table not found or access denied", 404

    name = request.form.get("name")
    col_type = request.form.get("type", "text")

    if col_type != "number":
        edit_mode = "direct"
        auto_change = 0
        change_type = None
        change_amount = None
        time_interval = None
    else:
        edit_mode = request.form.get("edit_mode", "direct")
        auto_change = 1 if request.form.get("auto_change") == "on" else 0
        change_type = None
        change_amount = None
        time_interval = request.form.get("time_interval")

        if change_amount:
            try:
                change_amount = float(change_amount)
            except Exception:
                change_amount = None

    db.execute(
        "INSERT INTO columns (table_id, name, type, auto_change, change_type, change_amount, time_interval, edit_mode) VALUES (?,?,?,?,?,?,?,?)",
        (
            table_id,
            name,
            col_type,
            auto_change,
            change_type,
            change_amount,
            time_interval,
            edit_mode,
        ),
    )

    col_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    rows = db.execute("SELECT id FROM rows WHERE table_id=?", (table_id,)).fetchall()
    for r in rows:
        db.execute(
            "INSERT INTO cell_values (row_id, column_id, value) VALUES (?, ?, ?)",
            (r["id"], col_id, ""),
        )

    db.commit()
    return redirect(f"/table/{table_id}")


@app.route("/create_row/<int:table_id>", methods=["POST"])
def create_row(table_id):
    login_check = require_login()
    if login_check:
        return login_check

    manager_id = get_current_manager_id()
    db = get_db()

    table = db.execute(
        "SELECT * FROM tables WHERE id=? AND manager_id=?",
        (table_id, manager_id),
    ).fetchone()

    if not table:
        return "Table not found or access denied", 404

    cursor = db.cursor()
    cursor.execute(
        "INSERT INTO rows (table_id) VALUES (?)",
        (table_id,),
    )
    row_id = cursor.lastrowid

    columns = db.execute(
        "SELECT * FROM columns WHERE table_id=?",
        (table_id,),
    ).fetchall()

    for col in columns:
        value = ""
        if col["type"] == "image":
            file_key = f"col_{col['id']}"
            if file_key in request.files:
                file = request.files[file_key]
                if file and file.filename and allowed_file(file.filename):
                    try:
                        rel_path, image_id = save_uploaded_image(file, manager_id, db, col['id'])
                        value = f"/image/{image_id}" if image_id else rel_path
                    except Exception as e:
                        app.logger.error(f"Error uploading image: {e}")
        else:
            value = request.form.get(f"col_{col['id']}", "")

        auto_change = 1 if col["auto_change"] and col["type"] == "number" else 0
        change_type = request.form.get(f"change_type_{col['id']}") if auto_change else None
        change_amount_str = request.form.get(f"change_amount_{col['id']}") if auto_change else None
        change_amount = None
        if change_amount_str:
            try:
                change_amount = float(change_amount_str)
            except ValueError:
                pass
        time_interval = col["time_interval"] if auto_change else None

        cursor.execute(
            "INSERT INTO cell_values (row_id, column_id, value, auto_change, change_type, change_amount, time_interval) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (row_id, col["id"], value, auto_change, change_type, change_amount, time_interval),
        )

    db.commit()
    return redirect(f"/table/{table_id}")


@app.route("/create_viewer", methods=["POST"])
def create_viewer():
    login_check = require_login()
    if login_check:
        return login_check

    manager_id = get_current_manager_id()
    table_id = request.form.get("table_id")
    row_id = request.form.get("row_id")
    viewer_id = request.form.get("viewer_id")
    password = request.form.get("password")

    db = get_db()
    table = db.execute(
        "SELECT * FROM tables WHERE id=? AND manager_id=?",
        (table_id, manager_id),
    ).fetchone()

    if not table:
        return "Table not found or access denied", 404

    viewer_id = (viewer_id or "").strip()
    password = password or ""
    row_id = (row_id or "").strip()

    if len(viewer_id) < 3 or len(password) < 6:
        flash("Viewer ID must be at least 3 characters and password at least 6 characters.", "error")
        db.close()
        return redirect(f"/table/{table_id}")

    row = db.execute(
        "SELECT id FROM rows WHERE id=? AND table_id=?",
        (row_id, table_id),
    ).fetchone()
    if not row:
        flash("Choose a valid row for this viewer.", "error")
        db.close()
        return redirect(f"/table/{table_id}")

    try:
        db.execute(
            "INSERT INTO viewers (viewer_id, password, table_id, row_id) VALUES (?, ?, ?, ?)",
            (viewer_id, generate_password_hash(password), table_id, row_id),
        )
        db.commit()
        flash("Viewer access created successfully.", "success")
    except sqlite3.IntegrityError:
        flash("That Viewer ID already exists. Choose a different one.", "error")
    finally:
        db.close()

    return redirect(f"/table/{table_id}")


@app.route("/update_cell/<int:row_id>/<int:column_id>", methods=["POST"])
def update_cell(row_id, column_id):
    login_check = require_login()
    if login_check:
        return login_check

    manager_id = get_current_manager_id()
    db = get_db()

    column = db.execute(
        "SELECT c.* FROM columns c JOIN tables t ON c.table_id=t.id WHERE c.id=? AND t.manager_id=?",
        (column_id, manager_id),
    ).fetchone()

    if not column:
        return "Column not found or access denied", 404

    existing = db.execute(
        "SELECT * FROM cell_values WHERE row_id=? AND column_id=?",
        (row_id, column_id),
    ).fetchone()

    value = ""

    if column["type"] == "image":
        if 'value' in request.files:
            file = request.files['value']
            if file and file.filename and allowed_file(file.filename):
                try:
                    rel_path, image_id = save_uploaded_image(file, manager_id, db, column_id)
                    value = f"/image/{image_id}" if image_id else rel_path
                except Exception as e:
                    app.logger.error(f"Error uploading image: {e}")
        else:
            value = existing["value"] if existing else ""
    else:
        value = request.form.get("value", "")

    if column["type"] == "number":
        if column["edit_mode"] == "systematic":
            op = request.form.get("operation")
            amt = request.form.get("amount")
            try:
                amt = float(amt)
            except Exception:
                amt = 0

            current = 0.0
            if existing and existing["value"]:
                try:
                    current = float(existing["value"])
                except Exception:
                    current = 0.0

            if op == "decrease":
                current -= amt
            else:
                current += amt

            value = str(current)

    if existing:
        db.execute(
            "UPDATE cell_values SET value=? WHERE row_id=? AND column_id=?",
            (value, row_id, column_id),
        )
    else:
        db.execute(
            "INSERT INTO cell_values (row_id, column_id, value) VALUES (?, ?, ?)",
            (row_id, column_id, value),
        )

    db.commit()

    if 'X-Requested-With' in request.headers:
        return jsonify({"success": True})

    table = db.execute(
        "SELECT table_id FROM rows WHERE id=?",
        (row_id,),
    ).fetchone()
    table_id = table["table_id"] if table else None
    if table_id:
        return redirect(f"/table/{table_id}")
    return redirect("/dashboard")


@app.route("/create_table", methods=["POST"])
def create_table():
    login_check = require_login()
    if login_check:
        return login_check

    manager_id = get_current_manager_id()
    name = request.form["name"]

    db = get_db()
    db.execute(
        "INSERT INTO tables (name, manager_id) VALUES (?, ?)",
        (name, manager_id),
    )
    db.commit()

    return redirect("/dashboard")


@app.route("/upload_image", methods=["POST"])
def upload_image():
    login_check = require_login()
    if login_check:
        return login_check

    manager_id = get_current_manager_id()

    if 'file' not in request.files:
        return "No file provided", 400

    file = request.files['file']

    if file.filename == '':
        return "No file selected", 400

    if not allowed_file(file.filename):
        return "File type not allowed. Allowed: png, jpg, jpeg, gif, webp", 400

    try:
        rel_path, image_id = save_uploaded_image(file, manager_id, get_db())
        response_path = f"/image/{image_id}" if image_id else rel_path
        return jsonify({"success": True, "filename": os.path.basename(rel_path), "path": response_path})
    except Exception as e:
        app.logger.error(f"Error uploading image: {e}")
        return "Error uploading file", 500


@app.route("/gallery")
def gallery():
    login_check = require_login()
    if login_check:
        return login_check

    manager_id = get_current_manager_id()
    db = get_db()
    search_query = request.args.get('q', '').strip()

    query = """
        SELECT 
            cv.value as image_path,
            cv.row_id,
            cv.column_id,
            t.name as table_name,
            c.name as column_name,
            r.created_at as uploaded_at
        FROM cell_values cv
        JOIN columns c ON cv.column_id = c.id
        JOIN tables t ON c.table_id = t.id
        JOIN rows r ON cv.row_id = r.id
        WHERE t.manager_id = ?
        AND c.type = 'image'
        AND cv.value != ''
        AND cv.value IS NOT NULL
    """

    params = [manager_id]

    if search_query:
        query += " AND (t.name LIKE ? OR c.name LIKE ? OR cv.value LIKE ?)"
        search_param = f"%{search_query}%"
        params.extend([search_param, search_param, search_param])

    query += " ORDER BY r.created_at DESC"

    images = db.execute(query, params).fetchall()
    normalized_images = []
    try:
        for img in images:
            imgd = dict(img)
            path = imgd.get('image_path') or imgd.get('file_path') or ''
            imgd['image_path'] = normalize_image_path(path, db)
            normalized_images.append(imgd)
    except Exception:
        normalized_images = [dict(i) for i in images]

    return render_template("gallery.html", images=normalized_images, search_query=search_query)


@app.route('/image/<int:image_id>')
def serve_image(image_id):
    db = get_db()
    row = db.execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()
    if not row:
        abort(404)

    file_path = row['file_path']
    if not file_path:
        abort(404)

    path = str(file_path).replace('\\', '/')
    if path.startswith('/static/'):
        local_path = os.path.join(BASE_DIR, *path.lstrip('/').split('/'))
        if os.path.isfile(local_path):
            mime_type = mimetypes.guess_type(local_path)[0] or 'application/octet-stream'
            return send_file(local_path, mimetype=mime_type)

    if os.path.isabs(path):
        local_path = path
        if os.path.isfile(local_path):
            mime_type = mimetypes.guess_type(local_path)[0] or 'application/octet-stream'
            return send_file(local_path, mimetype=mime_type)

    abort(404)


@app.before_request
def _start_background_tasks():
    global background_thread_started
    if not background_thread_started:
        start_auto_update_thread()
        background_thread_started = True


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=os.environ.get("FLASK_DEBUG", "1") == "1",
    )
