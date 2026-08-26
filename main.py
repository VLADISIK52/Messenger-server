from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
import sqlite3
import json
from typing import Dict, List

app = FastAPI()

def get_db():
    return sqlite3.connect("messenger.db")

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS friends (
            user1 TEXT,
            user2 TEXT,
            PRIMARY KEY (user1, user2)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            name TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_members (
            chat_id INTEGER,
            username TEXT,
            FOREIGN KEY(chat_id) REFERENCES chats(id)
        )
    """)
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

class AddFriend(BaseModel):
    username: str
    friend_username: str

class CreateGroup(BaseModel):
    name: str
    members: List[str]

class CreatePrivateChat(BaseModel):
    target_username: str
    sender_username: str

@app.get("/")
def home():
    return FileResponse("index.html")

@app.post("/register")
def register(user: UserAuth):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO users (username, password) VALUES (?, ?)", (user.username.lower(), user.password))
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
    cursor.execute("SELECT * FROM users WHERE username = ? AND password = ?", (user.username.lower(), user.password))
    res = cursor.fetchone()
    conn.close()
    if res:
        return {"message": "Успешно", "username": user.username.lower()}
    raise HTTPException(status_code=400, detail="Неверные данные")

@app.post("/friends/add")
def add_friend(data: AddFriend):
    conn = get_db()
    cursor = conn.cursor()
    u1, u2 = data.username.lower(), data.friend_username.lower()
    cursor.execute("SELECT * FROM users WHERE username = ?", (u2,))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    
    try:
        cursor.execute("INSERT INTO friends VALUES (?, ?)", (u1, u2))
        cursor.execute("INSERT INTO friends VALUES (?, ?)", (u2, u1))
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()
    return {"message": "Друг добавлен!"}

@app.get("/friends/{username}")
def get_friends(username: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT user2 FROM friends WHERE user1 = ?", (username.lower(),))
    friends = [row[0] for row in cursor.fetchall()]
    conn.close()
    return friends

@app.get("/chats/{username}")
def get_user_chats(username: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT c.id, c.type, c.name 
        FROM chats c
        JOIN chat_members cm ON c.id = cm.chat_id
        WHERE cm.username = ?
    """, (username.lower(),))
    rows = cursor.fetchall()
    
    chats = []
    for row in rows:
        chat_id, chat_type, chat_name = row
        if chat_type == 'private':
            cursor.execute("SELECT username FROM chat_members WHERE chat_id = ? AND username != ?", (chat_id, username.lower()))
            other_user = cursor.fetchone()
            display_name = f"👤 @{other_user[0]}" if other_user else "Личные сообщения"
        else:
            display_name = f"👥 {chat_name}"
        
        chats.append({"id": chat_id, "type": chat_type, "name": display_name})
    
    conn.close()
    return chats

@app.delete("/chats/{chat_id}")
def delete_chat(chat_id: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM chat_members WHERE chat_id = ?", (chat_id,))
    cursor.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
    cursor.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
    conn.commit()
    conn.close()
    return {"message": "Чат удален"}

@app.get("/messages/{chat_id}")
def get_chat_messages(chat_id: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT sender, type, content, timestamp FROM messages WHERE chat_id = ? ORDER BY id ASC", (chat_id,))
    rows = cursor.fetchall()
    conn.close()
    return [{"sender": r[0], "type": r[1], "content": r[2], "timestamp": r[3]} for r in rows]

@app.post("/chats/private")
def create_private_chat(data: CreatePrivateChat):
    conn = get_db()
    cursor = conn.cursor()
    u1, u2 = data.sender_username.lower(), data.target_username.lower()
    cursor.execute("""
        SELECT cm1.chat_id FROM chat_members cm1
        JOIN chat_members cm2 ON cm1.chat_id = cm2.chat_id
        JOIN chats c ON c.id = cm1.chat_id
        WHERE cm1.username = ? AND cm2.username = ? AND c.type = 'private'
    """, (u1, u2))
    existing = cursor.fetchone()
    if existing:
        conn.close()
        return {"chat_id": existing[0]}

    cursor.execute("INSERT INTO chats (type) VALUES ('private')")
    chat_id = cursor.lastrowid
    cursor.execute("INSERT INTO chat_members VALUES (?, ?)", (chat_id, u1))
    cursor.execute("INSERT INTO chat_members VALUES (?, ?)", (chat_id, u2))
    conn.commit()
    conn.close()
    return {"chat_id": chat_id}

@app.post("/chats/group")
def create_group_chat(data: CreateGroup):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO chats (type, name) VALUES ('group', ?)", (data.name,))
    chat_id = cursor.lastrowid
    for member in set([m.lower() for m in data.members]):
        cursor.execute("INSERT INTO chat_members VALUES (?, ?)", (chat_id, member))
    conn.commit()
    conn.close()
    return {"chat_id": chat_id}

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, username: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[username.lower()] = websocket

    def disconnect(self, username: str):
        u = username.lower()
        if u in self.active_connections:
            del self.active_connections[u]

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
                try:
                    await self.active_connections[member].send_text(payload)
                except:
                    pass

manager = ConnectionManager()

@app.websocket("/ws/{username}")
async def websocket_endpoint(websocket: WebSocket, username: str):
    await manager.connect(username, websocket)
    try:
        while True:
            raw_data = await websocket.receive_text()
            data = json.loads(raw_data)
            msg_type = data.get("type", "text")
            await manager.send_to_chat(data["chat_id"], username.lower(), msg_type, data["content"])
    except WebSocketDisconnect:
        manager.disconnect(username)




