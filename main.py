import os
import sqlite3
from typing import Dict, List
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

app = FastAPI()

UPLOAD_DIR = "uploads"
STATIC_DIR = "static"
DB_FILE = "chat.db"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ==========================================================
#                        БАЗА ДАННЫХ
# ==========================================================

def get_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    return conn


def safe_alter(cursor, sql):
    """Пытается добавить колонку в существующую таблицу, игнорируя ошибку если она уже есть"""
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
            avatar_url TEXT DEFAULT '/uploads/default.png',
            bio TEXT DEFAULT ''
        )
    ''')
    safe_alter(cursor, "ALTER TABLE users ADD COLUMN bio TEXT DEFAULT ''")

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


def fetch_reactions_for_messages(cursor, message_ids: List[int]) -> Dict[int, List[dict]]:
    """Возвращает {message_id: [{username, emoji}, ...]} для списка id сообщений"""
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
    """Преобразует строки SQL (с LEFT JOIN на reply) в список словарей с превью ответа и реакциями"""
    ids = [r[0] for r in rows]
    reactions_map = fetch_reactions_for_messages(cursor, ids)

    result = []
    for r in rows:
        (msg_id, chat_id, sender, content, msg_type, status, reply_to,
         timestamp, reply_sender, reply_content, reply_msg_type) = r

        reply_preview = None
        if reply_to and reply_sender is not None:
            reply_preview = {
                "id": reply_to,
                "sender": reply_sender,
                "content": reply_content,
                "msg_type": reply_msg_type
            }

        result.append({
            "id": msg_id, "chat_id": chat_id, "sender": sender,
            "content": content, "msg_type": msg_type, "status": status,
            "timestamp": timestamp, "reply_to": reply_preview,
            "reactions": reactions_map.get(msg_id, [])
        })
    return result


MESSAGES_SELECT_SQL = '''
    SELECT m.id, m.chat_id, m.sender, m.content, m.msg_type, m.status, m.reply_to, m.timestamp,
           r.sender, r.content, r.msg_type
    FROM messages m
    LEFT JOIN messages r ON m.reply_to = r.id
    WHERE m.chat_id = ?
    ORDER BY m.timestamp ASC
'''


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


def get_group_member_usernames(group_id: str) -> List[str]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT username FROM group_members WHERE group_id = ?", (group_id,))
    members = [row[0] for row in cursor.fetchall()]
    conn.close()
    return members


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
async def register(username: str = Form(...), avatar: UploadFile = File(None)):
    conn = get_db()
    cursor = conn.cursor()

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


@app.get("/groups/{group_id}/members")
def get_group_members(group_id: str):
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
async def add_group_member(group_id: str, username: str = Form(...), requester: str = Form(...)):
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
async def remove_group_member(group_id: str, username: str = Form(...), requester: str = Form(...)):
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
async def leave_group(group_id: str, username: str = Form(...)):
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
        # Группа опустела - удаляем её полностью
        cursor.execute("DELETE FROM groups WHERE group_id = ?", (group_id,))
        cursor.execute("DELETE FROM messages WHERE chat_id = ?", (group_id,))
    elif row[0] == username:
        # Владелец вышел - передаём права первому оставшемуся участнику
        cursor.execute("UPDATE groups SET owner = ? WHERE group_id = ?", (remaining[0], group_id))

    conn.commit()
    conn.close()
    return {"status": "ok"}


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
    cursor.execute(MESSAGES_SELECT_SQL, (chat_id,))
    rows = cursor.fetchall()
    result = rows_to_messages(cursor, rows)
    conn.close()
    return result


@app.get("/group-messages/{group_id}")
def get_group_messages(group_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(MESSAGES_SELECT_SQL, (group_id,))
    rows = cursor.fetchall()
    result = rows_to_messages(cursor, rows)
    conn.close()
    return result


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
            # 1. НОВОЕ СООБЩЕНИЕ (текст, картинка, файл, голосовое, стикер, ответ)
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
                    "type": "message",
                    "id": msg_id,
                    "chat_id": chat_id,
                    "target": target,
                    "sender": username,
                    "content": content,
                    "msg_type": content_type,
                    "status": initial_status,
                    "is_group": is_group,
                    "duration": duration,
                    "reply_to": reply_preview,
                    "reactions": []
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
                    "type": "status_update",
                    "chat_id": chat_id,
                    "status": "read"
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

                update_payload = {
                    "type": "reaction_update",
                    "message_id": message_id,
                    "reactions": reactions
                }

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

                typing_payload = {
                    "type": "typing",
                    "sender": username,
                    "target": target,
                    "is_group": is_group,
                    "is_typing": is_typing
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