import os
from datetime import datetime

from flask import Flask, render_template, request, redirect, jsonify, session
import sqlite3
import threading
import time

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "replace-with-a-secure-random-secret")  # Change in production

background_thread_started = False


def get_db():
    conn = sqlite3.connect("database.db")
    conn.row_factory = sqlite3.Row
    return conn


def get_cell_values(db, table_id):
    """Return a dict keyed by (row_id, column_id) -> cell data."""
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
    return nested


def apply_auto_updates():
    """Apply automatic changes for cells configured to auto update over time."""
    db = get_db()

    cells = db.execute(
        "SELECT * FROM cell_values WHERE auto_change=1 AND change_amount IS NOT NULL"
    ).fetchall()

    if not cells:
        return

    now = datetime.utcnow()

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

    db = get_db()
    viewer = db.execute(
        "SELECT * FROM viewers WHERE viewer_id=? AND password=?",
        (viewer_id, password),
    ).fetchone()

    if viewer:
        return redirect(f"/viewer/{viewer_id}")

    return "Viewer login failed"


@app.route("/viewer/<viewer_id>")
def viewer_view(viewer_id):

    db = get_db()
    viewer = db.execute(
        "SELECT * FROM viewers WHERE viewer_id=?",
        (viewer_id,),
    ).fetchone()

    if not viewer:
        return "Viewer not found"

    table = db.execute(
        "SELECT * FROM tables WHERE id=?",
        (viewer["table_id"],),
    ).fetchone()

    row = db.execute(
        "SELECT * FROM rows WHERE id=?",
        (viewer["row_id"],),
    ).fetchone()

    columns = db.execute(
        "SELECT * FROM columns WHERE table_id=?",
        (viewer["table_id"],),
    ).fetchall()

    cell_values = get_cell_values(db, viewer["table_id"])

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

    username = request.form["username"]
    password = request.form["password"]

    db = get_db()

    user = db.execute(
        "SELECT * FROM managers WHERE username=? AND password=?",
        (username, password),
    ).fetchone()

    if user:
        session["manager_id"] = user["id"]
        session["username"] = user["username"]
        return redirect("/dashboard")
    else:
        return "Login Failed"


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("register.html")

    username = request.form["username"]
    password = request.form["password"]

    db = get_db()

    db.execute(
        "INSERT INTO managers (username,password) VALUES (?,?)",
        (username, password),
    )

    db.commit()

    return redirect("/manager")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


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

    # Delete everything related to this table
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
    else:
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

    # For non-number columns, ignore auto-generation settings
    if col_type != "number":
        edit_mode = "direct"
        auto_change = 0
        change_type = None
        change_amount = None
        time_interval = None
    else:
        edit_mode = request.form.get("edit_mode", "direct")
        auto_change = 1 if request.form.get("auto_change") == "on" else 0
        change_type = None  # Now per-cell
        change_amount = None  # Now per-cell
        time_interval = request.form.get("time_interval")

        if change_amount:
            try:
                change_amount = float(change_amount)
            except Exception:
                change_amount = None

    db = get_db()

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

    # Ensure existing rows have a placeholder value for the new column
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
        "SELECT * FROM columns WHERE table_id= ?",
        (table_id,),
    ).fetchall()

    for col in columns:
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

    db.execute(
        "INSERT INTO viewers (viewer_id, password, table_id, row_id) VALUES (?, ?, ?, ?)",
        (viewer_id, password, table_id, row_id),
    )
    db.commit()

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
    else:
        # Redirect back to the table view
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


@app.before_request
def _start_background_tasks():
    global background_thread_started
    if not background_thread_started:
        # Start the auto-update thread once per process.
        start_auto_update_thread()
        background_thread_started = True


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=os.environ.get("FLASK_DEBUG", "1") == "1",
    )
