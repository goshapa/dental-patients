import os
from datetime import date, datetime

from flask import Flask, Response, g, render_template, request, redirect, url_for
from werkzeug.utils import secure_filename
from vercel.blob import put as blob_put, get as blob_get

import db as dbmod
import bot_logic

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "local-dental-records-app")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB per upload

dbmod.init_db()


def get_db():
    if "db" not in g:
        g.db = dbmod.connect()
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def upload_attachment(patient_id, visit_id, filename, data):
    safe_name = secure_filename(filename) or "file"
    pathname = f"patients/{patient_id}/visits/{visit_id}/{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{safe_name}"
    blob_put(pathname, body=data, access="private", overwrite=True)
    return pathname


@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    db = get_db()
    if q:
        like = f"%{q}%"
        patients = db.execute(
            "SELECT * FROM patients WHERE full_name ILIKE %s OR phone ILIKE %s OR birth_date ILIKE %s ORDER BY full_name",
            (like, like, like),
        ).fetchall()
    else:
        patients = db.execute("SELECT * FROM patients ORDER BY full_name").fetchall()

    patients_data = []
    for p in patients:
        last_visit = db.execute(
            "SELECT visit_date FROM visits WHERE patient_id = %s ORDER BY visit_date DESC LIMIT 1",
            (p["id"],),
        ).fetchone()
        patients_data.append({**p, "last_visit": last_visit["visit_date"] if last_visit else None})

    return render_template("index.html", patients=patients_data, q=q)


@app.route("/patients/new", methods=["GET", "POST"])
def new_patient():
    if request.method == "POST":
        db = get_db()
        row = db.execute(
            "INSERT INTO patients (full_name, birth_date, phone, allergies, chronic_conditions, notes, access_token, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                request.form["full_name"].strip(),
                request.form.get("birth_date", "").strip(),
                request.form.get("phone", "").strip(),
                request.form.get("allergies", "").strip(),
                request.form.get("chronic_conditions", "").strip(),
                request.form.get("notes", "").strip(),
                dbmod.gen_token(),
                datetime.now(),
            ),
        ).fetchone()
        db.commit()
        return redirect(url_for("patient_detail", patient_id=row["id"]))
    return render_template("patient_form.html")


@app.route("/patients/<int:patient_id>")
def patient_detail(patient_id):
    db = get_db()
    patient = db.execute("SELECT * FROM patients WHERE id = %s", (patient_id,)).fetchone()
    if patient is None:
        return "Пациент не найден", 404
    return _render_patient(db, patient)


@app.route("/card/<token>")
def patient_card(token):
    """Direct, per-patient link (shared by the bot) — same card, addressed by an unguessable token."""
    db = get_db()
    patient = db.execute("SELECT * FROM patients WHERE access_token = %s", (token,)).fetchone()
    if patient is None:
        return "Ссылка недействительна", 404
    return _render_patient(db, patient)


def _render_patient(db, patient):
    visits = db.execute(
        "SELECT * FROM visits WHERE patient_id = %s ORDER BY visit_date DESC, id DESC", (patient["id"],)
    ).fetchall()
    visits_data = []
    for v in visits:
        files = db.execute("SELECT * FROM files WHERE visit_id = %s ORDER BY uploaded_at", (v["id"],)).fetchall()
        visits_data.append({**v, "files": files})
    return render_template("patient.html", patient=patient, visits=visits_data, today=date.today().isoformat())


@app.route("/patients/<int:patient_id>/edit", methods=["GET", "POST"])
def edit_patient(patient_id):
    db = get_db()
    patient = db.execute("SELECT * FROM patients WHERE id = %s", (patient_id,)).fetchone()
    if patient is None:
        return "Пациент не найден", 404
    if request.method == "POST":
        db.execute(
            "UPDATE patients SET full_name=%s, birth_date=%s, phone=%s, allergies=%s, chronic_conditions=%s, notes=%s WHERE id=%s",
            (
                request.form["full_name"].strip(),
                request.form.get("birth_date", "").strip(),
                request.form.get("phone", "").strip(),
                request.form.get("allergies", "").strip(),
                request.form.get("chronic_conditions", "").strip(),
                request.form.get("notes", "").strip(),
                patient_id,
            ),
        )
        db.commit()
        return redirect(url_for("patient_detail", patient_id=patient_id))
    return render_template("patient_form.html", patient=patient)


@app.route("/patients/<int:patient_id>/delete", methods=["POST"])
def delete_patient(patient_id):
    db = get_db()
    db.execute("DELETE FROM patients WHERE id = %s", (patient_id,))
    db.commit()
    return redirect(url_for("index"))


@app.route("/patients/<int:patient_id>/visits/new", methods=["POST"])
def new_visit(patient_id):
    db = get_db()
    patient = db.execute("SELECT id FROM patients WHERE id = %s", (patient_id,)).fetchone()
    if patient is None:
        return "Пациент не найден", 404

    row = db.execute(
        "INSERT INTO visits (patient_id, visit_date, tooth_number, complaints, complications, diagnosis, "
        "treatment, materials, recommendations, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (
            patient_id,
            request.form.get("visit_date") or date.today().isoformat(),
            request.form.get("tooth_number", "").strip(),
            request.form.get("complaints", "").strip(),
            request.form.get("complications", "").strip(),
            request.form.get("diagnosis", "").strip(),
            request.form.get("treatment", "").strip(),
            request.form.get("materials", "").strip(),
            request.form.get("recommendations", "").strip(),
            datetime.now(),
        ),
    ).fetchone()
    visit_id = row["id"]

    for f in request.files.getlist("attachments"):
        if f and f.filename and dbmod.allowed_file(f.filename):
            pathname = upload_attachment(patient_id, visit_id, f.filename, f.read())
            db.execute(
                "INSERT INTO files (visit_id, blob_pathname, description, uploaded_at) VALUES (%s, %s, %s, %s)",
                (visit_id, pathname, f.filename, datetime.now()),
            )
    db.commit()

    return redirect(url_for("patient_detail", patient_id=patient_id))


@app.route("/visits/<int:visit_id>/delete", methods=["POST"])
def delete_visit(visit_id):
    db = get_db()
    row = db.execute("SELECT patient_id FROM visits WHERE id = %s", (visit_id,)).fetchone()
    if row is None:
        return "Запись не найдена", 404
    patient_id = row["patient_id"]
    db.execute("DELETE FROM visits WHERE id = %s", (visit_id,))
    db.commit()
    return redirect(url_for("patient_detail", patient_id=patient_id))


@app.route("/files/<int:file_id>")
def serve_file(file_id):
    db = get_db()
    row = db.execute("SELECT * FROM files WHERE id = %s", (file_id,)).fetchone()
    if row is None:
        return "Файл не найден", 404
    result = blob_get(row["blob_pathname"], access="private")
    return Response(
        result.content,
        mimetype=result.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'inline; filename="{row["description"] or "file"}"',
            "Cache-Control": "private, no-cache",
        },
    )


@app.route("/retention")
def retention_check():
    """Patients whose last visit is older than RETENTION_YEARS — candidates for deletion."""
    db = get_db()
    cutoff = date.today().replace(year=date.today().year - dbmod.RETENTION_YEARS).isoformat()
    rows = db.execute(
        """
        SELECT p.*, MAX(v.visit_date) AS last_visit
        FROM patients p LEFT JOIN visits v ON v.patient_id = p.id
        GROUP BY p.id
        HAVING MAX(v.visit_date) IS NOT NULL AND MAX(v.visit_date) < %s
        ORDER BY last_visit
        """,
        (cutoff,),
    ).fetchall()
    return render_template("retention.html", patients=rows, cutoff=cutoff, years=dbmod.RETENTION_YEARS)


@app.route("/api/telegram", methods=["POST"])
def telegram_webhook():
    update = request.get_json(force=True, silent=True) or {}
    try:
        bot_logic.handle_update(update, get_db())
        get_db().commit()
    except Exception:
        app.logger.exception("telegram webhook error")
    return {"ok": True}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
