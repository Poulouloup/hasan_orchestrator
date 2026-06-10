"""
Authentification par Bearer token.

Deux niveaux de tokens :
1. ORCHESTRATOR_ADMIN_KEY : utilisé par Hermes pour /api/* et la connexion MCP.
2. session_token par device : généré au /register, vérifié en SQLite,
   révoqué automatiquement lors d'un nouveau /register (un seul token actif
   par device).
"""

from __future__ import annotations

import hmac
import logging

from fastapi import Header, HTTPException, status

import config
from models import Device
from registry import registry

logger = logging.getLogger("hasan.auth")


def _extract_bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="En-tête Authorization manquant",
        )
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Format d'en-tête Authorization invalide (attendu: 'Bearer <token>')",
        )
    return parts[1].strip()


async def require_admin(authorization: str | None = Header(default=None)) -> str:
    """Dépendance FastAPI : vérifie que le token correspond à ORCHESTRATOR_ADMIN_KEY."""
    token = _extract_bearer_token(authorization)
    if not hmac.compare_digest(token, config.ORCHESTRATOR_ADMIN_KEY):
        logger.warning("Tentative d'accès admin avec un token invalide")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token administrateur invalide",
        )
    return token


async def require_device_session(authorization: str | None = Header(default=None)) -> Device:
    """Dépendance FastAPI : vérifie le session_token d'un device et retourne
    le device correspondant."""
    token = _extract_bearer_token(authorization)
    device = await registry.get_by_session_token(token)
    if device is None:
        logger.warning("Tentative d'accès device avec un session_token invalide")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session token invalide ou expiré",
        )
    return device
