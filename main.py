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
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            status_text TEXT DEFAULT 'В ангаре'
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
            is_read INTEGER DEFAULT 0,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

init_db()

class UserAuth(BaseModel):
    username: str
    password: str

class ProfileUpdate(BaseModel):
    username: str
    new_username: str
    status_text: Optional[str] = 'В ангаре'

class AddFriend(BaseModel):
    username: str
    friend_username: str

class CreateGroup(BaseModel):
    name: str
    members: List[str]

class CreatePrivateChat(BaseModel):
    target_username: str
    sender_username: str

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

    def is_online(self, username: str) -> bool:
        return username.lower() in self.active_connections

    async def send_to_chat(self, chat_id: int, sender: str, msg_type: str, content: str):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO messages (chat_id, sender, type, content, is_read) VALUES (?, ?, ?, ?, 0)",
            (chat_id, sender, msg_type, content)
        )
        msg_id = cursor.lastrowid
        conn.commit()
        
        cursor.execute("SELECT username FROM chat_members WHERE chat_id = ?", (chat_id,))
        members = [row[0] for row in cursor.fetchall()]
        conn.close()

        payload = json.dumps({
            "id": msg_id,
            "chat_id": chat_id,
            "sender": sender,
            "type": msg_type,
            "content": content,
            "is_read": 0
        })

        for member in members:
            if member in self.active_connections:
                try:
                    await self.active_connections[member].send_text(payload)
                except:
                    pass

manager = ConnectionManager()

@app.get("/")
def home():
    return FileResponse("index.html")

@app.post("/register")
def register(user: UserAuth):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO users (username, password, status_text) VALUES (?, ?, ?)", (user.username.lower(), user.password, "В ангаре"))
        conn.commit()
        conn.close()
        return {"message": "Регистрация успешна!"}
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Позывной уже занят")

@app.post("/login")
def login(user: UserAuth):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT username, status_text FROM users WHERE username = ? AND password = ?", (user.username.lower(), user.password))
    res = cursor.fetchone()
    conn.close()
    if res:
        return {"message": "Успешно", "username": res[0], "status_text": res[1]}
    raise HTTPException(status_code=400, detail="Неверные данные")

@app.get("/user/profile/{username}")
def get_profile(username: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT username, status_text FROM users WHERE username = ?", (username.lower(),))
    res = cursor.fetchone()
    conn.close()
    if res:
        return {"username": res[0], "status_text": res[1], "online": manager.is_online(res[0])}
    raise HTTPException(status_code=404, detail="Пользователь не найден")

@app.post("/user/profile/update")
def update_profile(data: ProfileUpdate):
    conn = get_db()
    cursor = conn.cursor()
    old_u = data.username.lower()
    new_u = data.new_username.lower()
    
    if old_u != new_u:
        cursor.execute("SELECT id FROM users WHERE username = ?", (new_u,))
        if cursor.fetchone():
            conn.close()
            raise HTTPException(status_code=400, detail="Новый позывной занят")
        
        cursor.execute("UPDATE users SET username = ? WHERE username = ?", (new_u, old_u))
        cursor.execute("UPDATE friends SET user1 = ? WHERE user1 = ?", (new_u, old_u))
        cursor.execute("UPDATE friends SET user2 = ? WHERE user2 = ?", (new_u, old_u))
        cursor.execute("UPDATE chat_members SET username = ? WHERE username = ?", (new_u, old_u))
        cursor.execute("UPDATE messages SET sender = ? WHERE sender = ?", (new_u, old_u))

    cursor.execute("UPDATE users SET status_text = ? WHERE username = ?", (data.status_text, new_u))
    conn.commit()
    conn.close()
    return {"message": "Профиль обновлен", "new_username": new_u, "status_text": data.status_text}

@app.post("/friends/add")
def add_friend(data: AddFriend):
    conn = get_db()
    cursor = conn.cursor()
    u1, u2 = data.username.lower(), data.friend_username.lower()
    cursor.execute("SELECT * FROM users WHERE username = ?", (u2,))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Боец не найден")
    
    try:
        cursor.execute("INSERT INTO friends VALUES (?, ?)", (u1, u2))
        cursor.execute("INSERT INTO friends VALUES (?, ?)", (u2, u1))
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()
    return {"message": "Союзник добавлен!"}

@app.get("/friends/{username}")
def get_friends(username: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT user2 FROM friends WHERE user1 = ?", (username.lower(),))
    rows = cursor.fetchall()
    
    friends = []
    for r in rows:
        f_name = r[0]
        cursor.execute("SELECT status_text FROM users WHERE username = ?", (f_name,))
        st = cursor.fetchone()
        friends.append({
            "username": f_name,
            "status_text": st[0] if st else "В ангаре",
            "online": manager.is_online(f_name)
        })
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
        is_online = False
        if chat_type == 'private':
            cursor.execute("SELECT username FROM chat_members WHERE chat_id = ? AND username != ?", (chat_id, username.lower()))
            other_user = cursor.fetchone()
            if other_user:
                display_name = f"👤 @{other_user[0]}"
                is_online = manager.is_online(other_user[0])
            else:
                display_name = "Связь"
        else:
            display_name = f"👥 {chat_name}"
        
        chats.append({"id": chat_id, "type": chat_type, "name": display_name, "online": is_online})
    
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
    return {"message": "Канал уничтожен"}

@app.get("/messages/{chat_id}/{username}")
def get_chat_messages(chat_id: int, username: str):
    conn = get_db()
    cursor = conn.cursor()
    
    # Отмечаем чужие сообщения как прочитанные
    cursor.execute("UPDATE messages SET is_read = 1 WHERE chat_id = ? AND sender != ?", (chat_id, username.lower()))
    conn.commit()
    
    cursor.execute("SELECT id, sender, type, content, is_read, timestamp FROM messages WHERE chat_id = ? ORDER BY id ASC", (chat_id,))
    rows = cursor.fetchall()
    conn.close()
    return [{"id": r[0], "sender": r[1], "type": r[2], "content": r[3], "is_read": r[4], "timestamp": r[5]} for r in rows]

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






