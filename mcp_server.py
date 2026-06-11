"""
Serveur MCP exposé à Hermes — zéro connaissance hardcodée des capabilities.

Contrairement à un serveur MCP classique qui expose des tools fixes,
celui-ci n'expose qu'un seul tool d'exécution : `exec_action`.

Les devices déclarent leurs propres capabilities à l'enregistrement.
L'orchestrateur les stocke dans la registry SQLite et les route
dynamiquement. Hermes découvre les capabilities disponibles via
`device_list()` et appelle `exec_action(action, params, device_name?)`
pour exécuter n'importe quelle action sur n'importe quel device.

Le serveur tourne en stdio (lancé par Hermes comme sous-processus) et
communique avec l'API FastAPI via les endpoints /internal/* pour créer
les commandes et attendre les résultats.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP

import config
from models import DeviceStatus
from registry import registry

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,  # stdout est réservé au protocole MCP
)
logger = logging.getLogger("hasan.mcp")

mcp = FastMCP("hasan-orchestrator")

ORCHESTRATOR_BASE_URL = f"http://{config.ORCHESTRATOR_HOST}:{config.ORCHESTRATOR_PORT}"


# ---------------------------------------------------------------------------
# Client HTTP interne vers l'orchestrateur (pour la création de commandes)
# ---------------------------------------------------------------------------

async def _internal_request(method: str, path: str, **kwargs) -> httpx.Response:
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {config.ORCHESTRATOR_ADMIN_KEY}"
    async with httpx.AsyncClient(base_url=ORCHESTRATOR_BASE_URL, timeout=30.0) as client:
        return await client.request(method, path, headers=headers, **kwargs)


# ---------------------------------------------------------------------------
# Routing des commandes
# ---------------------------------------------------------------------------

class RoutingError(Exception):
    """Erreur de routage rapportée telle quelle à Hermes."""


async def _resolve_target_device(
    device_name: Optional[str], capability: str
):
    """Résout le device cible en fonction du nom fourni ou de la capability.

    Retourne le Device choisi.
    Lève RoutingError avec un message clair en cas de problème.
    """
    if device_name:
        device = await registry.get_by_name(device_name)
        if device is None:
            raise RoutingError(f"Device '{device_name}' introuvable")
        if device.status != DeviceStatus.online:
            raise RoutingError(f"Device {device_name} est offline")
        cap = device.capabilities.get(capability)
        if cap is None or not cap.enabled:
            raise RoutingError(f"Capability {capability} non activée sur {device_name}")
        return device

    # Pas de device précisé : cherche tous les devices online avec la capability
    devices = await registry.list_all()
    candidates = [
        d
        for d in devices
        if d.status == DeviceStatus.online
        and capability in d.capabilities
        and d.capabilities[capability].enabled
    ]

    if not candidates:
        raise RoutingError(f"Aucun device online avec {capability}")

    if len(candidates) == 1:
        return candidates[0]

    names = ", ".join(d.device_name for d in candidates)
    raise RoutingError(f"Plusieurs devices disponibles : [{names}]. Précise le device.")


async def route_command(
    action: str,
    params: dict[str, Any],
    device_name: Optional[str] = None,
    capability: Optional[str] = None,
) -> dict[str, Any]:
    """Logique commune de routage d'une commande vers un device.

    1. Résout le device cible (par nom ou par capability disponible).
    2. Si la capability nécessite une confirmation -> crée la commande en
       awaiting_confirmation et retourne immédiatement un message d'attente.
    3. Sinon, crée la commande pending, attend le résultat (polling 500ms,
       timeout 25s) et retourne le résultat ou une erreur de timeout.
    """
    cap_name = capability or action

    try:
        device = await _resolve_target_device(device_name, cap_name)
    except RoutingError as exc:
        return {"error": str(exc)}

    cap_flag = device.capabilities.get(cap_name)
    requires_confirmation = bool(cap_flag and cap_flag.auth_required)

    # Création de la commande via l'API interne (queue partagée du process principal)
    response = await _internal_request(
        "POST",
        "/internal/commands",
        json={
            "device_hash": device.device_hash,
            "action": action,
            "params": params,
            "requires_confirmation": requires_confirmation,
        },
    )
    if response.status_code != 200:
        return {"error": f"Erreur orchestrateur : {response.text}"}

    command_data = response.json()
    command_id = command_data["command_id"]

    if requires_confirmation:
        return {
            "status": "awaiting_confirmation",
            "message": f"En attente de confirmation sur {device.device_name}",
            "command_id": command_id,
        }

    # Attend le résultat
    result_response = await _internal_request(
        "GET", f"/internal/commands/{command_id}/wait"
    )
    if result_response.status_code != 200:
        return {"error": f"Erreur orchestrateur : {result_response.text}"}

    result = result_response.json()
    final_status = result.get("status")

    if final_status == "expired":
        return {"error": f"Timeout — {device.device_name} n'a pas répondu"}

    if final_status == "failed":
        error_msg = (result.get("result") or {}).get("error", "erreur inconnue")
        return {"error": error_msg}

    return result.get("result") or {}


# ---------------------------------------------------------------------------
# Tools MCP
# ---------------------------------------------------------------------------

@mcp.tool()
async def device_list() -> list[dict[str, Any]]:
    """Liste tous les devices online avec leurs capabilities activées."""
    devices = await registry.list_all()
    return [
        {
            "name": d.device_name,
            "type": d.device_type.value,
            "status": d.status.value,
            "capabilities_enabled": [
                name for name, flag in d.capabilities.items() if flag.enabled
            ],
        }
        for d in devices
        if d.status == DeviceStatus.online
    ]


@mcp.tool()
async def device_info(device_name: str) -> dict[str, Any]:
    """Retourne les informations détaillées d'un device (registry complet)."""
    device = await registry.get_by_name(device_name)
    if device is None:
        return {"error": f"Device '{device_name}' introuvable"}

    data = device.model_dump(mode="json")
    data.pop("session_token", None)
    return data


@mcp.tool()
async def exec_action(
    action: str, params: dict[str, Any], device_name: Optional[str] = None
) -> dict[str, Any]:
    """Execute any action on a device (generic tool).

    This is the only execution tool in the orchestrator. The device
    declares its own capabilities at registration time — this tool
    routes any action to the right device dynamically.

    Use device_list() to discover available devices and their
    declared capabilities.

    Examples:
    - exec_action("send_sms", {"numero": "0612345678", "message": "hello"}, "phone")
    - exec_action("record_audio", {"duration": 10}, "phone")
    - exec_action("open_file", {"path": "/home/user/doc.pdf"}, "desk")
    """
    return await route_command(
        action=action,
        params=params,
        device_name=device_name,
        capability=action,
    )


@mcp.tool()
async def get_command_result(command_id: str) -> dict[str, Any]:
    """Récupère le résultat d'une commande (auth_required notamment).

    Les actions avec auth_required=True (ex: get_location, send_sms)
    retournent un command_id via exec_action sans le résultat final.
    Utilise ce tool pour récupérer le résultat une fois que l'utilisateur
    a confirmé sur son téléphone.

    Retourne le résultat complet (lat/lng pour location, status pour SMS, etc.)
    ou {"error": "..."} si la commande n'existe plus (TTL expiré).
    """
    resp = await _internal_request("GET", f"/api/commands/{command_id}")
    if resp.status_code != 200:
        return {"error": f"Commande '{command_id}' introuvable ou expirée"}
    data = resp.json()
    return {
        "command_id": data["command_id"],
        "status": data["status"],
        "result": data.get("result"),
        "error": (data.get("result") or {}).get("error"),
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
