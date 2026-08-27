import os
import shutil
import sqlite3
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Dict

app = FastAPI()

# Создаем папку для хранения аватарок и медиа
UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# --- БАЗА ДАННЫХ (SQLite) ---
DB_FILE = "chat.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    # Таблица пользователей
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password TEXT NOT NULL,
            avatar_url TEXT DEFAULT ''
        )
    ''')
    # Таблица чатов
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            type TEXT
        )
    ''')
    # Участники чатов
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS chat_members (
            chat_id INTEGER,
            username TEXT
        )
    ''')
    # Сообщения
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            sender TEXT,
            type TEXT,
            content TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Друзья
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS friends (
            user_a TEXT,
            user_b TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# Менеджер WebSocket соединений
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, username: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[username] = websocket

    def disconnect(self, username: str):
        if username in self.active_connections:
            del self.active_connections[username]

    async def broadcast_to_chat(self, chat_id: int, message_data: dict):
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT username FROM chat_members WHERE chat_id = ?", (chat_id,))
        members = [row[0] for row in cursor.fetchall()]
        conn.close()

        for member in members:
            if member in self.active_connections:
                await self.active_connections[member].send_json(message_data)

manager = ConnectionManager()

# --- МОДЕЛИ ---
class AuthModel(BaseModel):
    username: str
    password: str

class PrivateChatModel(BaseModel):
    sender_username: str
    target_username: str

class GroupChatModel(BaseModel):
    name: str
    members: List[str]

class FriendModel(BaseModel):
    username: str
    friend_username: str

# --- API ---

@app.get("/")
def get_index():
    return FileResponse("index.html")

@app.post("/register")
def register(data: AuthModel):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT username FROM users WHERE username = ?", (data.username,))
    if cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Пользователь уже существует")
    cursor.execute("INSERT INTO users (username, password) VALUES (?, ?)", (data.username, data.password))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.post("/login")
def login(data: AuthModel):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT username, avatar_url FROM users WHERE username = ? AND password = ?", (data.username, data.password))
    user = cursor.fetchone()
    conn.close()
    if not user:
        raise HTTPException(status_code=400, detail="Неверный логин или пароль")
    return {"status": "ok", "username": user[0], "avatar_url": user[1]}

@app.get("/user/{username}")
def get_user(username: str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT username, avatar_url FROM users WHERE username = ?", (username,))
    user = cursor.fetchone()
    conn.close()
    if not user:
        raise HTTPException(status_code=404, detail="Не найдено")
    return {"username": user[0], "avatar_url": user[1]}

@app.post("/avatar/upload")
async def upload_avatar(username: str = Form(...), file: UploadFile = File(...)):
    extension = file.filename.split(".")[-1]
    filename = f"avatar_{username}.{extension}"
    file_path = os.path.join(UPLOAD_DIR, filename)
    
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    avatar_url = f"/uploads/{filename}"
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET avatar_url = ? WHERE username = ?", (avatar_url, username))
    conn.commit()
    conn.close()
    
    return {"avatar_url": avatar_url}

@app.get("/chats/{username}")
def get_chats(username: str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT c.id, c.name, c.type 
        FROM chats c
        JOIN chat_members cm ON c.id = cm.chat_id
        WHERE cm.username = ?
    ''', (username,))
    chats = cursor.fetchall()
    
    result = []
    for c_id, name, c_type in chats:
        display_name = name
        avatar_url = ""
        is_online = False
        
        if c_type == 'private':
            cursor.execute("SELECT username FROM chat_members WHERE chat_id = ? AND username != ?", (c_id, username))
            other = cursor.fetchone()
            if other:
                other_user = other[0]
                display_name = f"@{other_user}"
                is_online = other_user in manager.active_connections
                cursor.execute("SELECT avatar_url FROM users WHERE username = ?", (other_user,))
                av = cursor.fetchone()
                avatar_url = av[0] if av else ""

        result.append({
            "id": c_id,
            "name": display_name,
            "type": c_type,
            "online": is_online,
            "avatar_url": avatar_url
        })
    conn.close()
    return result

@app.post("/chats/private")
def create_private_chat(data: PrivateChatModel):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Проверка существования чата
    cursor.execute('''
        SELECT c.id FROM chats c
        JOIN chat_members cm1 ON c.id = cm1.chat_id
        JOIN chat_members cm2 ON c.id = cm2.chat_id
        WHERE c.type = 'private' AND cm1.username = ? AND cm2.username = ?
    ''', (data.sender_username, data.target_username))
    existing = cursor.fetchone()
    if existing:
        conn.close()
        return {"chat_id": existing[0]}
    
    cursor.execute("INSERT INTO chats (name, type) VALUES ('', 'private')")
    chat_id = cursor.lastrowid
    cursor.execute("INSERT INTO chat_members (chat_id, username) VALUES (?, ?)", (chat_id, data.sender_username))
    cursor.execute("INSERT INTO chat_members (chat_id, username) VALUES (?, ?)", (chat_id, data.target_username))
    conn.commit()
    conn.close()
    return {"chat_id": chat_id}

@app.post("/chats/group")
def create_group_chat(data: GroupChatModel):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO chats (name, type) VALUES (?, 'group')", (data.name,))
    chat_id = cursor.lastrowid
    for member in set(data.members):
        cursor.execute("INSERT INTO chat_members (chat_id, username) VALUES (?, ?)", (chat_id, member))
    conn.commit()
    conn.close()
    return {"chat_id": chat_id}

@app.delete("/chats/{chat_id}")
def delete_chat(chat_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
    cursor.execute("DELETE FROM chat_members WHERE chat_id = ?", (chat_id,))
    cursor.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted"}

@app.get("/messages/{chat_id}/{username}")
def get_messages(chat_id: int, username: str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT m.sender, m.type, m.content, u.avatar_url 
        FROM messages m
        LEFT JOIN users u ON m.sender = u.username
        WHERE m.chat_id = ? ORDER BY m.id ASC
    ''', (chat_id,))
    msgs = cursor.fetchall()
    conn.close()
    return [{"sender": m[0], "type": m[1], "content": m[2], "avatar_url": m[3]} for m in msgs]

@app.get("/friends/{username}")
def get_friends(username: str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT user_b FROM friends WHERE user_a = ?", (username,))
    friends_a = cursor.fetchall()
    conn.close()
    
    result = []
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    for f in friends_a:
        f_user = f[0]
        cursor.execute("SELECT avatar_url FROM users WHERE username = ?", (f_user,))
        av = cursor.fetchone()
        result.append({
            "username": f_user,
            "online": f_user in manager.active_connections,
            "avatar_url": av[0] if av else ""
        })
    conn.close()
    return result

@app.post("/friends/add")
def add_friend(data: FriendModel):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT username FROM users WHERE username = ?", (data.friend_username,))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    
    cursor.execute("INSERT INTO friends (user_a, user_b) VALUES (?, ?)", (data.username, data.friend_username))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.websocket("/ws/{username}")
async def websocket_endpoint(websocket: WebSocket, username: str):
    await manager.connect(username, websocket)
    try:
        while True:
            data = await websocket.receive_json()
            chat_id = data.get("chat_id")
            msg_type = data.get("type")
            content = data.get("content")

            # Сохранение сообщения в БД
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("INSERT INTO messages (chat_id, sender, type, content) VALUES (?, ?, ?, ?)",
                           (chat_id, username, msg_type, content))
            conn.commit()
            
            # Получение аватарки
            cursor.execute("SELECT avatar_url FROM users WHERE username = ?", (username,))
            av = cursor.fetchone()
            avatar_url = av[0] if av else ""
            conn.close()

            payload = {
                "chat_id": chat_id,
                "sender": username,
                "type": msg_type,
                "content": content,
                "avatar_url": avatar_url
            }
            await manager.broadcast_to_chat(chat_id, payload)
    except WebSocketDisconnect:
        manager.disconnect(username)
