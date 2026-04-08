import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from jose import JWTError, jwt
from pymongo.errors import PyMongoError

from src.utils.db import db
from src.config import env


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AuthTokens:
    """JWT session tokens with optional MongoDB revocation.

    Environment:
    - JARVIS_JWT_SECRET: required for issuing/verifying tokens
    - JARVIS_JWT_TTL_SECONDS: default 28800 (8h)
    - JARVIS_JWT_ISSUER: default "jarvis"
    - AUTH_TOKEN_USE_DB_REVOCATION: default true (if DB connected)
    """

    def __init__(self):
        self.secret = env.get_str("JARVIS_JWT_SECRET", "")
        self.issuer = env.get_str("JARVIS_JWT_ISSUER", "jarvis")
        self.ttl_seconds = 28800
        self.use_db_revocation = True
        self._revocation_indexes_ready = False

    def _ensure_revocation_indexes(self, col) -> None:
        if self._revocation_indexes_ready:
            return
        col.create_index([("jti", 1)], unique=True)
        col.create_index([("exp", 1)])
        self._revocation_indexes_ready = True

    def issue(self, username: str, role: str = "user") -> str:
        if not self.secret:
            raise RuntimeError("JARVIS_JWT_SECRET is not set")

        now = _utcnow()
        exp = now + timedelta(seconds=self.ttl_seconds)
        jti = secrets.token_urlsafe(16)

        payload = {
            "iss": self.issuer,
            "sub": username,
            "role": role,
            "jti": jti,
            "iat": int(now.timestamp()),
            "exp": int(exp.timestamp()),
        }

        return jwt.encode(payload, self.secret, algorithm="HS256")

    def verify(self, token: str) -> Tuple[bool, Optional[str], Optional[dict]]:
        if not token:
            return False, None, None
        if not self.secret:
            return False, None, None

        try:
            payload = jwt.decode(token, self.secret, algorithms=["HS256"], options={"require_exp": True})

            if payload.get("iss") != self.issuer:
                return False, None, None

            # Optional revocation check
            if self.use_db_revocation:
                try:
                    # Do not force reconnection here; verification is on the hot request path.
                    # If DB is unavailable, skip revocation checks and keep tokens usable.
                    if db.db is not None:
                        col = db.db.auth_revoked_tokens
                        self._ensure_revocation_indexes(col)
                        jti = payload.get("jti")
                        if jti and col.find_one({"jti": jti}):
                            return False, None, None
                except (PyMongoError, OSError):
                    # If DB is down, treat tokens as valid (degrades gracefully).
                    pass

            return True, payload.get("sub"), payload

        except JWTError:
            return False, None, None
        except Exception:
            return False, None, None

    def revoke(self, token: str) -> bool:
        ok, _, payload = self.verify(token)
        if not ok or not payload:
            return False

        if not self.use_db_revocation:
            return True

        try:
            db._ensure_connected()
            if db.db is None:
                return False
            col = db.db.auth_revoked_tokens
            self._ensure_revocation_indexes(col)
            col.update_one(
                {"jti": payload.get("jti")},
                {"$set": {"jti": payload.get("jti"), "exp": payload.get("exp"), "revoked_at": datetime.now(timezone.utc)}},
                upsert=True,
            )
            return True
        except (PyMongoError, OSError, ValueError):
            return False
