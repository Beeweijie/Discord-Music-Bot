"""Local management credential provisioning; never print or return the secret."""
import os
import secrets
from bot_app.infrastructure.paths import BASE_DIR


def management_token():
    configured = os.getenv("MANAGEMENT_API_TOKEN", "").strip()
    if configured:
        if len(configured) < 32:
            raise ValueError("MANAGEMENT_API_TOKEN 必须至少有 32 个字符")
        return configured
    path = BASE_DIR / "runtime" / "admin_token"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as stream:
            os.chmod(path, 0o600)
            token = secrets.token_urlsafe(40)
            stream.write(token)
    except FileExistsError:
        token = path.read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise ValueError("本机管理密钥无效，请检查 runtime/admin_token")
    return token
