"""Open the local web dashboard directly."""
import os
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
from bot_app.infrastructure.paths import BASE_DIR


def main():
    load_dotenv(BASE_DIR / ".env")
    host = os.getenv("MANAGEMENT_API_HOST", "127.0.0.1")
    if host == "0.0.0.0":
        host = "127.0.0.1"
    port = int(os.getenv("MANAGEMENT_API_PORT", "8766"))
    url = f"http://{host}:{port}"
    webbrowser.open(url)


if __name__ == "__main__":
    main()
