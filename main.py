import os
import sqlite3
from typing import Dict, List
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

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
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT,
            sender TEXT,
            content TEXT,
            msg_type TEXT DEFAULT 'text',
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS reactions (
            message_id INTEGER,
            username TEXT,
            emoji TEXT,
            PRIMARY KEY (message_id, username)
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# Утилита для создания уникального ID чата между двумя людьми
def get_chat_id(user1: str, user2: str) -> str:
    # Сортируем имена по алфавиту, чтобы ID всегда был одинаковым независимо от того, кто пишет
    return "_".join(sorted([user1, user2]))

# --- МЕНЕДЖЕР WEBSOCKET СЕДНЕНИЙ ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, username: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[username] = websocket

    def disconnect(self, username: str):
        if username in self.active_connections:
            del self.active_connections[username]

    async def send_personal_message(self, message: dict, target_user: str):
        if target_user in self.active_connections:
            try:
                await self.active_connections[target_user].send_json(message)
            except Exception:
                pass

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

# Получение списка пользователей онлайн
@app.get("/users")
def get_online_users():
    return list(manager.active_connections.keys())

# Получение истории личной переписки
@app.get("/messages/{user1}/{user2}")
def get_messages(user1: str, user2: str):
    chat_id = get_chat_id(user1, user2)
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, chat_id, sender, content, msg_type, timestamp FROM messages WHERE chat_id = ? ORDER BY timestamp ASC",
        (chat_id,)
    )
    rows = cursor.fetchall()
    conn.close()
    
    messages = []
    for r in rows:
        messages.append({
            "id": r[0],
            "chat_id": r[1],
            "sender": r[2],
            "content": r[3],
            "msg_type": r[4],  # исправлено с type на msg_type для фронтенда
            "timestamp": r[5]
        })
    return messages

# Удаление чата
@app.delete("/messages/{user1}/{user2}")
def delete_chat(user1: str, user2: str):
    chat_id = get_chat_id(user1, user2)
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted"}

# --- WEBSOCKET ЧАТА ---

@app.websocket("/ws/{username}")
async def websocket_endpoint(websocket: WebSocket, username: str):
    await manager.connect(username, websocket)
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type", "message")
            target = data.get("target")
            
            if msg_type == "message" and target:
                chat_id = get_chat_id(username, target)
                content = data.get("content", "")
                content_type = data.get("msg_type", "text")
                
                # Сохраняем в БД
                conn = sqlite3.connect(DB_FILE)
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO messages (chat_id, sender, content, msg_type) VALUES (?, ?, ?, ?)",
                    (chat_id, username, content, content_type)
                )
                conn.commit()
                msg_id = cursor.lastrowid
                conn.close()
                
                msg_data = {
                    "type": "message",
                    "id": msg_id,
                    "target": target,
                    "sender": username,
                    "content": content,
                    "msg_type": content_type
                }
                
                # Отправляем сообщение ТОЛЬКО получателю
                await manager.send_personal_message(msg_data, target)
                
            elif msg_type in ["typing", "stop_typing"] and target:
                # Статус печатает - тоже отправляем только собеседнику
                await manager.send_personal_message({
                    "type": msg_type,
                    "sender": username
                }, target)
                
    except WebSocketDisconnect:
        manager.disconnect(username)
