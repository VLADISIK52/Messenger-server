from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
import sqlite3
import json
from typing import Dict, List, Optional

app = FastAPI()

def get_db():
    return sqlite3.connect("messenger.db")

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    # Таблица пользователей
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    """)
    # Таблица чатов (private или group)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            name TEXT
        )
    """)
    # Участники чатов
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_members (
            chat_id INTEGER,
            username TEXT,
            FOREIGN KEY(chat_id) REFERENCES chats(id)
        )
    """)
    # Таблица сообщений с поддержкой медиафайлов (text, voice, circle)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            sender TEXT,
            type TEXT DEFAULT 'text',
            content TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

init_db()

class UserAuth(BaseModel):
    username: str
    password: str

class CreateGroup(BaseModel):
    name: str
    members: List[str]

class CreatePrivateChat(BaseModel):
    target_username: str
    sender_username: str

@app.get("/")
def home():
    return FileResponse("index.html")

# Регистрация и авторизация
@app.post("/register")
def register(user: UserAuth):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO users (username, password) VALUES (?, ?)", (user.username, user.password))
        conn.commit()
        conn.close()
        return {"message": "Регистрация успешна!"}
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Юзернейм уже занят")

@app.post("/login")
def login(user: UserAuth):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE username = ? AND password = ?", (user.username, user.password))
    res = cursor.fetchone()
    conn.close()
    if res:
        return {"message": "Успешно", "username": user.username}
    raise HTTPException(status_code=400, detail="Неверное имя пользователя или пароль")

# Создание ЛС
@app.post("/chats/private")
def create_private_chat(data: CreatePrivateChat):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO chats (type) VALUES ('private')")
    chat_id = cursor.lastrowid
    cursor.execute("INSERT INTO chat_members VALUES (?, ?)", (chat_id, data.sender_username))
    cursor.execute("INSERT INTO chat_members VALUES (?, ?)", (chat_id, data.target_username))
    conn.commit()
    conn.close()
    return {"chat_id": chat_id}

# Создание группы
@app.post("/chats/group")
def create_group_chat(data: CreateGroup):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO chats (type, name) VALUES ('group', ?)", (data.name,))
    chat_id = cursor.lastrowid
    for member in data.members:
        cursor.execute("INSERT INTO chat_members VALUES (?, ?)", (chat_id, member))
    conn.commit()
    conn.close()
    return {"chat_id": chat_id}

# Менеджер WebSocket-соединений
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, username: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[username] = websocket

    def disconnect(self, username: str):
        if username in self.active_connections:
            del self.active_connections[username]

    async def send_to_chat(self, chat_id: int, sender: str, msg_type: str, content: str):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO messages (chat_id, sender, type, content) VALUES (?, ?, ?, ?)",
            (chat_id, sender, msg_type, content)
        )
        conn.commit()
        
        cursor.execute("SELECT username FROM chat_members WHERE chat_id = ?", (chat_id,))
        members = [row[0] for row in cursor.fetchall()]
        conn.close()

        payload = json.dumps({
            "chat_id": chat_id,
            "sender": sender,
            "type": msg_type,
            "content": content
        })

        for member in members:
            if member in self.active_connections:
                await self.active_connections[member].send_text(payload)

manager = ConnectionManager()

@app.websocket("/ws/{username}")
async def websocket_endpoint(websocket: WebSocket, username: str):
    await manager.connect(username, websocket)
    try:
        while True:
            raw_data = await websocket.receive_text()
            data = json.loads(raw_data)
            msg_type = data.get("type", "text")
            await manager.send_to_chat(data["chat_id"], username, msg_type, data["content"])
    except WebSocketDisconnect:
        manager.disconnect(username)

