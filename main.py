import os
import re
import hashlib
import secrets
import sqlite3
import json
import base64
import asyncio
import urllib.request
import urllib.error
from collections import deque
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from typing import Dict, List, Optional, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# ==========================================================
#                    КОНФИГУРАЦИЯ
# ==========================================================
IS_RENDER = os.path.isdir("/data")
DATA_DIR = "/data" if IS_RENDER else "."
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
STATIC_DIR = "static"
DB_FILE = os.path.join(DATA_DIR, "chat.db")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "")

# OneSignal (нативные пуши в приложение median.co)
ONE_SIGNAL_APP_ID = os.environ.get("ONE_SIGNAL_APP_ID", "")
ONE_SIGNAL_REST_KEY = os.environ.get("ONE_SIGNAL_REST_KEY", "")
ONESIGNAL_LAST_ERROR = ""

try:
    APP_VERSION = os.environ.get("RENDER_GIT_COMMIT", "") or str(int(os.path.getmtime("index.html")))
except Exception:
    APP_VERSION = "dev"

MSK = timezone(timedelta(hours=3))
CONNECT_LOG = deque(maxlen=100)

USERNAME_RE = re.compile(r'^[A-Za-zА-Яа-яЁё0-9]{3,20}$')
MAX_UPLOAD_SIZE = 25 * 1024 * 1024
ALLOWED_UPLOAD_EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.gif', '.webp',
    '.mp3', '.wav', '.ogg', '.webm', '.m4a',
    '.pdf', '.txt', '.zip', '.mp4', '.mov'
}
MESSAGES_PAGE_SIZE = 50

# ==========================================================
#          WEB PUSH (уведомления браузера / PWA / TWA)
# ==========================================================
from pywebpush import webpush, WebPushException
from py_vapid import Vapid
from cryptography.hazmat.primitives.serialization import (
    Encoding, PublicFormat, PrivateFormat, NoEncryption,
)

VAPID_CLAIMS = {"sub": "mailto:admin@nexus-messenger.local"}

# Ключ VAPID создаётся ОДИН раз и хранится в базе, чтобы не гулять
# между перезапусками/просыпаниями сервера (иначе пуши тихо отваливаются).
_VAPID_MEM = None  # (private_pem, public_b64)


def _load_or_make_vapid(conn: sqlite3.Connection):
    row = conn.execute("SELECT private_pem, public_b64 FROM vapid WHERE id = 1").fetchone()
    if row:
        return row[0], row[1]
    v = Vapid()
    v.generate_keys()
    pem = v.private_key.private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
    ).decode()
    raw = v.public_key.public_bytes(
        Encoding.DER, PublicFormat.SubjectPublicKeyInfo
    )[-65:]
    pub = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    conn.execute(
        "INSERT OR REPLACE INTO vapid (id, private_pem, public_b64) VALUES (1, ?, ?)",
        (pem, pub),
    )
    conn.commit()
    return pem, pub


def get_vapid_pair():
    global _VAPID_MEM
    if _VAPID_MEM:
        return _VAPID_MEM
    conn = get_db()
    try:
        _VAPID_MEM = _load_or_make_vapid(conn)
    finally:
        conn.close()
    return _VAPID_MEM


def get_vapid_public_b64() -> str:
    return get_vapid_pair()[1]


def send_push_to_user(username: str, title: str, body: str) -> None:
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
    private_pem, _ = get_vapid_pair()
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
                vapid_private_key=private_pem,
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
#          ONESIGNAL (нативные пуши приложения)
# ==========================================================

def _onesignal_post(payload: dict) -> bool:
    global ONESIGNAL_LAST_ERROR
    if not ONE_SIGNAL_APP_ID or not ONE_SIGNAL_REST_KEY:
        ONESIGNAL_LAST_ERROR = "нет переменных окружения"
        return False
    payload = dict(payload)
    payload.setdefault("app_id", ONE_SIGNAL_APP_ID)
    data = json.dumps(payload).encode()
    key = ONE_SIGNAL_REST_KEY.strip()
    if key.startswith("os_v2_app_") or key.startswith("os_v2_"):
        auth_variants = [f"Bearer {key}", f"Key={key}"]
    else:
        auth_variants = [f"Key={key}", f"Bearer {key}"]
    for auth in auth_variants:
        req = urllib.request.Request(
            "https://api.onesignal.com/notifications",
            data=data,
            headers={"Content-Type": "application/json", "Authorization": auth},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode()[:200]
                print(f"[ONESIGNAL] ok status={resp.status} body={body}", flush=True)
                ONESIGNAL_LAST_ERROR = ""
                return True
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode()[:200]
            except Exception:
                pass
            print(f"[ONESIGNAL] HTTP {e.code} auth={auth.split(' ')[0]} body={err_body}", flush=True)
            ONESIGNAL_LAST_ERROR = f"HTTP {e.code}: {err_body}"
            continue
        except Exception as e:
            print(f"[ONESIGNAL] ошибка: {e}", flush=True)
            ONESIGNAL_LAST_ERROR = str(e)
            continue
    return False


def send_onesignal_push(username: str, title: str, body: str) -> bool:
    """Пуш конкретному пользователю (привязка по external user id = ник)."""
    return _onesignal_post({
        "include_external_user_ids": [username],
        "headings": {"en": title},
        "contents": {"en": body},
    })


def send_onesignal_broadcast(title: str, body: str) -> bool:
    """Пуш всем устройствам приложения сразу."""
    return _onesignal_post({
        "included_segments": ["All"],
        "headings": {"en": title},
        "contents": {"en": body},
    })


def notify_offline(username: str, title: str, body: str) -> None:
    send_push_to_user(username, title, body)
    send_onesignal_push(username, title, body)


# ==========================================================
#                        БАЗА ДАННЫХ
# ==========================================================

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def safe_alter(cursor: sqlite3.Cursor, sql: str) -> None:
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
    safe_alter(cursor, "ALTER TABLE users ADD COLUMN status_text TEXT DEFAULT ''")
    safe_alter(cursor, "ALTER TABLE users ADD COLUMN created_at DATETIME DEFAULT CURRENT_TIMESTAMP")
    safe_alter(cursor, "ALTER TABLE users ADD COLUMN last_seen DATETIME")
    safe_alter(cursor, "ALTER TABLE users ADD COLUMN banned INTEGER DEFAULT 0")
    safe_alter(cursor, "ALTER TABLE users ADD COLUMN theme TEXT DEFAULT 'dark'")
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
    safe_alter(cursor, "ALTER TABLE messages ADD COLUMN edited_at DATETIME")
    safe_alter(cursor, "ALTER TABLE messages ADD COLUMN duration INTEGER")
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
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS blocks (
            blocker TEXT NOT NULL,
            blocked TEXT NOT NULL,
            PRIMARY KEY (blocker, blocked)
        )
    ''')
    # Постоянный VAPID-ключ (создаётся один раз, переживает рестарты сервера)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS vapid (
            id          INTEGER PRIMARY KEY CHECK (id = 1),
            private_pem TEXT NOT NULL,
            public_b64  TEXT NOT NULL
        )
    ''')
    conn.commit()
    conn.close()
    # Прогреваем ключ при старте, чтобы не создавать его в момент первого пуша
    get_vapid_pair()


def get_chat_id(user1: str, user2: str) -> str:
    return "_".join(sorted([user1, user2]))


def chat_recipients(chat_id: str) -> List[str]:
    if chat_id.startswith("group_"):
        return get_group_member_usernames(chat_id)
    parts = chat_id.split("_")
    return parts if len(parts) == 2 else []


def is_blocked(blocker: str, blocked: str) -> bool:
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT 1 FROM blocks WHERE blocker = ? AND blocked = ?",
            (blocker, blocked),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def is_admin(username: str) -> bool:
    return bool(ADMIN_USERNAME) and username == ADMIN_USERNAME


def is_banned(username: str) -> bool:
    conn = get_db()
    try:
        row = conn.execute("SELECT banned FROM users WHERE username = ?", (username,)).fetchone()
    finally:
        conn.close()
    return bool(row and row[0])


def touch_last_seen(username: str) -> None:
    conn = get_db()
    try:
        conn.execute("UPDATE users SET last_seen = CURRENT_TIMESTAMP WHERE username = ?", (username,))
        conn.commit()
    finally:
        conn.close()


def utc_str_to_msk(s: Optional[str]) -> str:
    if not s:
        return ""
    try:
        dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.astimezone(MSK).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return s


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
    if is_banned(username):
        raise HTTPException(status_code=403, detail="Вы забанены администратором")
    return username


def require_admin(token: Optional[str]) -> str:
    username = require_auth(token)
    if not is_admin(username):
        raise HTTPException(status_code=403, detail="Только для администратора")
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
         timestamp, edited_at, duration, reply_sender, reply_content, reply_msg_type) = r
        reply_preview = None
        if reply_to and reply_sender is not None:
            reply_preview = {
                "id": reply_to, "sender": reply_sender,
                "content": reply_content, "msg_type": reply_msg_type,
            }
        result.append({
            "id": msg_id, "chat_id": chat_id, "sender": sender,
            "content": content, "msg_type": msg_type, "status": status,
            "timestamp": timestamp, "edited": edited_at is not None,
            "duration": duration,
            "reply_to": reply_preview,
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
#             МЕНЕДЖЕР WEBSOCKET: МУЛЬТИ-СОЕДИНЕНИЯ
# ==========================================================

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, Set[WebSocket]] = {}

    async def connect(self, username: str, websocket: WebSocket) -> None:
        await websocket.accept()
        print(f"[WS] + connect: {username}", flush=True)
        CONNECT_LOG.appendleft({
            "time": datetime.now(MSK).strftime("%d.%m %H:%M:%S"),
            "user": username, "action": "connect",
        })
        self.active_connections.setdefault(username, set()).add(websocket)
        touch_last_seen(username)

    def disconnect(self, username: str, websocket: Optional[WebSocket] = None) -> None:
        socks = self.active_connections.get(username)
        if socks is None:
            return
        if websocket is None:
            self.active_connections.pop(username, None)
        else:
            socks.discard(websocket)
            if not socks:
                self.active_connections.pop(username, None)
        print(f"[WS] - disconnect: {username}", flush=True)
        CONNECT_LOG.appendleft({
            "time": datetime.now(MSK).strftime("%d.%m %H:%M:%S"),
            "user": username, "action": "disconnect",
        })
        touch_last_seen(username)

    def is_online(self, username: str) -> bool:
        return bool(self.active_connections.get(username))

    async def send_to_user(self, message: dict, target_user: str) -> bool:
        socks = list(self.active_connections.get(target_user, ()))
        if not socks:
            return False
        ok = False
        dead = []
        for ws in socks:
            try:
                await ws.send_json(message)
                ok = True
            except Exception as e:
                print(f"[WS] Ошибка отправки '{target_user}': {e}")
                dead.append(ws)
        for ws in dead:
            self.disconnect(target_user, ws)
        return ok


manager = ConnectionManager()


# ==========================================================
#                    ПРИЛОЖЕНИЕ FASTAPI
# ==========================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
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


# ==========================================================
#                    ОБЫЧНЫЕ ЭНДПОИНТЫ
# ==========================================================

@app.get("/")
def get_index():
    with open("index.html", "r", encoding="utf-8") as f:
        html = f.read()
    html = html.replace("__APP_VERSION__", APP_VERSION)
    html = html.replace("__ONESIGNAL_APP_ID__", ONE_SIGNAL_APP_ID)
    return Response(
        content=html,
        media_type="text/html; charset=utf-8",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


@app.get("/version")
def get_version():
    return JSONResponse(
        {"version": APP_VERSION},
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@app.get("/manifest.json")
def get_manifest():
    return FileResponse("manifest.json", media_type="application/manifest+json")


@app.get("/sw.js")
def get_sw():
    return FileResponse(
        "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


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
    require_auth(token)
    q = q.strip()
    if not q:
        return []
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT username, avatar_url FROM users WHERE banned = 0 OR banned IS NULL"
        ).fetchall()
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
        row = conn.execute("SELECT 1, banned FROM users WHERE username = ?", (username,)).fetchone()
        if row:
            if row[1]:
                raise HTTPException(status_code=403, detail="Этот пользователь забанен")
            raise HTTPException(status_code=400, detail="Этот никнейм уже занят")
        avatar_url = "/uploads/default.png"
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
            "INSERT INTO users (username, password_hash, avatar_url, bio, status_text) VALUES (?, ?, ?, ?, '')",
            (username, hash_password(password), avatar_url, ""),
        )
        conn.commit()
    finally:
        conn.close()
    token = create_session(username)
    return {"status": "ok", "username": username, "avatar_url": avatar_url,
            "bio": "", "status_text": "", "token": token}


@app.post("/login")
async def login(username: str = Form(...), password: str = Form(...)):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT password_hash, avatar_url, bio, status_text, banned FROM users WHERE username = ?", (username,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Пользователь не найден")
        if row[4]:
            raise HTTPException(status_code=403, detail="Вы забанены администратором")
        if not row[0]:
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
        "status_text": row[3] or "",
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
            "SELECT avatar_url, bio, status_text, last_seen, theme, banned FROM users WHERE username = ?", (username,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {"username": username, "avatar_url": "/uploads/default.png", "bio": "", "status_text": "",
                "last_seen": None, "theme": "dark", "banned": 0, "is_admin": is_admin(username)}
    return {
        "username": username,
        "avatar_url": row[0] or "/uploads/default.png",
        "bio": row[1] or "",
        "status_text": row[2] or "",
        "last_seen": row[3],
        "theme": row[4] or "dark",
        "banned": row[5] or 0,
        "is_admin": is_admin(username),
    }


@app.post("/profile/update")
async def update_profile(
    token: str = Form(...),
    bio: str = Form(""),
    status_text: str = Form(""),
    theme: str = Form("dark"),
    avatar: UploadFile = File(None),
):
    username = require_auth(token)
    bio = bio[:150]
    status_text = status_text[:40]
    theme = theme if theme in ("dark", "light", "auto") else "dark"
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
            "UPDATE users SET avatar_url = ?, bio = ?, status_text = ?, theme = ? WHERE username = ?",
            (avatar_url, bio, status_text, theme, username),
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok", "username": username, "avatar_url": avatar_url,
            "bio": bio, "status_text": status_text, "theme": theme,
            "is_admin": is_admin(username)}


@app.post("/password/change")
async def password_change(
    token: str = Form(...),
    old_password: str = Form(...),
    new_password: str = Form(...),
):
    username = require_auth(token)
    if len(new_password) < 4:
        raise HTTPException(status_code=400, detail="Новый пароль должен быть не короче 4 символов")
    conn = get_db()
    try:
        row = conn.execute("SELECT password_hash FROM users WHERE username = ?", (username,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Пользователь не найден")
        if row[0] and not verify_password(old_password, row[0]):
            raise HTTPException(status_code=401, detail="Неверный текущий пароль")
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?",
            (hash_password(new_password), username),
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok"}


@app.get("/sessions/list")
def sessions_list(token: str):
    username = require_auth(token)
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT token, created_at FROM sessions WHERE username = ? ORDER BY created_at DESC", (username,)
        ).fetchall()
    finally:
        conn.close()
    return [
        {"current": t == token, "token_prefix": (t[:6] + "…"), "created_at": utc_str_to_msk(ca)}
        for t, ca in rows
    ]


@app.post("/sessions/logout-all")
async def sessions_logout_all(token: str = Form(...)):
    username = require_auth(token)
    conn = get_db()
    try:
        conn.execute("DELETE FROM sessions WHERE username = ? AND token != ?", (username, token))
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok"}


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
#                    АДМИН: БАНЫ И РАССЫЛКА
# ==========================================================

@app.post("/admin/ban")
async def admin_ban(token: str = Form(...), username: str = Form(...)):
    require_admin(token)
    if username == ADMIN_USERNAME:
        raise HTTPException(status_code=400, detail="Нельзя забанить самого себя")
    conn = get_db()
    try:
        if not conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            raise HTTPException(status_code=404, detail="Пользователь не найден")
        conn.execute("UPDATE users SET banned = 1 WHERE username = ?", (username,))
        conn.execute("DELETE FROM sessions WHERE username = ?", (username,))
        conn.commit()
    finally:
        conn.close()
    for ws in list(manager.active_connections.get(username, ())):
        try:
            await ws.close(code=4003)
        except Exception:
            pass
    manager.disconnect(username)
    return {"status": "ok"}


@app.post("/admin/unban")
async def admin_unban(token: str = Form(...), username: str = Form(...)):
    require_admin(token)
    conn = get_db()
    try:
        conn.execute("UPDATE users SET banned = 0 WHERE username = ?", (username,))
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok"}


@app.post("/admin/broadcast")
async def admin_broadcast(
    token: str = Form(...),
    title: str = Form(...),
    body: str = Form(...),
):
    require_admin(token)
    title = title.strip()[:80] or "📢 Объявление"
    body = body.strip()[:300]
    payload = {"type": "broadcast", "title": title, "body": body}
    online_count = 0
    for uname in list(manager.active_connections.keys()):
        if await manager.send_to_user(payload, uname):
            online_count += 1
    conn = get_db()
    try:
        users = [r[0] for r in conn.execute(
            "SELECT DISTINCT username FROM push_subs"
        ).fetchall()]
    finally:
        conn.close()
    for uname in users:
        await asyncio.to_thread(send_push_to_user, uname, f"📢 {title}", body)
    onesignal_ok = await asyncio.to_thread(send_onesignal_broadcast, f"📢 {title}", body)
    print(f"[ADMIN] broadcast: online={online_count}, webpush={len(users)}, onesignal={onesignal_ok}", flush=True)
    return {
        "status": "ok",
        "online": online_count,
        "pushed": len(users),
        "onesignal": onesignal_ok,
        "onesignal_error": "" if onesignal_ok else ONESIGNAL_LAST_ERROR,
    }


# ==========================================================
#                    ЧЁРНЫЙ СПИСОК
# ==========================================================

@app.post("/blocks/add")
async def blocks_add(token: str = Form(...), username: str = Form(...)):
    me = require_auth(token)
    if username == me:
        raise HTTPException(status_code=400, detail="Нельзя заблокировать самого себя")
    conn = get_db()
    try:
        if not conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            raise HTTPException(status_code=404, detail="Пользователь не найден")
        conn.execute(
            "INSERT OR IGNORE INTO blocks (blocker, blocked) VALUES (?, ?)", (me, username)
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok"}


@app.post("/blocks/remove")
async def blocks_remove(token: str = Form(...), username: str = Form(...)):
    me = require_auth(token)
    conn = get_db()
    try:
        conn.execute("DELETE FROM blocks WHERE blocker = ? AND blocked = ?", (me, username))
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok"}


@app.get("/blocks/my/{username}")
def blocks_my(username: str, token: str):
    me = require_auth(token)
    if me != username:
        raise HTTPException(status_code=403, detail="Нет доступа")
    conn = get_db()
    try:
        rows = conn.execute("SELECT blocked FROM blocks WHERE blocker = ?", (me,)).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


# ==========================================================
#              РЕДАКТИРОВАНИЕ И УДАЛЕНИЕ СООБЩЕНИЙ
# ==========================================================

@app.post("/messages/{msg_id}/edit")
async def message_edit(msg_id: int, token: str = Form(...), content: str = Form(...)):
    me = require_auth(token)
    content = content.strip()
    if not content or len(content) > 2000:
        raise HTTPException(status_code=400, detail="Некорректный текст сообщения")
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT chat_id, sender, msg_type FROM messages WHERE id = ?", (msg_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Сообщение не найдено")
        chat_id, sender, msg_type = row
        if sender != me:
            raise HTTPException(status_code=403, detail="Можно редактировать только свои сообщения")
        if msg_type != "text":
            raise HTTPException(status_code=400, detail="Редактировать можно только текстовые сообщения")
        conn.execute(
            "UPDATE messages SET content = ?, edited_at = CURRENT_TIMESTAMP WHERE id = ?",
            (content, msg_id),
        )
        conn.commit()
    finally:
        conn.close()
    payload = {"type": "message_edit", "message_id": msg_id, "content": content}
    for user in chat_recipients(chat_id):
        await manager.send_to_user(payload, user)
    return {"status": "ok"}


@app.post("/messages/{msg_id}/delete")
async def message_delete(msg_id: int, token: str = Form(...)):
    me = require_auth(token)
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT chat_id, sender FROM messages WHERE id = ?", (msg_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Сообщение не найдено")
        chat_id, sender = row
        if sender != me:
            raise HTTPException(status_code=403, detail="Можно удалять только свои сообщения")
        conn.execute(
            "UPDATE messages SET msg_type = 'deleted', content = '', edited_at = NULL WHERE id = ?",
            (msg_id,),
        )
        conn.commit()
    finally:
        conn.close()
    payload = {"type": "message_delete", "message_id": msg_id}
    for user in chat_recipients(chat_id):
        await manager.send_to_user(payload, user)
    return {"status": "ok"}


# ==========================================================
#                    АДМИН-ПАНЕЛЬ
# ==========================================================

@app.get("/admin/stats")
def admin_stats(token: str):
    require_admin(token)
    conn = get_db()
    try:
        users = [
            {
                "username": r[0],
                "avatar_url": r[1] or "/uploads/default.png",
                "status_text": r[2] or "",
                "created_at": utc_str_to_msk(r[3]),
                "banned": r[4] or 0,
            }
            for r in conn.execute(
                "SELECT username, avatar_url, status_text, created_at, banned FROM users ORDER BY rowid"
            ).fetchall()
        ]
        messages_total = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE msg_type != 'deleted'"
        ).fetchone()[0]
    finally:
        conn.close()
    return {
        "online": list(manager.active_connections.keys()),
        "users": users,
        "messages_total": messages_total,
        "log": list(CONNECT_LOG),
    }


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
SELECT m.id, m.chat_id, m.sender, m.content, m.msg_type, m.status, m.reply_to, m.timestamp, m.edited_at, m.duration,
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
        await websocket.accept()
        await websocket.close(code=4001)
        return
    if is_banned(username):
        await websocket.accept()
        await websocket.close(code=4003)
        return

    await manager.connect(username, websocket)
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type", "message")

            if msg_type == "message":
                is_group = bool(data.get("is_group", False))
                target = data.get("target")
                content = data.get("content", "").strip()
                content_type = data.get("msg_type", "text")
                duration = data.get("duration")
                reply_to_id = data.get("reply_to")

                if not target or not content:
                    continue
                if len(content) > 2000:
                    continue
                if is_group and not is_group_member(target, username):
                    continue
                if not is_group and is_blocked(target, username):
                    await websocket.send_json({
                        "type": "error",
                        "detail": f"@{target} ограничил(а) вам сообщения",
                    })
                    continue

                chat_id = target if is_group else get_chat_id(username, target)
                initial_status = "delivered" if (not is_group and manager.is_online(target)) else "sent"
                conn = get_db()
                try:
                    cursor = conn.cursor()
                    cursor.execute(
                        "INSERT INTO messages (chat_id, sender, content, msg_type, status, reply_to, duration) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (chat_id, username, content, content_type, initial_status, reply_to_id, duration if isinstance(duration, int) else None),
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
                    "is_group": is_group, "duration": duration, "edited": False,
                    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    "reply_to": reply_preview, "reactions": [],
                }
                if is_group:
                    for member_user in get_group_member_usernames(target):
                        if member_user == username:
                            continue
                        ok = await manager.send_to_user(msg_payload, member_user)
                        if not ok:
                            await asyncio.to_thread(
                                notify_offline, member_user,
                                f"👥 {username}", content[:100],
                            )
                else:
                    await manager.send_to_user(msg_payload, username)
                    if target != username:
                        ok = await manager.send_to_user(msg_payload, target)
                        if not ok:
                            await asyncio.to_thread(
                                notify_offline, target,
                                f"💬 {username}", content[:100],
                            )

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

            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})
                continue

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
        manager.disconnect(username, websocket)
    except Exception as e:
        print(f"[WS] Неожиданная ошибка у '{username}': {e}")
        manager.disconnect(username, websocket)