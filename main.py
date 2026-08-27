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
    
    # Таблица пользователей
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            avatar_url TEXT
        )
    ''')
    
    # Таблица сообщений
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
    
    # Таблица реакций (эмодзи)
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

# --- МЕНЕДЖЕР WEBSOCKET СЕДНЕНИЙ ---
class ConnectionManager:
    def __init__(self):
        # Храним активные соединения: {username: WebSocket}
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, username: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[username] = websocket

    def disconnect(self, username: str):
        if username in self.active_connections:
            del self.active_connections[username]

    async def send_personal_message(self, message: dict, websocket: WebSocket):
        await websocket.send_json(message)

    async def broadcast(self, data: dict):
        for connection in self.active_connections.values():
            try:
                await connection.send_json(data)
            except Exception:
                pass

manager = ConnectionManager()

# --- ЭНДПОИНТЫ API ---

@app.get("/")
def get_index():
    return FileResponse("index.html")

# Регистрация и загрузка аватарки
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

# Загрузка медиафайлов (голосовые, кружочки, картинки)
@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    file_path = os.path.join(UPLOAD_DIR, file.filename)
    with open(file_path, "wb") as f:
        f.write(await file.read())
    return {"file_url": f"/uploads/{file.filename}"}

# Получение истории сообщений чата
@app.get("/messages/{chat_id}")
def get_messages(chat_id: str):
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
            "type": r[4],
            "timestamp": r[5]
        })
    return messages

# Добавление реакции на сообщение
@app.post("/messages/{msg_id}/react")
def add_reaction(msg_id: int, username: str = Form(...), emoji: str = Form(...)):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("REPLACE INTO reactions (message_id, username, emoji) VALUES (?, ?, ?)", 
                   (msg_id, username, emoji))
    conn.commit()
    conn.close()
    return {"status": "ok"}

# --- WEBSOCKET ЧАТА ---

@app.websocket("/ws/{username}")
async def websocket_endpoint(websocket: WebSocket, username: str):
    await manager.connect(username, websocket)
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type", "message")
            
            # 1. Обработка обычной отправки сообщений / голосовых / файлов
            if msg_type == "message":
                chat_id = data.get("chat_id", "global")
                content = data.get("content", "")
                content_type = data.get("msg_type", "text")
                
                # Сохраняем в базу данных
                conn = sqlite3.connect(DB_FILE)
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO messages (chat_id, sender, content, msg_type) VALUES (?, ?, ?, ?)",
                    (chat_id, username, content, content_type)
                )
                conn.commit()
                msg_id = cursor.lastrowid
                conn.close()
                
                # Рассылаем всем подключенным пользователям
                broadcast_data = {
                    "type": "message",
                    "id": msg_id,
                    "chat_id": chat_id,
                    "sender": username,
                    "content": content,
                    "msg_type": content_type
                }
                await manager.broadcast(broadcast_data)
                
            # 2. Обработка статусов "печатает..." / "перестал печатать"
            elif msg_type in ["typing", "stop_typing"]:
                await manager.broadcast({
                    "type": msg_type,
                    "sender": username,
                    "chat_id": data.get("chat_id", "global")
                })
                
    except WebSocketDisconnect:
        manager.disconnect(username)
