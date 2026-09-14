import os
import secrets

import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.environ.get("POSTGRES_URL") or os.environ.get("DATABASE_URL")
RETENTION_YEARS = 5
ALLOWED_EXT = {"png", "jpg", "jpeg", "gif", "bmp", "webp", "pdf", "dcm"}

_initialized = False


def connect():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db():
    """Idempotent — safe to call on every cold start."""
    global _initialized
    if _initialized:
        return
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS patients (
                id SERIAL PRIMARY KEY,
                full_name TEXT NOT NULL,
                birth_date TEXT,
                phone TEXT,
                allergies TEXT,
                chronic_conditions TEXT,
                notes TEXT,
                access_token TEXT UNIQUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS visits (
                id SERIAL PRIMARY KEY,
                patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
                visit_date TEXT NOT NULL,
                tooth_number TEXT,
                complaints TEXT,
                complications TEXT,
                diagnosis TEXT,
                treatment TEXT,
                materials TEXT,
                recommendations TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                id SERIAL PRIMARY KEY,
                visit_id INTEGER NOT NULL REFERENCES visits(id) ON DELETE CASCADE,
                blob_pathname TEXT NOT NULL,
                description TEXT,
                uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_sessions (
                chat_id BIGINT PRIMARY KEY,
                state TEXT NOT NULL DEFAULT 'idle',
                payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_visits_patient ON visits(patient_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_visit ON files(visit_id)")

        # migrate older deployments that predate access_token
        cols = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'patients'"
        ).fetchall()
        col_names = {c["column_name"] for c in cols}
        if "access_token" not in col_names:
            conn.execute("ALTER TABLE patients ADD COLUMN access_token TEXT UNIQUE")

        rows = conn.execute(
            "SELECT id FROM patients WHERE access_token IS NULL OR access_token = ''"
        ).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE patients SET access_token = %s WHERE id = %s", (gen_token(), r["id"])
            )
        conn.commit()
    _initialized = True


def gen_token():
    return secrets.token_urlsafe(16)


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT
