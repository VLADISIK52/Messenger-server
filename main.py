import os
import re
import hashlib
import secrets
import sqlite3
from typing import Dict, List, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

# Разрешаем запросы с любого источника — это важно для APK-обёртки,
# так как некоторые генераторы приложений грузят страницу не с самого
# домена сервера, и без CORS браузерный движок внутри приложения
# будет блокировать запросы к /login, /register и остальным эндпоинтам.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = "uploads"
STATIC_DIR = "static"
DB_FILE = "chat.db"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

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
#                        БАЗА ДАННЫХ
# ==========================================================

def get_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    return conn


def safe_alter(cursor, sql):
    try:
        cursor.execute(sql)
    except sqlite3.OperationalError:
        pass


def init_db():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password_hash TEXT,
            avatar_url TEXT DEFAULT '/uploads/default.png',
            bio TEXT DEFAULT ''
        )
    ''')
    safe_alter(cursor, "ALTER TABLE users ADD COLUMN bio TEXT DEFAULT ''")
    safe_alter(cursor, "ALTER TABLE users ADD COLUMN password_hash TEXT")

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            username TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT,
            sender TEXT,
            content TEXT,
            msg_type TEXT DEFAULT 'text',
            status TEXT DEFAULT 'sent',
            reply_to INTEGER,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    safe_alter(cursor, "ALTER TABLE messages ADD COLUMN reply_to INTEGER")

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS reactions (
            message_id INTEGER,
            username TEXT,
            emoji TEXT,
            PRIMARY KEY (message_id, username)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS groups (
            group_id TEXT PRIMARY KEY,
            title TEXT,
            owner TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS group_members (
            group_id TEXT,
            username TEXT,
            PRIMARY KEY (group_id, username)
        )
    ''')

    conn.commit()
    conn.close()


init_db()


def get_chat_id(user1: str, user2: str) -> str:
    return "_".join(sorted([user1, user2]))


# ==========================================================
#                    ПАРОЛИ И АВТОРИЗАЦИЯ
# ==========================================================

def hash_password(password: str) -> str:
    salt = os.urandom(16).hex()
    dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt), 100_000)
    return f"{salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, hash_hex = stored.split('$')
        dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt), 100_000)
        return secrets.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def create_session(username: str) -> str:
    token = secrets.token_hex(24)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO sessions (token, username) VALUES (?, ?)", (token, username))
    conn.commit()
    conn.close()
    return token


def get_username_by_token(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT username FROM sessions WHERE token = ?", (token,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def require_auth(token: Optional[str]) -> str:
    """Возвращает username владельца токена или бросает 401"""
    username = get_username_by_token(token)
    if not username:
        raise HTTPException(status_code=401, detail="Не авторизован. Войдите заново.")
    return username


def is_group_member(group_id: str, username: str) -> bool:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT 1 FROM group_members WHERE group_id = ? AND username = ?",
        (group_id, username)
    )
    row = cursor.fetchone()
    conn.close()
    return row is not None


def fetch_reactions_for_messages(cursor, message_ids: List[int]) -> Dict[int, List[dict]]:
    if not message_ids:
        return {}
    placeholders = ",".join("?" * len(message_ids))
    cursor.execute(
        f"SELECT message_id, username, emoji FROM reactions WHERE message_id IN ({placeholders})",
        message_ids
    )
    result: Dict[int, List[dict]] = {}
    for message_id, username, emoji in cursor.fetchall():
        result.setdefault(message_id, []).append({"username": username, "emoji": emoji})
    return result


def rows_to_messages(cursor, rows) -> List[dict]:
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
                "content": reply_content, "msg_type": reply_msg_type
            }

        result.append({
            "id": msg_id, "chat_id": chat_id, "sender": sender,
            "content": content, "msg_type": msg_type, "status": status,
            "timestamp": timestamp, "reply_to": reply_preview,
            "reactions": reactions_map.get(msg_id, [])
        })
    return result


def get_group_member_usernames(group_id: str) -> List[str]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT username FROM group_members WHERE group_id = ?", (group_id,))
    members = [row[0] for row in cursor.fetchall()]
    conn.close()
    return members


# ==========================================================
#              МЕНЕДЖЕР WEBSOCKET-СОЕДИНЕНИЙ
# ==========================================================

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, username: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[username] = websocket

    def disconnect(self, username: str):
        if username in self.active_connections:
            del self.active_connections[username]

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
            return False


manager = ConnectionManager()


# ==========================================================
#                      ОБЫЧНЫЕ ЭНДПОИНТЫ
# ==========================================================

@app.get("/")
def get_index():
    return FileResponse("index.html")


@app.get("/manifest.json")
def get_manifest():
    return FileResponse("manifest.json", media_type="application/manifest+json")


@app.get("/sw.js")
def get_sw():
    return FileResponse("sw.js", media_type="application/javascript")


@app.post("/register")
async def register(username: str = Form(...), password: str = Form(...), avatar: UploadFile = File(None)):
    if not USERNAME_RE.match(username):
        raise HTTPException(status_code=400, detail="Никнейм должен быть 3-20 символов: только буквы (рус/лат) и цифры")
    if len(password) < 4:
        raise HTTPException(status_code=400, detail="Пароль должен быть не короче 4 символов")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT username FROM users WHERE username = ?", (username,))
    if cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Этот никнейм уже занят")

    avatar_url = "/uploads/default.png"
    if avatar:
        file_path = os.path.join(UPLOAD_DIR, avatar.filename)
        with open(file_path, "wb") as f:
            f.write(await avatar.read())
        avatar_url = f"/uploads/{avatar.filename}"

    cursor.execute(
        "INSERT INTO users (username, password_hash, avatar_url, bio) VALUES (?, ?, ?, ?)",
        (username, hash_password(password), avatar_url, "")
    )
    conn.commit()
    conn.close()

    token = create_session(username)
    return {"status": "ok", "username": username, "avatar_url": avatar_url, "bio": "", "token": token}


@app.post("/login")
async def login(username: str = Form(...), password: str = Form(...)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT password_hash, avatar_url, bio FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    if not row[0]:
        # Старый аккаунт, созданный ещё до появления паролей — задаём пароль сейчас
        cursor.execute("UPDATE users SET password_hash = ? WHERE username = ?", (hash_password(password), username))
        conn.commit()
    elif not verify_password(password, row[0]):
        conn.close()
        raise HTTPException(status_code=401, detail="Неверный пароль")

    conn.close()
    token = create_session(username)
    return {
        "status": "ok", "username": username,
        "avatar_url": row[1] or "/uploads/default.png", "bio": row[2] or "", "token": token
    }


@app.post("/logout")
async def logout(token: str = Form(...)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.get("/profile/{username}")
def get_profile(username: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT avatar_url, bio FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return {"username": username, "avatar_url": "/uploads/default.png", "bio": ""}
    return {"username": username, "avatar_url": row[0] or "/uploads/default.png", "bio": row[1] or ""}


@app.post("/profile/update")
async def update_profile(
    token: str = Form(...),
    bio: str = Form(""),
    avatar: UploadFile = File(None)
):
    username = require_auth(token)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT avatar_url FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    avatar_url = row[0] if row else "/uploads/default.png"

    if avatar:
        file_path = os.path.join(UPLOAD_DIR, avatar.filename)
        with open(file_path, "wb") as f:
            f.write(await avatar.read())
        avatar_url = f"/uploads/{avatar.filename}"

    cursor.execute("UPDATE users SET avatar_url = ?, bio = ? WHERE username = ?", (avatar_url, bio, username))
    conn.commit()
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
#                     РАБОТА С ГРУППАМИ
# ==========================================================

@app.post("/groups/create")
async def create_group(token: str = Form(...), title: str = Form(...), members: str = Form(...)):
    owner = require_auth(token)
    member_list = [m.strip() for m in members.split(",") if m.strip()]
    if owner not in member_list:
        member_list.append(owner)

    group_id = f"group_{os.urandom(6).hex()}"

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO groups (group_id, title, owner) VALUES (?, ?, ?)", (group_id, title, owner))
    for m in member_list:
        cursor.execute("INSERT INTO group_members (group_id, username) VALUES (?, ?)", (group_id, m))
    conn.commit()
    conn.close()

    return {"status": "ok", "group_id": group_id, "title": title, "members": member_list}


@app.get("/groups/my/{username}")
def get_user_groups(username: str, token: str):
    requester = require_auth(token)
    if requester != username:
        raise HTTPException(status_code=403, detail="Нет доступа")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT g.group_id, g.title, g.owner
        FROM groups g
        JOIN group_members gm ON g.group_id = gm.group_id
        WHERE gm.username = ?
    ''', (username,))
    rows = cursor.fetchall()
    conn.close()
    return [{"group_id": r[0], "title": r[1], "owner": r[2]} for r in rows]


@app.get("/groups/{group_id}/members")
def get_group_members(group_id: str, token: str):
    requester = require_auth(token)
    if not is_group_member(group_id, requester):
        raise HTTPException(status_code=403, detail="Нет доступа к этой группе")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT owner FROM groups WHERE group_id = ?", (group_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Группа не найдена")
    owner = row[0]
    cursor.execute("SELECT username FROM group_members WHERE group_id = ?", (group_id,))
    members = [r[0] for r in cursor.fetchall()]
    conn.close()
    return {"group_id": group_id, "owner": owner, "members": members}


@app.post("/groups/{group_id}/members/add")
async def add_group_member(group_id: str, token: str = Form(...), username: str = Form(...)):
    requester = require_auth(token)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT owner FROM groups WHERE group_id = ?", (group_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Группа не найдена")
    if row[0] != requester:
        conn.close()
        raise HTTPException(status_code=403, detail="Только создатель может добавлять участников")

    cursor.execute("INSERT OR IGNORE INTO group_members (group_id, username) VALUES (?, ?)", (group_id, username))
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.post("/groups/{group_id}/members/remove")
async def remove_group_member(group_id: str, token: str = Form(...), username: str = Form(...)):
    requester = require_auth(token)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT owner FROM groups WHERE group_id = ?", (group_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Группа не найдена")
    if row[0] != requester:
        conn.close()
        raise HTTPException(status_code=403, detail="Только создатель может удалять участников")
    if username == row[0]:
        conn.close()
        raise HTTPException(status_code=400, detail="Создатель не может удалить сам себя, используйте выход из группы")

    cursor.execute("DELETE FROM group_members WHERE group_id = ? AND username = ?", (group_id, username))
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.post("/groups/{group_id}/leave")
async def leave_group(group_id: str, token: str = Form(...)):
    username = require_auth(token)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT owner FROM groups WHERE group_id = ?", (group_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Группа не найдена")

    cursor.execute("DELETE FROM group_members WHERE group_id = ? AND username = ?", (group_id, username))
    cursor.execute("SELECT username FROM group_members WHERE group_id = ?", (group_id,))
    remaining = [r[0] for r in cursor.fetchall()]

    if not remaining:
        cursor.execute("DELETE FROM groups WHERE group_id = ?", (group_id,))
        cursor.execute("DELETE FROM messages WHERE chat_id = ?", (group_id,))
    elif row[0] == username:
        cursor.execute("UPDATE groups SET owner = ? WHERE group_id = ?", (remaining[0], group_id))

    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.delete("/groups/{group_id}")
def delete_group(group_id: str, token: str):
    owner = require_auth(token)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT owner FROM groups WHERE group_id = ?", (group_id,))
    row = cursor.fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Группа не найдена")
    if row[0] != owner:
        conn.close()
        raise HTTPException(status_code=403, detail="Только создатель может удалить группу")

    cursor.execute("DELETE FROM groups WHERE group_id = ?", (group_id,))
    cursor.execute("DELETE FROM group_members WHERE group_id = ?", (group_id,))
    cursor.execute("DELETE FROM messages WHERE chat_id = ?", (group_id,))

    conn.commit()
    conn.close()
    return {"status": "deleted"}


# ==========================================================
#                 СООБЩЕНИЯ И ИСТОРИЯ ЧАТОВ
# ==========================================================

def build_paginated_sql(before_id: Optional[int]) -> str:
    if before_id:
        return '''
            SELECT m.id, m.chat_id, m.sender, m.content, m.msg_type, m.status, m.reply_to, m.timestamp,
                   r.sender, r.content, r.msg_type
            FROM messages m
            LEFT JOIN messages r ON m.reply_to = r.id
            WHERE m.chat_id = ? AND m.id < ?
            ORDER BY m.id DESC LIMIT ?
        '''
    return '''
        SELECT m.id, m.chat_id, m.sender, m.content, m.msg_type, m.status, m.reply_to, m.timestamp,
               r.sender, r.content, r.msg_type
        FROM messages m
        LEFT JOIN messages r ON m.reply_to = r.id
        WHERE m.chat_id = ?
        ORDER BY m.id DESC LIMIT ?
    '''


@app.get("/messages/{user1}/{user2}")
def get_messages(user1: str, user2: str, token: str, before_id: Optional[int] = None):
    requester = require_auth(token)
    if requester not in (user1, user2):
        raise HTTPException(status_code=403, detail="Нет доступа к этой переписке")

    chat_id = get_chat_id(user1, user2)
    conn = get_db()
    cursor = conn.cursor()
    sql = build_paginated_sql(before_id)
    params = (chat_id, before_id, MESSAGES_PAGE_SIZE) if before_id else (chat_id, MESSAGES_PAGE_SIZE)
    cursor.execute(sql, params)
    rows = list(reversed(cursor.fetchall()))
    result = rows_to_messages(cursor, rows)
    conn.close()
    return result


@app.get("/group-messages/{group_id}")
def get_group_messages(group_id: str, token: str, before_id: Optional[int] = None):
    requester = require_auth(token)
    if not is_group_member(group_id, requester):
        raise HTTPException(status_code=403, detail="Нет доступа к этой группе")

    conn = get_db()
    cursor = conn.cursor()
    sql = build_paginated_sql(before_id)
    params = (group_id, before_id, MESSAGES_PAGE_SIZE) if before_id else (group_id, MESSAGES_PAGE_SIZE)
    cursor.execute(sql, params)
    rows = list(reversed(cursor.fetchall()))
    result = rows_to_messages(cursor, rows)
    conn.close()
    return result


@app.delete("/messages/{user1}/{user2}")
def delete_chat(user1: str, user2: str, token: str):
    requester = require_auth(token)
    if requester not in (user1, user2):
        raise HTTPException(status_code=403, detail="Нет доступа к этой переписке")

    chat_id = get_chat_id(user1, user2)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted"}


# ==========================================================
#                     WEBSOCKET: ГЛАВНАЯ ЛОГИКА
# ==========================================================

@app.websocket("/ws/{username}")
async def websocket_endpoint(websocket: WebSocket, username: str, token: Optional[str] = None):
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
                content = data.get("content", "")
                content_type = data.get("msg_type", "text")
                duration = data.get("duration")
                reply_to_id = data.get("reply_to")

                if not target or not content:
                    continue

                # Проверяем, что отправитель реально состоит в группе
                if is_group and not is_group_member(target, username):
                    continue

                chat_id = target if is_group else get_chat_id(username, target)

                initial_status = "sent"
                if not is_group and manager.is_online(target):
                    initial_status = "delivered"

                conn = get_db()
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO messages (chat_id, sender, content, msg_type, status, reply_to) VALUES (?, ?, ?, ?, ?, ?)",
                    (chat_id, username, content, content_type, initial_status, reply_to_id)
                )
                conn.commit()
                msg_id = cursor.lastrowid

                reply_preview = None
                if reply_to_id:
                    cursor.execute("SELECT sender, content, msg_type FROM messages WHERE id = ?", (reply_to_id,))
                    rr = cursor.fetchone()
                    if rr:
                        reply_preview = {"id": reply_to_id, "sender": rr[0], "content": rr[1], "msg_type": rr[2]}
                conn.close()

                msg_payload = {
                    "type": "message", "id": msg_id, "chat_id": chat_id, "target": target,
                    "sender": username, "content": content, "msg_type": content_type,
                    "status": initial_status, "is_group": is_group, "duration": duration,
                    "reply_to": reply_preview, "reactions": []
                }

                if is_group:
                    for member_user in get_group_member_usernames(target):
                        await manager.send_to_user(msg_payload, member_user)
                else:
                    await manager.send_to_user(msg_payload, username)
                    if target != username:
                        await manager.send_to_user(msg_payload, target)

            # ------------------------------------------------
            # 2. ОТМЕТКА "ПРОЧИТАНО"
            # ------------------------------------------------
            elif msg_type == "read_receipt":
                chat_id = data.get("chat_id")
                sender_to_notify = data.get("sender")
                if not chat_id or not sender_to_notify:
                    continue

                conn = get_db()
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE messages SET status = 'read' WHERE chat_id = ? AND sender = ?",
                    (chat_id, sender_to_notify)
                )
                conn.commit()
                conn.close()

                await manager.send_to_user({
                    "type": "status_update", "chat_id": chat_id, "status": "read"
                }, sender_to_notify)

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
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT emoji FROM reactions WHERE message_id = ? AND username = ?",
                    (message_id, username)
                )
                existing = cursor.fetchone()

                if existing and existing[0] == emoji:
                    cursor.execute(
                        "DELETE FROM reactions WHERE message_id = ? AND username = ?",
                        (message_id, username)
                    )
                else:
                    cursor.execute(
                        "REPLACE INTO reactions (message_id, username, emoji) VALUES (?, ?, ?)",
                        (message_id, username, emoji)
                    )
                conn.commit()

                cursor.execute("SELECT username, emoji FROM reactions WHERE message_id = ?", (message_id,))
                reactions = [{"username": r[0], "emoji": r[1]} for r in cursor.fetchall()]
                conn.close()

                update_payload = {"type": "reaction_update", "message_id": message_id, "reactions": reactions}

                if is_group:
                    for member_user in get_group_member_usernames(target):
                        await manager.send_to_user(update_payload, member_user)
                else:
                    await manager.send_to_user(update_payload, username)
                    if target != username:
                        await manager.send_to_user(update_payload, target)

            # ------------------------------------------------
            # 4. ИНДИКАТОР "ПЕЧАТАЕТ..."
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
                    "type": "typing", "sender": username, "target": target,
                    "is_group": is_group, "is_typing": is_typing
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