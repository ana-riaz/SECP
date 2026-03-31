"""
auth.py — JWT authentication for the SECP RAG API.

Single-user model: credentials stored in .env.
  AUTH_USERNAME        — login username
  AUTH_PASSWORD_HASH   — bcrypt hash of the password
  JWT_SECRET           — HMAC-SHA256 signing secret
  JWT_EXPIRE_HOURS     — token lifetime (default 8)
"""

from __future__ import annotations
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from passlib.context import CryptContext
from dotenv import load_dotenv

load_dotenv()

_SECRET        = os.getenv("JWT_SECRET", "change-me-in-production")
_ALGORITHM     = "HS256"
_EXPIRE_HOURS  = int(os.getenv("JWT_EXPIRE_HOURS", "8"))
_USERNAME      = os.getenv("AUTH_USERNAME", "admin")
_PWD_HASH      = os.getenv("AUTH_PASSWORD_HASH", "")

_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
_bearer  = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _pwd_ctx.verify(plain, hashed)
    except Exception:
        return False


def create_token(username: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=_EXPIRE_HOURS)
    return jwt.encode(
        {"sub": username, "exp": expire},
        _SECRET,
        algorithm=_ALGORITHM,
    )


def authenticate(username: str, password: str) -> Optional[str]:
    """
    Returns a JWT token if credentials are valid, else None.
    Constant-time comparison to prevent timing attacks.
    """
    import hmac
    username_ok = hmac.compare_digest(username.lower(), _USERNAME.lower())
    password_ok = verify_password(password, _PWD_HASH) if _PWD_HASH else False
    if username_ok and password_ok:
        return create_token(username)
    return None


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------

def require_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> str:
    """
    FastAPI dependency — raises 401 if token is missing or invalid.
    Returns the username on success.
    """
    err = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not credentials:
        raise err
    try:
        payload = jwt.decode(credentials.credentials, _SECRET, algorithms=[_ALGORITHM])
        username: str = payload.get("sub", "")
        if not username:
            raise err
        return username
    except JWTError:
        raise err
