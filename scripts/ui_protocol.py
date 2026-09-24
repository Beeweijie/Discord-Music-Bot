"""Small acknowledged local protocol for the single Windows tray instance."""

import hashlib
import socket
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
UI_HOST = "127.0.0.1"
UI_PORT = 48731
_PROJECT_ID = hashlib.sha256(str(PROJECT_ROOT).casefold().encode()).hexdigest()[:16]
SHOW_REQUEST = f"discord-music-bot:{_PROJECT_ID}:show".encode("ascii")
SHOW_ACK = b"discord-music-bot:ok"


def notify_existing_ui() -> bool:
    try:
        with socket.create_connection((UI_HOST, UI_PORT), timeout=1) as connection:
            connection.sendall(SHOW_REQUEST)
            connection.shutdown(socket.SHUT_WR)
            return connection.recv(64) == SHOW_ACK
    except OSError:
        return False


def bind_ui_server() -> socket.socket:
    """Reserve the singleton before any GUI or bot process is created."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        server.bind((UI_HOST, UI_PORT))
        server.listen(5)
        server.settimeout(0.5)
        return server
    except Exception:
        server.close()
        raise
