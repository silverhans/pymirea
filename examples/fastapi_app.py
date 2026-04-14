"""Пример: маленький FastAPI-сервис с эндпоинтами /api/login и /api/schedule.
Сессии хранятся зашифрованными через SessionCrypto в per-process dict
(в продакшене — используйте реальную БД).

Запуск::

    pip install fastapi uvicorn
    export MIREA_SESSION_KEY="..."
    uvicorn examples.fastapi_app:app --reload
"""

import os
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from pymirea import (
    Config,
    MireaAPI,
    MireaAuth,
    configure,
)
from pymirea.crypto import get_crypto

configure(Config(session_keys=os.environ["MIREA_SESSION_KEY"]))
crypto = get_crypto()
app = FastAPI(title="pymirea demo")

# token (uuid) -> зашифрованные cookies МИРЭА
SESSIONS: dict[str, str] = {}


class LoginBody(BaseModel):
    login: str
    password: str


class OtpBody(BaseModel):
    challenge_id: str
    code: str


PENDING: dict[str, dict] = {}


@app.post("/api/login")
async def login(body: LoginBody):
    auth = MireaAuth()
    result = await auth.login(body.login, body.password)

    if result.challenge:
        import uuid

        challenge_id = str(uuid.uuid4())
        PENDING[challenge_id] = {"auth": auth, "challenge": result.challenge}
        return {"need_otp": True, "challenge_id": challenge_id}

    if not result.tokens:
        raise HTTPException(401, "Login failed")

    return _issue_token(result.tokens)


@app.post("/api/login/otp")
async def login_otp(body: OtpBody):
    state = PENDING.pop(body.challenge_id, None)
    if not state:
        raise HTTPException(404, "Unknown challenge")

    auth: MireaAuth = state["auth"]
    result = await auth.complete_2fa(state["challenge"], body.code)
    if not result.tokens:
        raise HTTPException(401, "Invalid OTP")

    return _issue_token(result.tokens)


@app.get("/api/schedule")
async def schedule(authorization: Optional[str] = Header(None)):
    cookies = _session_from_header(authorization)
    api = MireaAPI(session_cookies=cookies)
    sched = await api.get_schedule()
    return {
        "days": [
            {
                "date": d.date,
                "lessons": [
                    {"start": l.start, "end": l.end, "subject": l.subject, "teacher": l.teacher}
                    for l in d.lessons
                ],
            }
            for d in sched.days
        ]
    }


# ── вспомогательные функции ─────────────────────────────────────────────────────────────
def _issue_token(cookies: dict) -> dict:
    import uuid

    token = str(uuid.uuid4())
    SESSIONS[token] = crypto.encrypt_session(cookies)
    return {"need_otp": False, "token": token}


def _session_from_header(auth: Optional[str]) -> dict:
    if not auth or not auth.startswith("Bearer "):
        raise HTTPException(401, "Missing bearer")
    encrypted = SESSIONS.get(auth.removeprefix("Bearer "))
    if not encrypted:
        raise HTTPException(401, "Unknown token")
    cookies = crypto.decrypt_session(encrypted)
    if cookies is None:
        raise HTTPException(401, "Session decrypt failed")
    return cookies
