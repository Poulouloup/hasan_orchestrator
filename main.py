"""
Point d'entrée FastAPI de l'orchestrateur Hasan.

Expose :
- les endpoints REST utilisés par les devices (register, heartbeat, commands,
  results, confirm, rename, capabilities)
- les endpoints REST admin utilisés par Hermes (/api/devices, /api/commands)

Le serveur MCP (mcp_server.py) est un processus séparé qui réutilise les
mêmes modules `registry` et `queue` pour communiquer avec cette API en
mode "in-process" si lancé conjointement, ou via HTTP si exposé séparément.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import config
from auth import require_admin, require_device_session
from models import (
    CapabilitiesUpdateRequest,
    CapabilityFlag,
    CapabilityPatch,
    Command,
    CommandOut,
    CommandStatus,
    ConfirmRequest,
    Device,
    HeartbeatRequest,
    HeartbeatResponse,
    RegisterRequest,
    RegisterResponse,
    RenameRequest,
    ResultRequest,
    utcnow,
)
from command_queue import command_queue
from registry import registry

# ---------------------------------------------------------------------------
# Configuration du logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("hasan.main")


# ---------------------------------------------------------------------------
# Application FastAPI
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Hasan Orchestrator",
    description="Orchestrateur multi-devices entre Hermes et les agents (mobile, desktop, laptop).",
    version="1.0.0",
)


@app.on_event("startup")
async def on_startup() -> None:
    """Initialise la base de données et démarre les tâches de fond."""
    await registry.init()
    asyncio.create_task(_offline_watcher_loop())
    asyncio.create_task(command_queue.cleanup_loop())
    logger.info(
        "Orchestrateur démarré sur %s:%s", config.ORCHESTRATOR_HOST, config.ORCHESTRATOR_PORT
    )


async def _offline_watcher_loop() -> None:
    """Tâche de fond : vérifie périodiquement les devices inactifs."""
    interval = config.HEARTBEAT_INTERVAL
    while True:
        await asyncio.sleep(interval)
        try:
            await registry.refresh_offline_status()
        except Exception:
            logger.exception("Erreur lors de la vérification des devices offline")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _device_to_public_dict(device: Device) -> dict:
    """Représentation publique d'un device (sans le session_token)."""
    data = device.model_dump(mode="json")
    data.pop("session_token", None)
    return data


# ===========================================================================
# ENDPOINTS POUR LES DEVICES
# ===========================================================================

@app.post("/register", response_model=RegisterResponse)
async def register_device(payload: RegisterRequest) -> RegisterResponse:
    """Enregistre ou met à jour un device. Génère un nouveau session_token."""

    # Si aucune capability fournie, on applique les valeurs par défaut
    capabilities = payload.capabilities
    if not capabilities:
        from models import DEFAULT_CAPABILITIES

        capabilities = {
            name: CapabilityFlag(**flag) for name, flag in DEFAULT_CAPABILITIES.items()
        }

    device, session_token = await registry.register_device(
        device_hash=payload.device_hash,
        device_name=payload.device_name,
        device_type=payload.device_type,
        capabilities=capabilities,
        version=payload.version,
    )

    return RegisterResponse(
        status="registered",
        session_token=session_token,
        heartbeat_interval=config.HEARTBEAT_INTERVAL,
        polling_interval=config.HEARTBEAT_INTERVAL,
        server_time=utcnow(),
    )


@app.post("/heartbeat", response_model=HeartbeatResponse)
async def heartbeat(
    payload: HeartbeatRequest, device: Device = Depends(require_device_session)
) -> HeartbeatResponse:
    """Keepalive d'un device + mise à jour des infos réseau."""

    if device.device_hash != payload.device_hash:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="device_hash ne correspond pas au session_token utilisé",
        )

    exists, refresh_needed = await registry.heartbeat(
        device_hash=payload.device_hash,
        capabilities_version=payload.capabilities_version,
        network=payload.network,
    )
    if not exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device inconnu")

    return HeartbeatResponse(
        status="ok",
        capabilities_refresh_needed=refresh_needed,
        server_time=utcnow(),
    )


@app.get("/commands")
async def get_commands(
    device_hash: str = Query(...),
    timeout: int = Query(default=config.LONG_POLL_TIMEOUT, ge=0, le=config.LONG_POLL_TIMEOUT),
    device: Device = Depends(require_device_session),
) -> list[CommandOut] | dict:
    """Long polling : retourne les commandes en attente pour ce device."""

    if device.device_hash != device_hash:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="device_hash ne correspond pas au session_token utilisé",
        )

    commands = await command_queue.wait_for_commands(device_hash, timeout=float(timeout))

    if not commands:
        return JSONResponse(content={})

    return [
        CommandOut(
            command_id=cmd.command_id,
            action=cmd.action,
            params=cmd.params,
            confirm_required=(cmd.status == CommandStatus.awaiting_confirmation),
            expires_at=cmd.expires_at,
        )
        for cmd in commands
    ]


@app.post("/results")
async def post_results(
    payload: ResultRequest, device: Device = Depends(require_device_session)
) -> dict:
    """Le device retourne le résultat d'une commande exécutée."""

    command = await command_queue.get_command(payload.command_id)
    if command is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Commande inconnue")

    if command.device_hash != device.device_hash:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cette commande ne concerne pas ce device",
        )

    if payload.status not in (CommandStatus.done, CommandStatus.failed):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="status doit être 'done' ou 'failed'",
        )

    await command_queue.set_result(
        command_id=payload.command_id,
        status=payload.status,
        data=payload.data,
        error=payload.error,
    )

    return {"status": "ok"}


@app.post("/confirm")
async def post_confirm(
    payload: ConfirmRequest, device: Device = Depends(require_device_session)
) -> dict:
    """Le device confirme ou refuse une action auth_required."""

    command = await command_queue.get_command(payload.command_id)
    if command is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Commande inconnue")

    if command.device_hash != device.device_hash:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cette commande ne concerne pas ce device",
        )

    updated = await command_queue.confirm_command(payload.command_id, payload.approved)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Commande inconnue")

    return {"status": updated.status.value}


@app.patch("/rename")
async def patch_rename(
    payload: RenameRequest, device: Device = Depends(require_device_session)
) -> dict:
    """Renomme le device authentifié (ou un autre device si l'admin l'autorise
    via le même session_token — ici on restreint au device lui-même)."""

    if device.device_hash != payload.device_hash:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="device_hash ne correspond pas au session_token utilisé",
        )

    try:
        updated = await registry.rename_device(payload.device_hash, payload.new_name)
    except ValueError as exc:
        if str(exc) == "name_taken":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Le nom '{payload.new_name}' est déjà utilisé par un autre device",
            )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device inconnu")

    return {"status": "ok", "device_name": updated.device_name, "previous_names": updated.previous_names}


@app.patch("/capabilities")
async def patch_capabilities(
    payload: CapabilitiesUpdateRequest, device: Device = Depends(require_device_session)
) -> dict:
    """Le device met à jour lui-même ses capabilities (sans re-register)."""

    if device.device_hash != payload.device_hash:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="device_hash ne correspond pas au session_token utilisé",
        )

    try:
        updated = await registry.update_capabilities(payload.device_hash, payload.capabilities, merge=True)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device inconnu")

    return {"status": "ok", "capabilities": {k: v.model_dump() for k, v in updated.capabilities.items()}}


# ===========================================================================
# ENDPOINTS ADMIN (Hermes)
# ===========================================================================

@app.get("/api/devices")
async def api_list_devices(_: str = Depends(require_admin)) -> list[dict]:
    """Liste tous les devices avec leur status."""
    devices = await registry.list_all()
    return [_device_to_public_dict(d) for d in devices]


@app.get("/api/devices/{device_name}")
async def api_get_device(device_name: str, _: str = Depends(require_admin)) -> dict:
    """Infos détaillées d'un device."""
    device = await registry.get_by_name(device_name)
    if device is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Device '{device_name}' introuvable")
    return _device_to_public_dict(device)


@app.get("/api/commands/{command_id}")
async def api_get_command(command_id: str, _: str = Depends(require_admin)) -> dict:
    """Status d'une commande."""
    command = await command_queue.get_command(command_id)
    if command is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Commande '{command_id}' introuvable")
    return command.model_dump(mode="json")


@app.patch("/api/devices/{device_name}/capabilities")
async def api_patch_capabilities(
    device_name: str, payload: dict[str, CapabilityPatch], _: str = Depends(require_admin)
) -> dict:
    """Modifie les capabilities d'un device depuis l'orchestrateur (merge)."""
    device = await registry.get_by_name(device_name)
    if device is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Device '{device_name}' introuvable")

    capabilities = {
        name: CapabilityFlag(enabled=patch.enabled, auth_required=patch.auth_required)
        for name, patch in payload.items()
    }

    updated = await registry.update_capabilities(device.device_hash, capabilities, merge=True)
    return {
        "status": "ok",
        "device_name": updated.device_name,
        "capabilities": {k: v.model_dump() for k, v in updated.capabilities.items()},
    }


@app.post("/api/devices/{device_name}/capabilities/{capability_name}")
async def api_set_single_capability(
    device_name: str, capability_name: str, payload: CapabilityPatch, _: str = Depends(require_admin)
) -> dict:
    """Ajoute ou modifie une seule capability."""
    device = await registry.get_by_name(device_name)
    if device is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Device '{device_name}' introuvable")

    flag = CapabilityFlag(enabled=payload.enabled, auth_required=payload.auth_required)
    updated = await registry.set_single_capability(device.device_hash, capability_name, flag)

    return {
        "status": "ok",
        "device_name": updated.device_name,
        "capability": capability_name,
        "value": updated.capabilities[capability_name].model_dump(),
    }


@app.delete("/api/devices/{device_name}/capabilities/{capability_name}")
async def api_delete_capability(
    device_name: str, capability_name: str, _: str = Depends(require_admin)
) -> dict:
    """Supprime complètement une capability d'un device."""
    device = await registry.get_by_name(device_name)
    if device is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Device '{device_name}' introuvable")

    try:
        updated = await registry.delete_capability(device.device_hash, capability_name)
    except ValueError as exc:
        if str(exc) == "capability_not_found":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Capability '{capability_name}' introuvable sur '{device_name}'",
            )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device inconnu")

    return {"status": "ok", "device_name": updated.device_name, "capabilities": {k: v.model_dump() for k, v in updated.capabilities.items()}}


# ===========================================================================
# ENDPOINTS INTERNES (utilisés par mcp_server.py pour le routage des commandes)
# ===========================================================================

class InternalCommandRequest(BaseModel):
    device_hash: str
    action: str
    params: dict
    requires_confirmation: bool = False


@app.post("/internal/commands")
async def internal_create_command(
    payload: InternalCommandRequest, _: str = Depends(require_admin)
) -> dict:
    """Crée une commande dans la file partagée (utilisé par le serveur MCP)."""
    command = await command_queue.create_command(
        device_hash=payload.device_hash,
        action=payload.action,
        params=payload.params,
        requires_confirmation=payload.requires_confirmation,
    )
    return command.model_dump(mode="json")


@app.get("/internal/commands/{command_id}/wait")
async def internal_wait_command(command_id: str, _: str = Depends(require_admin)) -> dict:
    """Attend le résultat d'une commande (polling interne, timeout configurable)."""
    command = await command_queue.wait_for_result(
        command_id,
        timeout=config.COMMAND_RESULT_TIMEOUT,
        poll_interval_ms=config.COMMAND_POLL_INTERVAL_MS,
    )
    if command is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Commande inconnue")
    return command.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Healthcheck
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "server_time": utcnow().isoformat()}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=config.ORCHESTRATOR_HOST,
        port=config.ORCHESTRATOR_PORT,
        reload=False,
    )
