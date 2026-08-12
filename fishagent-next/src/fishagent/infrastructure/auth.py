import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Dict, Optional

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError


@dataclass
class AuthSession:
    token: str
    csrf_token: str
    username: str
    role: str
    expires_at: float


class AuthManager:
    def __init__(
        self,
        enabled: bool = False,
        username: str = "admin",
        password: str = "",
        users: Optional[dict[str, dict[str, str]]] = None,
        cookie_secure: bool = False,
    ) -> None:
        self.enabled = enabled
        self.username = username
        self.cookie_secure = cookie_secure
        self._password_hasher = PasswordHasher()
        self._users: dict[str, tuple[str, str]] = {}
        if password:
            self._users[username] = (self._password_hasher.hash(password), "admin")
        for name, user in (users or {}).items():
            user_password = str(user.get("password") or "")
            if user_password:
                self._users[name] = (
                    self._password_hasher.hash(user_password),
                    str(user.get("role") or "viewer").lower(),
                )
        self.sessions: Dict[str, AuthSession] = {}

    def _verify_password(self, password: str, password_hash: str) -> bool:
        try:
            return self._password_hasher.verify(password_hash, password)
        except (InvalidHashError, VerificationError, VerifyMismatchError):
            return False

    def login(self, username: str, password: str, ttl_seconds: int = 28_800) -> Optional[AuthSession]:
        user = self._users.get(username)
        if not self.enabled or user is None or not self._verify_password(password, user[0]):
            return None
        session = AuthSession(
            token=secrets.token_urlsafe(32),
            csrf_token=secrets.token_urlsafe(24),
            username=username,
            role=user[1],
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
    users: dict[str, dict[str, str]] = {}
    raw_users = os.environ.get("FISHAGENT_USERS_JSON", "")
    if raw_users:
        try:
            decoded = json.loads(raw_users)
            if isinstance(decoded, dict):
                users = {
                    str(name): value
                    for name, value in decoded.items()
                    if isinstance(value, dict)
                }
        except json.JSONDecodeError:
            pass
    return AuthManager(
        enabled=enabled,
        username=username,
        password=password,
        users=users,
        cookie_secure=os.environ.get("FISHAGENT_AUTH_COOKIE_SECURE", "false").lower() in {"1", "true", "yes", "on"},
    )
