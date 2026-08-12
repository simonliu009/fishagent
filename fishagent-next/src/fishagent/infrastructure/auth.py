import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class AuthSession:
    token: str
    csrf_token: str
    username: str
    role: str
    expires_at: float


class AuthManager:
    def __init__(self, enabled: bool = False, username: str = "admin", password: str = "") -> None:
        self.enabled = enabled
        self.username = username
        self._password_hash = self._hash_password(password) if password else ""
        self.sessions: Dict[str, AuthSession] = {}

    @staticmethod
    def _hash_password(password: str, salt: Optional[bytes] = None) -> str:
        salt = salt or secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 260_000)
        return "%s$%s" % (salt.hex(), digest.hex())

    def _verify_password(self, password: str) -> bool:
        if not self._password_hash:
            return False
        salt_hex, digest_hex = self._password_hash.split("$", 1)
        calculated = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 260_000)
        return hmac.compare_digest(calculated.hex(), digest_hex)

    def login(self, username: str, password: str, ttl_seconds: int = 28_800) -> Optional[AuthSession]:
        if not self.enabled or username != self.username or not self._verify_password(password):
            return None
        session = AuthSession(
            token=secrets.token_urlsafe(32),
            csrf_token=secrets.token_urlsafe(24),
            username=username,
            role="admin",
            expires_at=time.time() + ttl_seconds,
        )
        self.sessions[session.token] = session
        return session

    def authenticate(self, cookie_header: str) -> Optional[AuthSession]:
        if not self.enabled:
            return AuthSession(token="disabled", csrf_token="disabled", username="local", role="admin", expires_at=0)
        token = ""
        for part in cookie_header.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "fishagent_session":
                token = value
                break
        session = self.sessions.get(token)
        if not session or session.expires_at <= time.time():
            if token:
                self.sessions.pop(token, None)
            return None
        return session

    def logout(self, cookie_header: str) -> None:
        session = self.authenticate(cookie_header)
        if session and session.token != "disabled":
            self.sessions.pop(session.token, None)


def auth_from_config(enabled: bool, username: str, password: str) -> AuthManager:
    return AuthManager(enabled=enabled, username=username, password=password)
