import os
import re
import hashlib
import secrets
import sqlite3
import json
import base64
import asyncio
from contextlib import asynccontextmanager
from typing import Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

# ==========================================================
#                    КОНФИГУРАЦИЯ ПУТЕЙ
# На Render диск монтируется в /data —
# там данные сохраняются между перезапусками.
# Локально (при разработке) всё лежит рядом с main.py.
# ==========================================================
IS_RENDER = os.path.isdir("/data")
DATA_DIR = "/data" if IS_RENDER else "."
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
STATIC_DIR = "static"
DB_FILE = os.path.join(DATA_DIR, "chat.db")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

# ==========================================================
#                    ПРИЛОЖЕНИЕ FASTAPI
# ==========================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # VAPID-ключи генерируются в памяти при импорте модуля (см. секцию WEB PUSH),
    # запись на диск не требуется — на Render файловая система только для чтения.
    yield

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

USERNAME_RE = re.compile(r'^[A-Za-zА-Яа-яЁё0-9]{3,20}$')
MAX_UPLOAD_SIZE = 25 * 1024 * 1024  # 25 МБ
ALLOWED_UPLOAD_EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.gif', '.webp',
    '.mp3', '.wav', '.ogg', '.webm', '.m4a',
    '.pdf', '.txt', '.zip', '.mp4', '.mov'
}
MESSAGES_PAGE_SIZE = 50

# ==========================================================
#          WEB PUSH (уведомления) — ключи в памяти
# (без записи на диск: на Render файловая система только для чтения)
# ==========================================================
from pywebpush import webpush, WebPushException
from py_vapid import Vapid
from cryptography.hazmat.primitives.serialization import (
    Encoding, PublicFormat, PrivateFormat, NoEncryption,
)

_VAPID = Vapid()
_VAPID.generate_keys()
_VAPID_PRIVATE_PEM = _VAPID.private_key.private_bytes(
    Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
).decode()
_VAPID_PUBLIC_B64 = base64.urlsafe_b64encode(
    _VAPID.public_key.public_bytes(Encoding.X9_62, PublicFormat.UncompressedPoint)
).decode().rstrip("=")
VAPID_CLAIMS = {"sub": "mailto:admin@nexus-messenger.local"}


def get_vapid_public_b64() -> str:
    """Публичный VAPID-ключ для браузера (base64url)."""
    return _VAPID_PUBLIC_B64


def send_push_to_user(username: str, title: str, body: str) -> None:
    """Шлёт Web Push всем подпискам пользователя; мёртвые подписки чистит."""
    conn = get_db()
    try:
        subs = conn.execute(
            "SELECT endpoint, p256dh, auth FROM push_subs WHERE username = ?",
            (username,),
        ).fetchall()
    finally:
        conn.close()
    if not subs:
        return
    payload = json.dumps({"title": title, "body": body})
    dead = []
    for endpoint, p256dh, auth in subs:
        try:
            webpush(
                subscription_info={
                    "endpoint": endpoint,
                    "keys": {"p256dh": p256dh, "auth": auth},
                },
                data=payload,
                vapid_private_key=_VAPID_PRIVATE_PEM,
                vapid_claims=VAPID_CLAIMS,
            )
        except WebPushException as e:
            print(f"[PUSH] ошибка для {username}: {e}")
            resp = getattr(e, "response", None)
            if resp is not None and getattr(resp, "status_code", 0) in (404, 410):
                dead.append(endpoint)
        except Exception as e:
            print(f"[PUSH] неожиданная ошибка: {e}")
    if dead:
        conn = get_db()
        try:
            conn.executemany(
                "DELETE FROM push_subs WHERE endpoint = ?", [(e,) for e in dead]
            )
            conn.commit()
        finally:
            conn.close()

# ==========================================================
#                        БАЗА ДАННЫХ
# ==========================================================

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")   # БАГ-ФИХ: WAL режим для параллельных запросов
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def safe_alter(cursor: sqlite3.Cursor, sql: str) -> None:
    """Выполняет ALTER TABLE, игнорируя ошибку если колонка уже существует."""
    try:
        cursor.execute(sql)
    except sqlite3.OperationalError:
        pass


def init_db() -> None:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            username      TEXT PRIMARY KEY,
            password_hash TEXT,
            avatar_url    TEXT DEFAULT '/uploads/default.png',
            bio           TEXT DEFAULT ''
        )
    ''')
    safe_alter(cursor, "ALTER TABLE users ADD COLUMN bio TEXT DEFAULT ''")
    safe_alter(cursor, "ALTER TABLE users ADD COLUMN password_hash TEXT")
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            token      TEXT PRIMARY KEY,
            username   TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id   TEXT    NOT NULL,
            sender    TEXT    NOT NULL,
            content   TEXT    NOT NULL,
            msg_type  TEXT    DEFAULT 'text',
            status    TEXT    DEFAULT 'sent',
            reply_to  INTEGER,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    safe_alter(cursor, "ALTER TABLE messages ADD COLUMN reply_to INTEGER")
    # Индексы для ускорения выборки истории
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat_id ON messages(chat_id, id)")
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS reactions (
            message_id INTEGER NOT NULL,
            username   TEXT    NOT NULL,
            emoji      TEXT    NOT NULL,
            PRIMARY KEY (message_id, username)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS groups (
            group_id   TEXT PRIMARY KEY,
            title      TEXT NOT NULL,
            owner      TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS group_members (
            group_id TEXT NOT NULL,
            username TEXT NOT NULL,
            PRIMARY KEY (group_id, username)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS push_subs (
            endpoint TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            p256dh   TEXT NOT NULL,
            auth     TEXT NOT NULL
        )
    ''')
    conn.commit()
    conn.close()


def get_chat_id(user1: str, user2: str) -> str:
    return "_".join(sorted([user1, user2]))

# ==========================================================
#                    ПАРОЛИ И АВТОРИЗАЦИЯ
# ==========================================================

def hash_password(password: str) -> str:
    salt = os.urandom(16).hex()
    dk = hashlib.pbkdf2_hmac(
        'sha256', password.encode('utf-8'), bytes.fromhex(salt), 100_000
    )
    return f"{salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, hash_hex = stored.split('$')
        dk = hashlib.pbkdf2_hmac(
            'sha256', password.encode('utf-8'), bytes.fromhex(salt), 100_000
        )
        return secrets.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def create_session(username: str) -> str:
    token = secrets.token_hex(24)
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO sessions (token, username) VALUES (?, ?)", (token, username)
        )
        conn.commit()
    finally:
        conn.close()
    return token


def get_username_by_token(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT username FROM sessions WHERE token = ?", (token,)
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def require_auth(token: Optional[str]) -> str:
    username = get_username_by_token(token)
    if not username:
        raise HTTPException(status_code=401, detail="Не авторизован. Войдите заново.")
    return username


def is_group_member(group_id: str, username: str) -> bool:
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT 1 FROM group_members WHERE group_id = ? AND username = ?",
            (group_id, username),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def fetch_reactions_for_messages(
    cursor: sqlite3.Cursor, message_ids: List[int]
) -> Dict[int, List[dict]]:
    if not message_ids:
        return {}
    placeholders = ",".join("?" * len(message_ids))
    cursor.execute(
        f"SELECT message_id, username, emoji FROM reactions WHERE message_id IN ({placeholders})",
        message_ids,
    )
    result: Dict[int, List[dict]] = {}
    for message_id, username, emoji in cursor.fetchall():
        result.setdefault(message_id, []).append({"username": username, "emoji": emoji})
    return result


def rows_to_messages(cursor: sqlite3.Cursor, rows) -> List[dict]:
    ids = [r[0] for r in rows]
    reactions_map = fetch_reactions_for_messages(cursor, ids)
    result = []
    for r in rows:
        (msg_id, chat_id, sender, content, msg_type, status, reply_to,
         timestamp, reply_sender, reply_content, reply_msg_type) = r
        reply_preview = None
        if reply_to and reply_sender is not None:
            reply_preview = {
                "id": reply_to, "sender": reply_sender,
                "content": reply_content, "msg_type": reply_msg_type,
            }
        result.append({
            "id": msg_id, "chat_id": chat_id, "sender": sender,
            "content": content, "msg_type": msg_type, "status": status,
            "timestamp": timestamp, "reply_to": reply_preview,
            "reactions": reactions_map.get(msg_id, []),
        })
    return result


def get_group_member_usernames(group_id: str) -> List[str]:
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT username FROM group_members WHERE group_id = ?", (group_id,)
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]

# ==========================================================
#             МЕНЕДЖЕР WEBSOCKET-СОЕДИНЕНИЙ
# ==========================================================

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, username: str, websocket: WebSocket) -> None:
        await websocket.accept()
        print(f"[WS] + connect: {username}", flush=True)
        self.active_connections[username] = websocket

    def disconnect(self, username: str) -> None:
        print(f"[WS] - disconnect: {username}", flush=True)
        self.active_connections.pop(username, None)

    def is_online(self, username: str) -> bool:
        return username in self.active_connections

    async def send_to_user(self, message: dict, target_user: str) -> bool:
        ws = self.active_connections.get(target_user)
        if ws is None:
            return False
        try:
            await ws.send_json(message)
            return True
        except Exception as e:
            print(f"[WS] Ошибка отправки '{target_user}': {e}")
            # БАГ-ФИХ: удаляем мёртвое соединение из словаря
            self.active_connections.pop(target_user, None)
            return False


manager = ConnectionManager()

# ==========================================================
#                    ОБЫЧНЫЕ ЭНДПОИНТЫ
# ==========================================================

@app.get("/")
def get_index():
    # ФИКС: запрещаем браузеру кэшировать страницу —
    # всегда отдаём свежий index.html
    return FileResponse(
        "index.html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


@app.get("/manifest.json")
def get_manifest():
    return FileResponse("manifest.json", media_type="application/manifest+json")


@app.get("/sw.js")
def get_sw():
    # Сервис-воркер тоже всегда должен быть свежим
    return FileResponse(
        "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


# Иконки PWA: файлы лежат в корне репозитория — отдаём их по корневым адресам
@app.get("/icon-192.png")
def get_icon192():
    return FileResponse("icon-192.png", media_type="image/png")


@app.get("/icon-512.png")
def get_icon512():
    return FileResponse("icon-512.png", media_type="image/png")


@app.get("/apple-touch-icon.png")
def get_apple_icon():
    return FileResponse("apple-touch-icon.png", media_type="image/png")


@app.get("/search")
def search_users(q: str, token: str):
    """Честный поиск: только реально существующие аккаунты,
    регистронезависимо (включая кириллицу), топ-10."""
    require_auth(token)
    q = q.strip()
    if not q:
        return []
    conn = get_db()
    try:
        rows = conn.execute("SELECT username, avatar_url FROM users").fetchall()
    finally:
        conn.close()
    ql = q.lower()
    found = [
        {"username": r[0], "avatar_url": r[1] or "/uploads/default.png"}
        for r in rows if ql in r[0].lower()
    ]
    return found[:10]


@app.get("/push/publickey")
def push_publickey():
    return {"publicKey": get_vapid_public_b64()}


@app.post("/push/subscribe")
async def push_subscribe(token: str = Form(...), subscription: str = Form(...)):
    username = require_auth(token)
    try:
        sub = json.loads(subscription)
        endpoint = sub["endpoint"]
        keys = sub.get("keys", {})
        p256dh = keys.get("p256dh", "")
        auth = keys.get("auth", "")
    except Exception:
        raise HTTPException(status_code=400, detail="Некорректная подписка")
    conn = get_db()
    try:
        conn.execute("DELETE FROM push_subs WHERE endpoint = ?", (endpoint,))
        conn.execute(
            "INSERT INTO push_subs (username, endpoint, p256dh, auth) VALUES (?, ?, ?, ?)",
            (username, endpoint, p256dh, auth),
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok"}


@app.post("/register")
async def register(
    username: str = Form(...),
    password: str = Form(...),
    avatar: UploadFile = File(None),
):
    if not USERNAME_RE.match(username):
        raise HTTPException(
            status_code=400,
            detail="Никнейм должен быть 3-20 символов: только буквы (рус/лат) и цифры",
        )
    if len(password) < 4:
        raise HTTPException(status_code=400, detail="Пароль должен быть не короче 4 символов")
    conn = get_db()
    try:
        if conn.execute(
            "SELECT 1 FROM users WHERE username = ?", (username,)
        ).fetchone():
            raise HTTPException(status_code=400, detail="Этот никнейм уже занят")
        avatar_url = "/uploads/default.png"
        if avatar and avatar.filename:
            # БАГ-ФИХ: безопасное имя файла + проверка расширения
            ext = os.path.splitext(avatar.filename)[1].lower()
            if ext not in ALLOWED_UPLOAD_EXTENSIONS:
                raise HTTPException(status_code=400, detail="Недопустимый тип файла для аватара")
            safe_name = f"{secrets.token_hex(8)}{ext}"
            file_path = os.path.join(UPLOAD_DIR, safe_name)
            content = await avatar.read()
            if len(content) > MAX_UPLOAD_SIZE:
                raise HTTPException(status_code=400, detail="Файл слишком большой")
            with open(file_path, "wb") as f:
                f.write(content)
            avatar_url = f"/uploads/{safe_name}"
        conn.execute(
            "INSERT INTO users (username, password_hash, avatar_url, bio) VALUES (?, ?, ?, ?)",
            (username, hash_password(password), avatar_url, ""),
        )
        conn.commit()
    finally:
        conn.close()
    token = create_session(username)
    return {"status": "ok", "username": username, "avatar_url": avatar_url, "bio": "", "token": token}


@app.post("/login")
async def login(username: str = Form(...), password: str = Form(...)):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT password_hash, avatar_url, bio FROM users WHERE username = ?", (username,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Пользователь не найден")
        if not row[0]:
            # Старый аккаунт без пароля — задаём пароль сейчас
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE username = ?",
                (hash_password(password), username),
            )
            conn.commit()
        elif not verify_password(password, row[0]):
            raise HTTPException(status_code=401, detail="Неверный пароль")
    finally:
        conn.close()
    token = create_session(username)
    return {
        "status": "ok",
        "username": username,
        "avatar_url": row[1] or "/uploads/default.png",
        "bio": row[2] or "",
        "token": token,
    }


@app.post("/logout")
async def logout(token: str = Form(...)):
    conn = get_db()
    try:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok"}


@app.get("/profile/{username}")
def get_profile(username: str):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT avatar_url, bio FROM users WHERE username = ?", (username,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {"username": username, "avatar_url": "/uploads/default.png", "bio": ""}
    return {"username": username, "avatar_url": row[0] or "/uploads/default.png", "bio": row[1] or ""}


@app.post("/profile/update")
async def update_profile(
    token: str = Form(...),
    bio: str = Form(""),
    avatar: UploadFile = File(None),
):
    username = require_auth(token)
    # БАГ-ФИХ: обрезаем bio до 150 символов на сервере тоже
    bio = bio[:150]
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT avatar_url FROM users WHERE username = ?", (username,)
        ).fetchone()
        avatar_url = row[0] if row else "/uploads/default.png"
        if avatar and avatar.filename:
            ext = os.path.splitext(avatar.filename)[1].lower()
            if ext not in ALLOWED_UPLOAD_EXTENSIONS:
                raise HTTPException(status_code=400, detail="Недопустимый тип файла для аватара")
            safe_name = f"{secrets.token_hex(8)}{ext}"
            file_path = os.path.join(UPLOAD_DIR, safe_name)
            content = await avatar.read()
            if len(content) > MAX_UPLOAD_SIZE:
                raise HTTPException(status_code=400, detail="Файл слишком большой")
            with open(file_path, "wb") as f:
                f.write(content)
            avatar_url = f"/uploads/{safe_name}"
        conn.execute(
            "UPDATE users SET avatar_url = ?, bio = ? WHERE username = ?",
            (avatar_url, bio, username),
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok", "username": username, "avatar_url": avatar_url, "bio": bio}


@app.post("/upload")
async def upload_file(token: str = Form(...), file: UploadFile = File(...)):
    require_auth(token)
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Этот тип файла не поддерживается")
    contents = await file.read()
    if len(contents) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=400, detail="Файл слишком большой (максимум 25 МБ)")
    safe_name = f"{secrets.token_hex(8)}{ext}"
    file_path = os.path.join(UPLOAD_DIR, safe_name)
    with open(file_path, "wb") as f:
        f.write(contents)
    return {"file_url": f"/uploads/{safe_name}"}


@app.get("/users")
def get_online_users():
    return list(manager.active_connections.keys())

# ==========================================================
#                    РАБОТА С ГРУППАМИ
# ==========================================================

@app.post("/groups/create")
async def create_group(
    token: str = Form(...),
    title: str = Form(...),
    members: str = Form(...),
):
    owner = require_auth(token)
    member_list = [m.strip() for m in members.split(",") if m.strip()]
    if owner not in member_list:
        member_list.append(owner)
    # БАГ-ФИХ: проверяем что участники существуют
    conn = get_db()
    try:
        for m in member_list:
            if not conn.execute("SELECT 1 FROM users WHERE username = ?", (m,)).fetchone():
                raise HTTPException(status_code=404, detail=f"Пользователь @{m} не найден")
        group_id = f"group_{os.urandom(6).hex()}"
        conn.execute(
            "INSERT INTO groups (group_id, title, owner) VALUES (?, ?, ?)",
            (group_id, title, owner),
        )
        for m in member_list:
            conn.execute(
                "INSERT INTO group_members (group_id, username) VALUES (?, ?)",
                (group_id, m),
            )
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok", "group_id": group_id, "title": title, "members": member_list}


@app.get("/groups/my/{username}")
def get_user_groups(username: str, token: str):
    requester = require_auth(token)
    if requester != username:
        raise HTTPException(status_code=403, detail="Нет доступа")
    conn = get_db()
    try:
        rows = conn.execute(
            '''SELECT g.group_id, g.title, g.owner
               FROM groups g
               JOIN group_members gm ON g.group_id = gm.group_id
               WHERE gm.username = ?''',
            (username,),
        ).fetchall()
    finally:
        conn.close()
    return [{"group_id": r[0], "title": r[1], "owner": r[2]} for r in rows]


@app.get("/groups/{group_id}/members")
def get_group_members(group_id: str, token: str):
    requester = require_auth(token)
    if not is_group_member(group_id, requester):
        raise HTTPException(status_code=403, detail="Нет доступа к этой группе")
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT owner FROM groups WHERE group_id = ?", (group_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Группа не найдена")
        owner = row[0]
        members = [
            r[0] for r in conn.execute(
                "SELECT username FROM group_members WHERE group_id = ?", (group_id,)
            ).fetchall()
        ]
    finally:
        conn.close()
    return {"group_id": group_id, "owner": owner, "members": members}


@app.post("/groups/{group_id}/members/add")
async def add_group_member(
    group_id: str,
    token: str = Form(...),
    username: str = Form(...),
):
    requester = require_auth(token)
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT owner FROM groups WHERE group_id = ?", (group_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Группа не найдена")
        if row[0] != requester:
            raise HTTPException(status_code=403, detail="Только создатель может добавлять участников")
        # БАГ-ФИХ: проверяем что пользователь существует
        if not conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            raise HTTPException(status_code=404, detail=f"Пользователь @{username} не найден")
        conn.execute(
            "INSERT OR IGNORE INTO group_members (group_id, username) VALUES (?, ?)",
            (group_id, username),
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok"}


@app.post("/groups/{group_id}/members/remove")
async def remove_group_member(
    group_id: str,
    token: str = Form(...),
    username: str = Form(...),
):
    requester = require_auth(token)
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT owner FROM groups WHERE group_id = ?", (group_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Группа не найдена")
        if row[0] != requester:
            raise HTTPException(status_code=403, detail="Только создатель может удалять участников")
        if username == row[0]:
            raise HTTPException(
                status_code=400,
                detail="Создатель не может удалить сам себя, используйте выход из группы",
            )
        conn.execute(
            "DELETE FROM group_members WHERE group_id = ? AND username = ?",
            (group_id, username),
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok"}


@app.post("/groups/{group_id}/leave")
async def leave_group(group_id: str, token: str = Form(...)):
    username = require_auth(token)
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT owner FROM groups WHERE group_id = ?", (group_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Группа не найдена")
        conn.execute(
            "DELETE FROM group_members WHERE group_id = ? AND username = ?",
            (group_id, username),
        )
        remaining = [
            r[0] for r in conn.execute(
                "SELECT username FROM group_members WHERE group_id = ?", (group_id,)
            ).fetchall()
        ]
        if not remaining:
            conn.execute("DELETE FROM groups WHERE group_id = ?", (group_id,))
            conn.execute("DELETE FROM messages WHERE chat_id = ?", (group_id,))
        elif row[0] == username:
            conn.execute(
                "UPDATE groups SET owner = ? WHERE group_id = ?", (remaining[0], group_id)
            )
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok"}


@app.delete("/groups/{group_id}")
def delete_group(group_id: str, token: str):
    owner = require_auth(token)
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT owner FROM groups WHERE group_id = ?", (group_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Группа не найдена")
        if row[0] != owner:
            raise HTTPException(status_code=403, detail="Только создатель может удалить группу")
        conn.execute("DELETE FROM groups WHERE group_id = ?", (group_id,))
        conn.execute("DELETE FROM group_members WHERE group_id = ?", (group_id,))
        conn.execute("DELETE FROM messages WHERE chat_id = ?", (group_id,))
        conn.commit()
    finally:
        conn.close()
    return {"status": "deleted"}

# ==========================================================
#                СООБЩЕНИЯ И ИСТОРИЯ ЧАТОВ
# ==========================================================

MSG_SELECT_SQL = '''
SELECT m.id, m.chat_id, m.sender, m.content, m.msg_type, m.status, m.reply_to, m.timestamp,
       r.sender, r.content, r.msg_type
FROM messages m
LEFT JOIN messages r ON m.reply_to = r.id
WHERE m.chat_id = ? {extra}
ORDER BY m.id DESC LIMIT ?
'''


@app.get("/messages/{user1}/{user2}")
def get_messages(
    user1: str, user2: str, token: str, before_id: Optional[int] = None
):
    requester = require_auth(token)
    if requester not in (user1, user2):
        raise HTTPException(status_code=403, detail="Нет доступа к этой переписке")
    chat_id = get_chat_id(user1, user2)
    conn = get_db()
    try:
        sql = MSG_SELECT_SQL.format(extra="AND m.id < ?" if before_id else "")
        params = (chat_id, before_id, MESSAGES_PAGE_SIZE) if before_id else (chat_id, MESSAGES_PAGE_SIZE)
        rows = list(reversed(conn.execute(sql, params).fetchall()))
        result = rows_to_messages(conn.cursor(), rows)
    finally:
        conn.close()
    return result


@app.get("/group-messages/{group_id}")
def get_group_messages(
    group_id: str, token: str, before_id: Optional[int] = None
):
    requester = require_auth(token)
    if not is_group_member(group_id, requester):
        raise HTTPException(status_code=403, detail="Нет доступа к этой группе")
    conn = get_db()
    try:
        sql = MSG_SELECT_SQL.format(extra="AND m.id < ?" if before_id else "")
        params = (group_id, before_id, MESSAGES_PAGE_SIZE) if before_id else (group_id, MESSAGES_PAGE_SIZE)
        rows = list(reversed(conn.execute(sql, params).fetchall()))
        result = rows_to_messages(conn.cursor(), rows)
    finally:
        conn.close()
    return result


@app.delete("/messages/{user1}/{user2}")
def delete_chat(user1: str, user2: str, token: str):
    requester = require_auth(token)
    if requester not in (user1, user2):
        raise HTTPException(status_code=403, detail="Нет доступа к этой переписке")
    chat_id = get_chat_id(user1, user2)
    conn = get_db()
    try:
        conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        conn.commit()
    finally:
        conn.close()
    return {"status": "deleted"}

# ==========================================================
#                    WEBSOCKET: ГЛАВНАЯ ЛОГИКА
# ==========================================================

@app.websocket("/ws/{username}")
async def websocket_endpoint(
    websocket: WebSocket,
    username: str,
    token: Optional[str] = None,
):
    verified_username = get_username_by_token(token)
    if not verified_username or verified_username != username:
        await websocket.close(code=4001)
        return

    await manager.connect(username, websocket)
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type", "message")

            # ------------------------------------------------
            # 1. НОВОЕ СООБЩЕНИЕ
            # ------------------------------------------------
            if msg_type == "message":
                is_group = bool(data.get("is_group", False))
                target = data.get("target")
                content = data.get("content", "").strip()
                content_type = data.get("msg_type", "text")
                duration = data.get("duration")
                reply_to_id = data.get("reply_to")

                if not target or not content:
                    continue
                # БАГ-ФИХ: ограничение длины сообщения на сервере
                if len(content) > 2000:
                    continue
                if is_group and not is_group_member(target, username):
                    continue

                chat_id = target if is_group else get_chat_id(username, target)
                initial_status = "delivered" if (not is_group and manager.is_online(target)) else "sent"
                conn = get_db()
                try:
                    cursor = conn.cursor()
                    cursor.execute(
                        "INSERT INTO messages (chat_id, sender, content, msg_type, status, reply_to) VALUES (?, ?, ?, ?, ?, ?)",
                        (chat_id, username, content, content_type, initial_status, reply_to_id),
                    )
                    conn.commit()
                    msg_id = cursor.lastrowid
                    reply_preview = None
                    if reply_to_id:
                        rr = cursor.execute(
                            "SELECT sender, content, msg_type FROM messages WHERE id = ?",
                            (reply_to_id,),
                        ).fetchone()
                        if rr:
                            reply_preview = {
                                "id": reply_to_id, "sender": rr[0],
                                "content": rr[1], "msg_type": rr[2],
                            }
                finally:
                    conn.close()

                msg_payload = {
                    "type": "message", "id": msg_id, "chat_id": chat_id,
                    "target": target, "sender": username, "content": content,
                    "msg_type": content_type, "status": initial_status,
                    "is_group": is_group, "duration": duration,
                    "reply_to": reply_preview, "reactions": [],
                }
                if is_group:
                    for member_user in get_group_member_usernames(target):
                        if member_user == username:
                            continue
                        ok = await manager.send_to_user(msg_payload, member_user)
                        if not ok:
                            await asyncio.to_thread(
                                send_push_to_user, member_user,
                                f"👥 {username}", content[:100],
                            )
                else:
                    await manager.send_to_user(msg_payload, username)
                    if target != username:
                        ok = await manager.send_to_user(msg_payload, target)
                        if not ok:
                            await asyncio.to_thread(
                                send_push_to_user, target,
                                f"💬 {username}", content[:100],
                            )

            # ------------------------------------------------
            # 2. ОТМЕТКА "ПРОЧИТАНО"
            # ------------------------------------------------
            elif msg_type == "read_receipt":
                chat_id = data.get("chat_id")
                sender_to_notify = data.get("sender")
                if not chat_id or not sender_to_notify:
                    continue
                conn = get_db()
                try:
                    conn.execute(
                        "UPDATE messages SET status = 'read' WHERE chat_id = ? AND sender = ? AND status != 'read'",
                        (chat_id, sender_to_notify),
                    )
                    conn.commit()
                finally:
                    conn.close()
                await manager.send_to_user(
                    {"type": "status_update", "chat_id": chat_id, "status": "read"},
                    sender_to_notify,
                )

            # ------------------------------------------------
            # 3. РЕАКЦИЯ НА СООБЩЕНИЕ
            # ------------------------------------------------
            elif msg_type == "reaction":
                message_id = data.get("message_id")
                target = data.get("target")
                is_group = bool(data.get("is_group", False))
                emoji = data.get("emoji")
                if not message_id or not target or not emoji:
                    continue
                if is_group and not is_group_member(target, username):
                    continue
                conn = get_db()
                try:
                    existing = conn.execute(
                        "SELECT emoji FROM reactions WHERE message_id = ? AND username = ?",
                        (message_id, username),
                    ).fetchone()
                    if existing and existing[0] == emoji:
                        conn.execute(
                            "DELETE FROM reactions WHERE message_id = ? AND username = ?",
                            (message_id, username),
                        )
                    else:
                        conn.execute(
                            "REPLACE INTO reactions (message_id, username, emoji) VALUES (?, ?, ?)",
                            (message_id, username, emoji),
                        )
                    conn.commit()
                    reactions = [
                        {"username": r[0], "emoji": r[1]}
                        for r in conn.execute(
                            "SELECT username, emoji FROM reactions WHERE message_id = ?",
                            (message_id,),
                        ).fetchall()
                    ]
                finally:
                    conn.close()
                update_payload = {
                    "type": "reaction_update",
                    "message_id": message_id,
                    "reactions": reactions,
                }
                if is_group:
                    for member_user in get_group_member_usernames(target):
                        await manager.send_to_user(update_payload, member_user)
                else:
                    await manager.send_to_user(update_payload, username)
                    if target != username:
                        await manager.send_to_user(update_payload, target)

            # ------------------------------------------------
            # 4. PING / PONG — держим соединение живым
            # ------------------------------------------------
            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})
                continue

            # ------------------------------------------------
            # 5. ИНДИКАТОР "ПЕЧАТАЕТ..."
            # ------------------------------------------------
            elif msg_type == "typing":
                is_group = bool(data.get("is_group", False))
                target = data.get("target")
                is_typing = bool(data.get("is_typing", False))
                if not target:
                    continue
                if is_group and not is_group_member(target, username):
                    continue
                typing_payload = {
                    "type": "typing", "sender": username,
                    "target": target, "is_group": is_group, "is_typing": is_typing,
                }
                if is_group:
                    for member_user in get_group_member_usernames(target):
                        if member_user != username:
                            await manager.send_to_user(typing_payload, member_user)
                else:
                    if target != username:
                        await manager.send_to_user(typing_payload, target)

    except WebSocketDisconnect:
        manager.disconnect(username)
    except Exception as e:
        print(f"[WS] Неожиданная ошибка у '{username}': {e}")
        manager.disconnect(username)