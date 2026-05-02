"""
SocialProof — Router: Authentication
Endpoints:
  POST /auth/register      — create account
  POST /auth/login         — returns JWT token (Bearer)
  POST /auth/cookie-login  — returns JWT token via HttpOnly cookie (C-8)
  POST /auth/cookie-logout — clears the HttpOnly cookie (C-8)
  GET  /auth/me            — decode token → user info
  POST /auth/logout        — client-side; endpoint for completeness
  GET  /auth/session       — generate anonymous session token

JWT is accepted via:
  1. Authorization: Bearer <token>  (API / existing frontend)
  2. HttpOnly cookie 'sp_jwt'       (cookie-based login, C-8 fix)

C-6  — In-memory IP rate limiter on /auth/login (10 failures / 60 s → 429).
        No extra library required.
C-7  — Hardened custom JWT: correct base64 padding, alg header check,
        nbf claim added, SECRET_KEY default raises on startup (not just warns).
C-8  — Cookie-based login endpoint + get_current_user accepts cookie OR header.
"""
import secrets
import bcrypt
import hashlib
import hmac
import base64
import json
import time
import os
from collections import defaultdict
from datetime import datetime, timedelta

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Header, Request, Response

from sqlalchemy.orm import Session

from config import SECRET_KEY, JWT_ALGORITHM, JWT_EXPIRE_DAYS, logger
from database.models import engine, UserORM
from schemas import RegisterRequest, LoginRequest, AuthResponse

router = APIRouter(prefix="/auth")

# ── C-7: Enforce non-default SECRET_KEY at startup ───────────────────────────
_SECRET_KEY_DEFAULT = "change-this-in-production-please"
if SECRET_KEY == _SECRET_KEY_DEFAULT:
    raise RuntimeError(
        "FATAL: SECRET_KEY is using the insecure default value. "
        "Set a strong SECRET_KEY in your .env file before running the server."
    )

# ── C-6: In-memory brute-force rate limiter ───────────────────────────────────
# Tracks failed login attempts per IP. No external library needed.
_RATE_WINDOW_SECONDS = 60
_RATE_MAX_FAILURES   = 10
_fail_counts: dict   = defaultdict(list)   # ip → [timestamp, ...]

def _check_rate_limit(ip: str):
    now    = time.time()
    cutoff = now - _RATE_WINDOW_SECONDS
    # Purge old entries
    _fail_counts[ip] = [t for t in _fail_counts[ip] if t > cutoff]
    if len(_fail_counts[ip]) >= _RATE_MAX_FAILURES:
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed login attempts. Please wait {_RATE_WINDOW_SECONDS} seconds.",
        )

def _record_failure(ip: str):
    _fail_counts[ip].append(time.time())

def _clear_failures(ip: str):
    _fail_counts.pop(ip, None)


# ── C-7: Hardened JWT helpers ─────────────────────────────────────────────────
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

def _b64url_decode(s: str) -> bytes:
    """Correct padding regardless of how many chars are missing."""
    padding = (4 - len(s) % 4) % 4
    return base64.urlsafe_b64decode(s + "=" * padding)

def _sign(payload: dict) -> str:
    header  = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body    = _b64url(json.dumps(payload).encode())
    sig     = _b64url(
        hmac.new(SECRET_KEY.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest()
    )
    return f"{header}.{body}.{sig}"

def _verify(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError("malformed token")
        header_b64, body_b64, sig = parts

        # C-7: verify alg claim before trusting the signature
        header = json.loads(_b64url_decode(header_b64))
        if header.get("alg") != "HS256":
            raise ValueError("unsupported algorithm")

        expected = _b64url(
            hmac.new(SECRET_KEY.encode(), f"{header_b64}.{body_b64}".encode(), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(sig, expected):
            raise ValueError("bad signature")

        payload = json.loads(_b64url_decode(body_b64))

        now = time.time()
        if payload.get("exp", 0) < now:
            raise ValueError("token expired")
        # C-7: nbf (not-before) check
        if "nbf" in payload and payload["nbf"] > now:
            raise ValueError("token not yet valid")

        return payload
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")


def _hash_pw(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def _verify_pw(plaintext: str, stored_hash: str) -> bool:
    return bcrypt.checkpw(plaintext.encode("utf-8"), stored_hash.encode("utf-8"))


# ── C-8: get_current_user accepts Bearer header OR HttpOnly cookie ────────────
def get_current_user(
    request: Request,
    authorization: str = Header(None),
) -> dict:
    """Dependency — call in protected routes. Accepts Bearer token or sp_jwt cookie."""
    # 1. Try Authorization header first (API clients, existing frontend)
    if authorization and authorization.startswith("Bearer "):
        return _verify(authorization.split(" ", 1)[1])
    # 2. Fall back to HttpOnly cookie (cookie-login flow)
    cookie_token = request.cookies.get("sp_jwt")
    if cookie_token:
        return _verify(cookie_token)
    raise HTTPException(status_code=401, detail="Authorization header or session cookie missing.")


def _make_payload(user_id: int, username: str, role: str) -> dict:
    now = int(time.time())
    return {
        "sub":  user_id,
        "user": username,
        "role": role,
        "iat":  now,
        "nbf":  now,
        "exp":  int((datetime.utcnow() + timedelta(days=JWT_EXPIRE_DAYS)).timestamp()),
    }


# ── Register ──────────────────────────────────────────────────────────────────
@router.post("/register", response_model=AuthResponse, status_code=201)
async def register(req: RegisterRequest):
    db = Session(engine)
    try:
        exists = db.execute(
            sa.text("SELECT id FROM users WHERE email=:e OR username=:u"),
            {"e": req.email, "u": req.username},
        ).fetchone()
        if exists:
            raise HTTPException(status_code=409, detail="Email or username already taken.")

        user = UserORM(
            username=req.username,
            email=req.email,
            password_hash=_hash_pw(req.password),
            role="user",
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        token = _sign(_make_payload(user.id, user.username, user.role))
        logger.info(f"New user registered: {user.username} (id={user.id})")
        return AuthResponse(token=token, user_id=user.id, username=user.username, role=user.role)
    finally:
        db.close()


# ── Login (Bearer token — existing flow) ──────────────────────────────────────
@router.post("/login", response_model=AuthResponse)
async def login(req: LoginRequest, request: Request):
    ip = request.client.host if request.client else "unknown"
    _check_rate_limit(ip)   # C-6

    db = Session(engine)
    try:
        row = db.execute(
            sa.text("SELECT * FROM users WHERE email=:id OR username=:id"),
            {"id": req.identifier},
        ).fetchone()
        if not row or not _verify_pw(req.password, row.password_hash):
            _record_failure(ip)   # C-6
            raise HTTPException(status_code=401, detail="Invalid credentials.")

        _clear_failures(ip)   # C-6: reset on success
        token = _sign(_make_payload(row.id, row.username, row.role))
        return AuthResponse(token=token, user_id=row.id, username=row.username, role=row.role)
    finally:
        db.close()


# ── Cookie Login (C-8 fix) ────────────────────────────────────────────────────
@router.post("/cookie-login")
async def cookie_login(req: LoginRequest, request: Request, response: Response):
    """
    Same as /login but sets the JWT as an HttpOnly; Secure; SameSite=Strict cookie
    instead of returning it in the response body.
    The response body still includes user_id, username, role for display purposes.
    The token itself is NOT in the response body.
    """
    ip = request.client.host if request.client else "unknown"
    _check_rate_limit(ip)

    db = Session(engine)
    try:
        row = db.execute(
            sa.text("SELECT * FROM users WHERE email=:id OR username=:id"),
            {"id": req.identifier},
        ).fetchone()
        if not row or not _verify_pw(req.password, row.password_hash):
            _record_failure(ip)
            raise HTTPException(status_code=401, detail="Invalid credentials.")

        _clear_failures(ip)
        token = _sign(_make_payload(row.id, row.username, row.role))

        response.set_cookie(
            key="sp_jwt",
            value=token,
            httponly=True,
            secure=True,          # HTTPS only
            samesite="strict",
            max_age=JWT_EXPIRE_DAYS * 86400,
            path="/",
        )
        # Return non-sensitive display data only — token is in the cookie, not here
        return {"user_id": row.id, "username": row.username, "role": row.role}
    finally:
        db.close()


# ── Cookie Logout (C-8 fix) ───────────────────────────────────────────────────
@router.post("/cookie-logout")
async def cookie_logout(response: Response):
    """Clears the sp_jwt HttpOnly cookie."""
    response.delete_cookie(key="sp_jwt", path="/", httponly=True, secure=True, samesite="strict")
    return {"ok": True}


# ── Me ────────────────────────────────────────────────────────────────────────
@router.get("/me")
async def me(request: Request, authorization: str = Header(None)):
    payload = get_current_user(request, authorization)
    return {"user_id": payload["sub"], "username": payload["user"], "role": payload["role"]}


# ── Logout (client-side, token is stateless) ──────────────────────────────────
@router.post("/logout")
async def logout():
    return {"ok": True, "message": "Token cleared client-side."}


# ── GET /auth/session — server-side anonymous session token ──────────────────
@router.get("/session")
async def get_session_token():
    """
    Generate a cryptographically secure anonymous session token.
    The frontend should call this once on first load and store it in
    localStorage. All subsequent /analyze calls include this token.
    Stateless — no DB write. Just a random unique string.
    """
    return {"session_token": secrets.token_hex(32)}  # 64-char hex string
