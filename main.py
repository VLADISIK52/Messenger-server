import os
import sqlite3
from typing import Dict
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

app = FastAPI()

UPLOAD_DIR = "uploads"
DB_FILE = "chat.db"
os.makedirs(UPLOAD_DIR, exist_ok=True)

app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")


# ==========================================================
#                        БАЗА ДАННЫХ
# ==========================================================

def get_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    return conn


def init_db():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            avatar_url TEXT DEFAULT '/uploads/default.png',
            bio TEXT DEFAULT ''
        )
    ''')

    # На случай, если таблица users уже существовала раньше без колонки bio
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN bio TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # колонка уже есть

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT,
            sender TEXT,
            content TEXT,
            msg_type TEXT DEFAULT 'text',
            status TEXT DEFAULT 'sent',
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
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
#              МЕНЕДЖЕР WEBSOCKET-СОЕДИНЕНИЙ
# ==========================================================

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, username: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[username] = websocket
        print(f"[WS] {username} подключился. Онлайн: {list(self.active_connections.keys())}")

    def disconnect(self, username: str):
        if username in self.active_connections:
            del self.active_connections[username]
        print(f"[WS] {username} отключился. Онлайн: {list(self.active_connections.keys())}")

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


@app.post("/register")
async def register(username: str = Form(...), avatar: UploadFile = File(None)):
    conn = get_db()
    cursor = conn.cursor()

    # Если юзер уже существует - не затираем его аватар/био
    cursor.execute("SELECT avatar_url, bio FROM users WHERE username = ?", (username,))
    existing = cursor.fetchone()

    avatar_url = existing[0] if existing else "/uploads/default.png"
    bio = existing[1] if existing else ""

    if avatar:
        file_path = os.path.join(UPLOAD_DIR, avatar.filename)
        with open(file_path, "wb") as f:
            f.write(await avatar.read())
        avatar_url = f"/uploads/{avatar.filename}"

    cursor.execute(
        "REPLACE INTO users (username, avatar_url, bio) VALUES (?, ?, ?)",
        (username, avatar_url, bio)
    )
    conn.commit()
    conn.close()

    return {"status": "ok", "username": username, "avatar_url": avatar_url, "bio": bio}


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
    username: str = Form(...),
    bio: str = Form(""),
    avatar: UploadFile = File(None)
):
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

    cursor.execute(
        "REPLACE INTO users (username, avatar_url, bio) VALUES (?, ?, ?)",
        (username, avatar_url, bio)
    )
    conn.commit()
    conn.close()

    return {"status": "ok", "username": username, "avatar_url": avatar_url, "bio": bio}


@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    file_path = os.path.join(UPLOAD_DIR, file.filename)
    with open(file_path, "wb") as f:
        f.write(await file.read())
    return {"file_url": f"/uploads/{file.filename}"}


@app.get("/users")
def get_online_users():
    return list(manager.active_connections.keys())


# ==========================================================
#                     РАБОТА С ГРУППАМИ
# ==========================================================

@app.post("/groups/create")
async def create_group(title: str = Form(...), owner: str = Form(...), members: str = Form(...)):
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
def get_user_groups(username: str):
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


@app.delete("/groups/{group_id}")
def delete_group(group_id: str, owner: str):
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

@app.get("/messages/{user1}/{user2}")
def get_messages(user1: str, user2: str):
    chat_id = get_chat_id(user1, user2)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, chat_id, sender, content, msg_type, status, timestamp FROM messages WHERE chat_id = ? ORDER BY timestamp ASC",
        (chat_id,)
    )
    rows = cursor.fetchall()
    conn.close()

    return [{
        "id": r[0], "chat_id": r[1], "sender": r[2],
        "content": r[3], "msg_type": r[4], "status": r[5], "timestamp": r[6]
    } for r in rows]


@app.get("/group-messages/{group_id}")
def get_group_messages(group_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, chat_id, sender, content, msg_type, status, timestamp FROM messages WHERE chat_id = ? ORDER BY timestamp ASC",
        (group_id,)
    )
    rows = cursor.fetchall()
    conn.close()

    return [{
        "id": r[0], "chat_id": r[1], "sender": r[2],
        "content": r[3], "msg_type": r[4], "status": r[5], "timestamp": r[6]
    } for r in rows]


@app.delete("/messages/{user1}/{user2}")
def delete_chat(user1: str, user2: str):
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
async def websocket_endpoint(websocket: WebSocket, username: str):
    await manager.connect(username, websocket)
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type", "message")

            # ------------------------------------------------
            # 1. НОВОЕ СООБЩЕНИЕ (текст, картинка, файл, голосовое)
            # ------------------------------------------------
            if msg_type == "message":
                is_group = bool(data.get("is_group", False))
                target = data.get("target")
                content = data.get("content", "")
                content_type = data.get("msg_type", "text")
                # длительность голосового сообщения в секундах (опционально)
                duration = data.get("duration")

                if not target or not content:
                    continue

                chat_id = target if is_group else get_chat_id(username, target)

                initial_status = "sent"
                if not is_group and manager.is_online(target):
                    initial_status = "delivered"

                conn = get_db()
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO messages (chat_id, sender, content, msg_type, status) VALUES (?, ?, ?, ?, ?)",
                    (chat_id, username, content, content_type, initial_status)
                )
                conn.commit()
                msg_id = cursor.lastrowid
                conn.close()

                msg_payload = {
                    "type": "message",
                    "id": msg_id,
                    "chat_id": chat_id,
                    "target": target,
                    "sender": username,
                    "content": content,
                    "msg_type": content_type,
                    "status": initial_status,
                    "is_group": is_group,
                    "duration": duration
                }

                if is_group:
                    conn = get_db()
                    cursor = conn.cursor()
                    cursor.execute("SELECT username FROM group_members WHERE group_id = ?", (target,))
                    members = [row[0] for row in cursor.fetchall()]
                    conn.close()

                    for member_user in members:
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
                    "type": "status_update",
                    "chat_id": chat_id,
                    "status": "read"
                }, sender_to_notify)

    except WebSocketDisconnect:
        manager.disconnect(username)
    except Exception as e:
        print(f"[WS] Неожиданная ошибка у '{username}': {e}")
        manager.disconnect(username)