import sqlite3
from datetime import datetime


def add_column_if_not_exists(conn, table_name, column_name, definition):
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table_name})")
    existing = [row[1] for row in cur.fetchall()]
    if column_name not in existing:
        cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def main():
    conn = sqlite3.connect("database.db")
    cur = conn.cursor()

    # Managers
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS managers(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password TEXT
        )
        """
    )

    # Tables
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS tables(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            manager_id INTEGER
        )
        """
    )

    # Ensure table ownership column exists for older databases
    add_column_if_not_exists(conn, "tables", "manager_id", "INTEGER")

    # Columns
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS columns(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_id INTEGER,
            name TEXT,
            type TEXT
        )
        """
    )

    # Migrate columns table for newer schema
    add_column_if_not_exists(conn, "columns", "auto_change", "INTEGER DEFAULT 0")
    add_column_if_not_exists(conn, "columns", "change_type", "TEXT")
    add_column_if_not_exists(conn, "columns", "change_amount", "REAL")
    add_column_if_not_exists(conn, "columns", "time_interval", "TEXT")
    add_column_if_not_exists(conn, "columns", "edit_mode", "TEXT DEFAULT 'direct'")
    add_column_if_not_exists(conn, "columns", "last_updated", "TIMESTAMP")

    # Rows
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS rows(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # Cell values
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS cell_values(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            row_id INTEGER,
            column_id INTEGER,
            value TEXT,
            auto_change INTEGER DEFAULT 0,
            change_type TEXT,
            change_amount REAL,
            time_interval TEXT,
            last_updated TIMESTAMP
        )
        """
    )

    # Migrate cell_values table for newer schema
    add_column_if_not_exists(conn, "cell_values", "auto_change", "INTEGER DEFAULT 0")
    add_column_if_not_exists(conn, "cell_values", "change_type", "TEXT")
    add_column_if_not_exists(conn, "cell_values", "change_amount", "REAL")
    add_column_if_not_exists(conn, "cell_values", "time_interval", "TEXT")
    add_column_if_not_exists(conn, "cell_values", "last_updated", "TIMESTAMP")

    # Viewers (restricted access to a single row)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS viewers(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            viewer_id TEXT UNIQUE,
            password TEXT,
            table_id INTEGER,
            row_id INTEGER
        )
        """
    )

    conn.commit()
    conn.close()

    print("Database created/updated successfully")


if __name__ == "__main__":
    main()
