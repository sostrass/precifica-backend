"""Autenticação multi-tenant: senha com bcrypt e sessão com JWT."""

import hashlib
from datetime import datetime, timedelta

import bcrypt
import jwt
from fastapi import Header, HTTPException

from .config import settings
from .db import SessionLocal
from .models import User


def _signing_key() -> str:
    """Chave de assinatura do JWT com pelo menos 32 bytes (exigência do HS256/SHA-256).

    Se a JWT_SECRET configurada for curta (< 32 bytes), deriva uma chave estável de
    64 bytes via SHA-256 — silencia o InsecureKeyLengthWarning e mantém o login
    funcionando com qualquer valor de secret, sem precisar trocar a env do Railway.
    """
    s = (settings.jwt_secret or "").encode()
    if len(s) >= 32:
        return settings.jwt_secret
    return hashlib.sha256(s).hexdigest()  # 64 caracteres = 64 bytes


_JWT_KEY = _signing_key()


def hash_password(senha: str) -> str:
    # bcrypt trava em senhas > 72 bytes; truncamos por segurança.
    return bcrypt.hashpw(senha.encode()[:72], bcrypt.gensalt()).decode()


def verify_password(senha: str, password_hash: str) -> bool:
    return bcrypt.checkpw(senha.encode()[:72], password_hash.encode())


def create_access_token(user_id: int) -> str:
    payload = {
        "sub": str(user_id),
        "type": "access",
        "exp": datetime.utcnow() + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, _JWT_KEY, algorithm="HS256")


def decode_token(token: str) -> dict:
    return jwt.decode(token, _JWT_KEY, algorithms=["HS256"])


def registrar(email: str, senha: str, nome: str | None = None) -> User:
    email = email.strip().lower()
    if "@" not in email or len(senha) < 6:
        raise HTTPException(status_code=422, detail="E-mail inválido ou senha curta (mín. 6).")
    with SessionLocal() as db:
        if db.query(User).filter(User.email == email).first():
            raise HTTPException(status_code=409, detail="E-mail já cadastrado.")
        user = User(email=email, password_hash=hash_password(senha), nome=nome)
        db.add(user)
        db.commit()
        db.refresh(user)
        return user


def autenticar(email: str, senha: str) -> User:
    email = email.strip().lower()
    with SessionLocal() as db:
        user = db.query(User).filter(User.email == email).first()
        if not user or not verify_password(senha, user.password_hash):
            raise HTTPException(status_code=401, detail="Credenciais inválidas.")
        return user


def get_current_user(authorization: str | None = Header(None)) -> User:
    """Dependency: extrai o usuário a partir do JWT no header Authorization."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token ausente.")
    token = authorization.split(" ", 1)[1]
    try:
        payload = decode_token(token)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Token inválido ou expirado.")
    with SessionLocal() as db:
        user = db.get(User, int(payload.get("sub", 0)))
        if not user:
            raise HTTPException(status_code=401, detail="Usuário não encontrado.")
        return user
