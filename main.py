import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse


BASE_DIR = Path(__file__).resolve().parent

# На Render постоянные данные будут храниться на подключённом диске /data.
# При локальном запуске используется файл рядом с main.py.
DATA_DIR = Path("/data")

if DATA_DIR.exists():
    DATABASE_PATH = DATA_DIR / "messenger.db"
else:
    DATABASE_PATH = BASE_DIR / "messenger.db"


app = FastAPI(title="Messenger")


def get_connection():
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database():
    with closing(get_connection()) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                last_seen TEXT NOT NULL
            )
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

        connection.commit()


@app.on_event("startup")
async def startup_event():
    initialize_database()
    print(f"Database path: {DATABASE_PATH}")


@app.get("/")
async def homepage():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/health")
async def health_check():
    return {"status": "ok"}


@app.get("/users")
async def get_users():
    with closing(get_connection()) as connection:
        rows = connection.execute(
            """
            SELECT username, last_seen
            FROM users
            ORDER BY username
            """
        ).fetchall()

    return [dict(row) for row in rows]


@app.get("/messages")
async def get_messages():
    with closing(get_connection()) as connection:
        rows = connection.execute(
            """
            SELECT id, username, text, created_at
            FROM messages
            ORDER BY id ASC
            LIMIT 200
            """
        ).fetchall()

    return [dict(row) for row in rows]


class ConnectionManager:
    def __init__(self):
        self.connections: Dict[WebSocket, str] = {}

    async def connect(self, websocket: WebSocket, username: str):
        await websocket.accept()
        self.connections[websocket] = username

    def disconnect(self, websocket: WebSocket):
        self.connections.pop(websocket, None)

    async def broadcast(self, message: dict):
        disconnected_connections = []

        for websocket in list(self.connections.keys()):
            try:
                await websocket.send_json(message)
            except Exception as error:
                print(f"Broadcast error: {error}")
                disconnected_connections.append(websocket)

        for websocket in disconnected_connections:
            self.disconnect(websocket)


manager = ConnectionManager()


def update_user(username: str):
    current_time = datetime.now(timezone.utc).isoformat()

    with closing(get_connection()) as connection:
        connection.execute(
            """
            INSERT INTO users (username, last_seen)
            VALUES (?, ?)
            ON CONFLICT(username)
            DO UPDATE SET last_seen = excluded.last_seen
            """,
            (username, current_time),
        )
        connection.commit()


def create_message(username: str, text: str):
    current_time = datetime.now(timezone.utc).isoformat()

    with closing(get_connection()) as connection:
        cursor = connection.execute(
            """
            INSERT INTO messages (username, text, created_at)
            VALUES (?, ?, ?)
            """,
            (username, text, current_time),
        )
        connection.commit()

    return {
        "type": "message",
        "id": cursor.lastrowid,
        "username": username,
        "text": text,
        "created_at": current_time,
    }


# Именно этот WebSocket-маршрут использует index.html.
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    username = websocket.query_params.get("username", "").strip()

    if not username:
        await websocket.close(
            code=1008,
            reason="Username is required"
        )
        return

    # Ограничиваем длину имени пользователя.
    username = username[:30]

    print(f"WebSocket handshake received: username={username}")

    # Критически важная строка:
    await websocket.accept()

    manager.connections[websocket] = username
    update_user(username)

    print(f"WebSocket accepted: username={username}")

    await manager.broadcast(
        {
            "type": "system",
            "text": f"{username} подключился к чату",
        }
    )

    try:
        while True:
            raw_data = await websocket.receive_text()

            try:
                data = json.loads(raw_data)
            except json.JSONDecodeError:
                print("Invalid JSON received")
                continue

            message_type = data.get("type")

            # Прикладной heartbeat от браузера.
            if message_type == "ping":
                await websocket.send_json(
                    {
                        "type": "pong"
                    }
                )
                continue

            if message_type != "message":
                continue

            text = str(data.get("text", "")).strip()

            if not text:
                continue

            # Ограничиваем длину сообщения.
            text = text[:2000]

            message = create_message(username, text)

            await manager.broadcast(message)

    except WebSocketDisconnect:
        manager.disconnect(websocket)

        print(f"WebSocket disconnected: username={username}")

        await manager.broadcast(
            {
                "type": "system",
                "text": f"{username} вышел из чата",
            }
        )

    except Exception as error:
        manager.disconnect(websocket)
        print(f"WebSocket error for {username}: {error}")
