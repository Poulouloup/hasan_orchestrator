"""
Serveur MCP exposé à Hermes.

Ce serveur tourne en stdio (lancé directement par Hermes comme sous-processus)
et communique avec l'orchestrateur FastAPI via son API REST interne, en
utilisant ORCHESTRATOR_ADMIN_KEY. Il réutilise directement les modules
`registry` et `queue` car il est conçu pour tourner sur la même machine,
mais passe par la couche métier en mémoire partagée si lancé in-process,
ou recrée une commande via la queue locale si lancé séparément.

Pour simplifier le déploiement (un seul process orchestrateur = une seule
queue en mémoire), ce serveur MCP importe directement `registry` et
`queue` et accède à la même base SQLite. La file de commandes en mémoire
(`command_queue`) doit donc être partagée : on lance ce script comme
sous-processus de l'orchestrateur principal n'est PAS supporté pour la queue
en mémoire séparée -> voir README pour le mode recommandé (MCP intégré dans
le même process via le endpoint HTTP /mcp, ou stdio avec queue partagée par
fichier).

Implémentation retenue : stdio + accès direct à la registry SQLite (lecture
des devices) et création des commandes via un appel HTTP interne vers
l'orchestrateur (POST /internal/commands), qui partage la queue en mémoire
du process principal. Cela garantit une seule source de vérité pour les
commandes en attente.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP

import config
from models import CapabilityFlag, CommandStatus, DeviceStatus, DeviceType
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
async def send_sms(numero: str, message: str, device_name: Optional[str] = None) -> dict[str, Any]:
    """Envoie un SMS depuis un device mobile (capability send_sms)."""
    return await route_command(
        action="send_sms",
        params={"numero": numero, "message": message},
        device_name=device_name,
        capability="send_sms",
    )


@mcp.tool()
async def make_call(numero: str, device_name: Optional[str] = None) -> dict[str, Any]:
    """Lance un appel téléphonique depuis un device mobile (capability make_call)."""
    return await route_command(
        action="make_call",
        params={"numero": numero},
        device_name=device_name,
        capability="make_call",
    )


@mcp.tool()
async def get_location(device_name: Optional[str] = None) -> dict[str, Any]:
    """Récupère la position GPS d'un device (capability get_location)."""
    return await route_command(
        action="get_location",
        params={},
        device_name=device_name,
        capability="get_location",
    )


@mcp.tool()
async def screenshot(device_name: Optional[str] = None) -> dict[str, Any]:
    """Prend une capture d'écran d'un device (capability screenshot)."""
    return await route_command(
        action="screenshot",
        params={},
        device_name=device_name,
        capability="screenshot",
    )


@mcp.tool()
async def open_file(path: str, device_name: Optional[str] = None) -> dict[str, Any]:
    """Ouvre un fichier sur un device (capability open_file)."""
    return await route_command(
        action="open_file",
        params={"path": path},
        device_name=device_name,
        capability="open_file",
    )


@mcp.tool()
async def run_terminal(command: str, device_name: Optional[str] = None) -> dict[str, Any]:
    """Exécute une commande dans un terminal sur un device (capability run_terminal,
    nécessite généralement une confirmation utilisateur)."""
    return await route_command(
        action="run_terminal",
        params={"command": command},
        device_name=device_name,
        capability="run_terminal",
    )


@mcp.tool()
async def launch_app(app_name: str, device_name: Optional[str] = None) -> dict[str, Any]:
    """Lance une application sur un device (capability launch_app)."""
    return await route_command(
        action="launch_app",
        params={"app_name": app_name},
        device_name=device_name,
        capability="launch_app",
    )


@mcp.tool()
async def get_battery(device_name: Optional[str] = None) -> dict[str, Any]:
    """Récupère le niveau de batterie d'un device (capability get_battery)."""
    return await route_command(
        action="get_battery",
        params={},
        device_name=device_name,
        capability="get_battery",
    )


@mcp.tool()
async def set_volume(level: int, device_name: Optional[str] = None) -> dict[str, Any]:
    """Règle le volume d'un device entre 0 et 100 (capability set_volume)."""
    if not 0 <= level <= 100:
        return {"error": "level doit être compris entre 0 et 100"}
    return await route_command(
        action="set_volume",
        params={"level": level},
        device_name=device_name,
        capability="set_volume",
    )


@mcp.tool()
async def send_notification(
    title: str, message: str, device_name: Optional[str] = None
) -> dict[str, Any]:
    """Envoie une notification push sur un device (capability send_notification)."""
    return await route_command(
        action="send_notification",
        params={"title": title, "message": message},
        device_name=device_name,
        capability="send_notification",
    )


@mcp.tool()
async def set_capability(
    device_name: str, capability: str, enabled: bool, auth_required: bool = False
) -> dict[str, Any]:
    """Active, désactive ou modifie une capability sur un device.

    Exemples :
    - set_capability("desk", "run_terminal", True, auth_required=True)
    - set_capability("phone", "get_location", False)
    """
    device = await registry.get_by_name(device_name)
    if device is None:
        return {"error": f"Device '{device_name}' introuvable"}

    flag = CapabilityFlag(enabled=enabled, auth_required=auth_required)
    try:
        updated = await registry.set_single_capability(device.device_hash, capability, flag)
    except ValueError:
        return {"error": f"Device '{device_name}' introuvable"}

    return {
        "status": "ok",
        "device_name": updated.device_name,
        "capability": capability,
        "value": updated.capabilities[capability].model_dump(),
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
