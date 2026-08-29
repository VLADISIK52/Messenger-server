import os
import sqlite3
from typing import Dict, List, Set
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

app = FastAPI()

UPLOAD_DIR = "uploads"
DB_FILE = "chat.db"
os.makedirs(UPLOAD_DIR, exist_ok=True)

app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# --- БАЗА ДАННЫХ ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            avatar_url TEXT
        )
    ''')
    
    # Поле status (sent / delivered / read)
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
    
    # Таблицы для групп
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

# --- МЕНЕДЖЕР WEBSOCKET СОЕДИНЕНИЙ ---
class ConnectionManager:
    def __init__(self):
        # username -> WebSocket
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, username: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[username] = websocket

    def disconnect(self, username: str):
        if username in self.active_connections:
            del self.active_connections[username]

    def is_online(self, username: str) -> bool:
        return username in self.active_connections

    async def send_personal_message(self, message: dict, target_user: str) -> bool:
        """Возвращает True, если сообщение было доставлено (юзер онлайн)"""
        if target_user in self.active_connections:
            try:
                await self.active_connections[target_user].send_json(message)
                return True
            except Exception:
                pass
        return False

manager = ConnectionManager()

# --- ЭНДПОИНТЫ API ---

@app.get("/")
def get_index():
    return FileResponse("index.html")

@app.post("/register")
async def register(username: str = Form(...), avatar: UploadFile = File(None)):
    avatar_url = "/uploads/default.png"
    if avatar:
        file_path = os.path.join(UPLOAD_DIR, avatar.filename)
        with open(file_path, "wb") as f:
            f.write(await avatar.read())
        avatar_url = f"/uploads/{avatar.filename}"

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("REPLACE INTO users (username, avatar_url) VALUES (?, ?)", (username, avatar_url))
    conn.commit()
    conn.close()
    
    return {"status": "ok", "username": username, "avatar_url": avatar_url}

@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    file_path = os.path.join(UPLOAD_DIR, file.filename)
    with open(file_path, "wb") as f:
        f.write(await file.read())
    return {"file_url": f"/uploads/{file.filename}"}

@app.get("/users")
def get_online_users():
    return list(manager.active_connections.keys())

# --- РАБОТА С ГРУППАМИ ---

@app.post("/groups/create")
async def create_group(title: str = Form(...), owner: str = Form(...), members: str = Form(...)):
    member_list = [m.strip() for m in members.split(",") if m.strip()]
    if owner not in member_list:
        member_list.append(owner)
        
    group_id = f"group_{os.urandom(6).hex()}"
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO groups (group_id, title, owner) VALUES (?, ?, ?)", (group_id, title, owner))
    for m in member_list:
        cursor.execute("INSERT INTO group_members (group_id, username) VALUES (?, ?)", (group_id, m))
    conn.commit()
    conn.close()
    
    return {"status": "ok", "group_id": group_id, "title": title, "members": member_list}

@app.get("/groups/my/{username}")
def get_user_groups(username: str):
    conn = sqlite3.connect(DB_FILE)
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
    """Удаление группы, состава её участников и всей истории сообщений чата"""
    conn = sqlite3.connect(DB_FILE)
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

# --- СООБЩЕНИЯ И ИСТОРИЯ ---

@app.get("/messages/{user1}/{user2}")
def get_messages(user1: str, user2: str):
    chat_id = get_chat_id(user1, user2)
    conn = sqlite3.connect(DB_FILE)
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
    conn = sqlite3.connect(DB_FILE)
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
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted"}

# --- WEBSOCKET LOGIC ---

@app.websocket("/ws/{username}")
async def websocket_endpoint(websocket: WebSocket, username: str):
    await manager.connect(username, websocket)
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type", "message")
            
            # 1. ОБРАБОТКА ЛИЧНЫХ И ГРУППОВЫХ СООБЩЕНИЙ
            if msg_type == "message":
                is_group = data.get("is_group", False)
                target = data.get("target")
                content = data.get("content", "")
                content_type = data.get("msg_type", "text")
                
                if not target:
                    continue
                
                chat_id = target if is_group else get_chat_id(username, target)
                
                initial_status = "sent"
                if not is_group and manager.is_online(target):
                    initial_status = "delivered"
                
                conn = sqlite3.connect(DB_FILE)
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
                    "is_group": is_group
                }

                # Отправляем подтверждение и копию сообщения автору
                await manager.send_personal_message(msg_payload, username)

                if is_group:
                    # Рассылка всем остальным участникам группы
                    conn = sqlite3.connect(DB_FILE)
                    cursor = conn.cursor()
                    cursor.execute("SELECT username FROM group_members WHERE group_id = ?", (target,))
                    members = cursor.fetchall()
                    conn.close()
                    for m in members:
                        member_user = m[0]
                        if member_user != username:
                            await manager.send_personal_message(msg_payload, member_user)
                else:
                    # Отправляем сообщение собеседнику (только если это не отправка самому себе)
                    if target != username:
                        await manager.send_personal_message(msg_payload, target)

            # 2. ОТМЕТКА О ПРОЧТЕНИИ (READ RECEIPT)
            elif msg_type == "read_receipt":
                chat_id = data.get("chat_id")
                sender_to_notify = data.get("sender")
                
                conn = sqlite3.connect(DB_FILE)
                cursor = conn.cursor()
                cursor.execute("UPDATE messages SET status = 'read' WHERE chat_id = ? AND sender = ?", (chat_id, sender_to_notify))
                conn.commit()
                conn.close()

                await manager.send_personal_message({
                    "type": "status_update",
                    "chat_id": chat_id,
                    "status": "read"
                }, sender_to_notify)

    except WebSocketDisconnect:
        manager.disconnect(username)
