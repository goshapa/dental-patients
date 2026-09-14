"""Stateless Telegram webhook handler.

Runs inside a Vercel Function (one process per request), so there is no
long-lived polling loop and no in-memory conversation state — the current
step of each chat's conversation is stored in the `bot_sessions` table and
read back on every request.
"""

import os
from datetime import date, datetime

import requests
from psycopg.types.json import Json

import db as dbmod

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TELEGRAM_FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}"
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
ALLOWED_IDS = {
    int(x) for x in os.environ.get("ALLOWED_TELEGRAM_ID", "").replace(";", ",").split(",") if x.strip().isdigit()
}

SKIP_WORDS = {"-", "—", "пропустить", "нет"}

HELP_TEXT = (
    "Привет! Я веду карты пациентов.\n\n"
    "/new_patient — добавить нового пациента\n"
    "/find Иванова — найти пациента и получить ссылку на карту\n"
    "/cancel — отменить текущий ввод\n\n"
    "Можно просто написать фамилию текстом — это то же самое, что /find."
)


def clean(text):
    text = (text or "").strip()
    return "" if text.lower() in SKIP_WORDS else text


def card_link(token):
    return f"{BASE_URL}/card/{token}"


# ---------- Telegram Bot API ----------

def send_message(chat_id, text, buttons=None):
    payload = {"chat_id": chat_id, "text": text}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=10)


def answer_callback_query(callback_query_id):
    requests.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": callback_query_id}, timeout=10)


def get_file_path(file_id):
    resp = requests.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id}, timeout=10).json()
    return resp.get("result", {}).get("file_path")


def download_file(file_path):
    resp = requests.get(f"{TELEGRAM_FILE_API}/{file_path}", timeout=30)
    resp.raise_for_status()
    return resp.content


# ---------- session (per-chat conversation state, stored in Postgres) ----------

def get_session(db, chat_id):
    row = db.execute("SELECT state, payload FROM bot_sessions WHERE chat_id = %s", (chat_id,)).fetchone()
    if row is None:
        return {"state": "idle", "payload": {}}
    return {"state": row["state"], "payload": row["payload"] or {}}


def set_session(db, chat_id, state, payload):
    db.execute(
        """
        INSERT INTO bot_sessions (chat_id, state, payload, updated_at) VALUES (%s, %s, %s, now())
        ON CONFLICT (chat_id) DO UPDATE SET state = EXCLUDED.state, payload = EXCLUDED.payload, updated_at = now()
        """,
        (chat_id, state, Json(payload)),
    )


def clear_session(db, chat_id):
    set_session(db, chat_id, "idle", {})


# ---------- helpers ----------

def upload_pending_file(patient_id, chat_id, original_name, data):
    from vercel.blob import put as blob_put
    import re

    safe = re.sub(r"[^A-Za-z0-9._-]", "_", original_name) or "file"
    pathname = f"patients/{patient_id}/uploads/{chat_id}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{safe}"
    blob_put(pathname, body=data, access="private", overwrite=True)
    return pathname


def do_find(db, chat_id, query):
    query = (query or "").strip()
    if not query:
        send_message(chat_id, "Использование: /find Иванова")
        return
    like = f"%{query}%"
    rows = db.execute(
        "SELECT * FROM patients WHERE full_name ILIKE %s OR phone ILIKE %s ORDER BY full_name LIMIT 10",
        (like, like),
    ).fetchall()
    if not rows:
        send_message(chat_id, "Никого не нашёл.")
        return
    for p in rows:
        text = p["full_name"] + (f"\n{p['phone']}" if p["phone"] else "")
        buttons = [
            [{"text": "🔗 Открыть карту", "url": card_link(p["access_token"])}],
            [{"text": "➕ Добавить визит", "callback_data": f"visit:{p['id']}"}],
        ]
        send_message(chat_id, text, buttons)


def finish_visit(db, chat_id, session):
    payload = session["payload"]
    patient_id = payload.get("patient_id")
    if patient_id is None:
        send_message(chat_id, "Нет активного визита.")
        return
    row = db.execute(
        "INSERT INTO visits (patient_id, visit_date, service, created_at) VALUES (%s, %s, %s, %s) RETURNING id",
        (patient_id, date.today().isoformat(), payload.get("service", ""), datetime.now()),
    ).fetchone()
    visit_id = row["id"]
    for f in payload.get("files", []):
        db.execute(
            "INSERT INTO files (visit_id, blob_pathname, description, uploaded_at) VALUES (%s, %s, %s, %s)",
            (visit_id, f["pathname"], f["original"], datetime.now()),
        )
    patient = db.execute("SELECT * FROM patients WHERE id = %s", (patient_id,)).fetchone()
    clear_session(db, chat_id)
    send_message(chat_id, f"Визит сохранён.\nКарта: {card_link(patient['access_token'])}")


# ---------- state machine ----------

NEXT_PROMPT = {
    "np_name": ("np_dob", "Дата рождения (ДД.ММ.ГГГГ), или «-» чтобы пропустить:"),
    "np_dob": ("np_phone", "Телефон, или «-»:"),
}

def create_patient(db, payload):
    token_row = db.execute(
        "INSERT INTO patients (full_name, birth_date, phone, access_token, created_at) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING access_token",
        (
            payload.get("full_name", ""), payload.get("birth_date", ""), payload.get("phone", ""),
            dbmod.gen_token(), datetime.now(),
        ),
    ).fetchone()
    return token_row["access_token"]


def handle_text_step(db, chat_id, state, payload, text):
    if state == "np_name":
        payload["full_name"] = text.strip()
        next_state, prompt = NEXT_PROMPT[state]
        set_session(db, chat_id, next_state, payload)
        send_message(chat_id, prompt)
        return

    if state == "np_dob":
        raw = clean(text)
        dob = ""
        if raw:
            try:
                dob = datetime.strptime(raw, "%d.%m.%Y").date().isoformat()
            except ValueError:
                send_message(chat_id, "Не понял дату. Формат ДД.ММ.ГГГГ, например 05.03.1990. Или «-»:")
                return
        payload["birth_date"] = dob
        next_state, prompt = NEXT_PROMPT[state]
        set_session(db, chat_id, next_state, payload)
        send_message(chat_id, prompt)
        return

    if state == "np_phone":
        payload["phone"] = clean(text)
        token = create_patient(db, payload)
        clear_session(db, chat_id)
        name = payload.get("full_name", "")
        send_message(
            chat_id,
            f"Пациент «{name}» добавлен.\nСсылка на карту: {card_link(token)}\n\n"
            f"Чтобы добавить визит — напишите /find {name.split()[0] if name else ''} и нажмите «Добавить визит».",
        )
        return

    if state == "v_service":
        payload["service"] = clean(text)
        payload["files"] = payload.get("files", [])
        set_session(db, chat_id, "v_files", payload)
        send_message(
            chat_id,
            "Пришлите фото/снимки (можно несколько сообщений подряд). "
            "Когда закончите — отправьте /done. Если фото нет — сразу /done.",
        )
        return

    # idle or unrecognized state — treat plain text as a search
    do_find(db, chat_id, text)


def handle_file_step(db, chat_id, payload, message):
    file_id = None
    original_name = "photo.jpg"
    if message.get("photo"):
        photo = message["photo"][-1]
        file_id = photo["file_id"]
        original_name = f"photo_{photo['file_unique_id']}.jpg"
    elif message.get("document"):
        doc = message["document"]
        original_name = doc.get("file_name") or f"file_{doc['file_unique_id']}"
        ext = original_name.rsplit(".", 1)[-1].lower() if "." in original_name else ""
        if ext not in {"png", "jpg", "jpeg", "gif", "bmp", "webp", "pdf", "dcm"}:
            send_message(chat_id, "Этот тип файла не поддерживается, пропускаю.")
            return
        file_id = doc["file_id"]

    if file_id is None:
        return

    file_path = get_file_path(file_id)
    if not file_path:
        send_message(chat_id, "Не удалось получить файл, попробуйте ещё раз.")
        return
    data = download_file(file_path)
    pathname = upload_pending_file(payload["patient_id"], chat_id, original_name, data)
    payload.setdefault("files", []).append({"pathname": pathname, "original": original_name})
    set_session(db, chat_id, "v_files", payload)
    send_message(chat_id, f"Файл сохранён ({len(payload['files'])}). Ещё, или /done.")


def handle_message(db, message):
    chat_id = message["chat"]["id"]
    user_id = message.get("from", {}).get("id")
    if user_id not in ALLOWED_IDS:
        send_message(chat_id, "Доступ к этому боту ограничен.")
        return

    text = (message.get("text") or "").strip()
    session = get_session(db, chat_id)
    state = session["state"]
    payload = session["payload"]

    if text == "/start":
        clear_session(db, chat_id)
        send_message(chat_id, HELP_TEXT)
        return

    if text == "/cancel":
        clear_session(db, chat_id)
        send_message(chat_id, "Отменено.")
        return

    if text.startswith("/new_patient"):
        set_session(db, chat_id, "np_name", {})
        send_message(chat_id, "ФИО пациента:")
        return

    if text.startswith("/find"):
        do_find(db, chat_id, text[len("/find"):].strip())
        return

    if text.startswith("/done"):
        if state == "v_files":
            finish_visit(db, chat_id, session)
        else:
            send_message(chat_id, "Нет активного визита.")
        return

    if state == "v_files":
        if message.get("photo") or message.get("document"):
            handle_file_step(db, chat_id, payload, message)
        else:
            send_message(chat_id, "Пришлите фото/документ, или отправьте /done.")
        return

    if text:
        handle_text_step(db, chat_id, state, payload, text)


def handle_callback(db, callback_query):
    user_id = callback_query.get("from", {}).get("id")
    chat_id = callback_query["message"]["chat"]["id"]
    answer_callback_query(callback_query["id"])
    if user_id not in ALLOWED_IDS:
        return

    data = callback_query.get("data", "")
    if data.startswith("visit:"):
        patient_id = int(data.split(":", 1)[1])
        patient = db.execute("SELECT * FROM patients WHERE id = %s", (patient_id,)).fetchone()
        if patient is None:
            send_message(chat_id, "Пациент не найден.")
            return
        set_session(db, chat_id, "v_service", {"patient_id": patient_id, "files": []})
        send_message(chat_id, f"Новый визит — {patient['full_name']}.\nКакую услугу оказали?")


def handle_update(update, db):
    if "callback_query" in update:
        handle_callback(db, update["callback_query"])
    elif "message" in update:
        handle_message(db, update["message"])
