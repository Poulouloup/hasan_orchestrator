"""
File de commandes en mémoire avec TTL.

Les commandes sont créées par les tools MCP, consommées par les devices via
/commands (long polling) puis complétées via /results ou /confirm.

Le stockage est en mémoire (dict) car la durée de vie d'une commande est très
courte (TTL par défaut 30s) : pas besoin de persistance lourde. Un accès
concurrent est protégé par un verrou asyncio.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import timedelta
from typing import Optional

import config
from models import Command, CommandStatus, utcnow

logger = logging.getLogger("hasan.queue")


class CommandQueue:
    """Gère les commandes en attente, leur cycle de vie et leur TTL."""

    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}
        self._lock = asyncio.Lock()
        # Événements utilisés pour réveiller immédiatement les long-pollers
        # quand une nouvelle commande arrive pour un device donné.
        self._device_events: dict[str, asyncio.Event] = {}

    # ------------------------------------------------------------------
    # Création
    # ------------------------------------------------------------------

    async def create_command(
        self,
        device_hash: str,
        action: str,
        params: dict,
        requires_confirmation: bool = False,
        ttl_seconds: Optional[int] = None,
    ) -> Command:
        """Crée une nouvelle commande dans la file pour un device donné."""
        ttl = ttl_seconds if ttl_seconds is not None else config.COMMAND_TTL_SECONDS
        now = utcnow()
        command = Command(
            command_id="cmd_" + uuid.uuid4().hex,
            device_hash=device_hash,
            action=action,
            params=params,
            status=(
                CommandStatus.awaiting_confirmation
                if requires_confirmation
                else CommandStatus.pending
            ),
            created_at=now,
            expires_at=now + timedelta(seconds=ttl),
        )

        async with self._lock:
            self._commands[command.command_id] = command

        logger.info(
            "Commande créée : %s action=%s device=%s status=%s",
            command.command_id,
            action,
            device_hash[:8],
            command.status.value,
        )

        self._notify_device(device_hash)
        return command

    # ------------------------------------------------------------------
    # Notification des long-pollers
    # ------------------------------------------------------------------

    def _notify_device(self, device_hash: str) -> None:
        event = self._device_events.get(device_hash)
        if event is not None:
            event.set()

    def _get_or_create_event(self, device_hash: str) -> asyncio.Event:
        event = self._device_events.get(device_hash)
        if event is None:
            event = asyncio.Event()
            self._device_events[device_hash] = event
        return event

    # ------------------------------------------------------------------
    # Récupération des commandes pour un device (GET /commands)
    # ------------------------------------------------------------------

    async def get_pending_for_device(self, device_hash: str) -> list[Command]:
        """Retourne les commandes pending/awaiting_confirmation non expirées
        pour ce device."""
        await self._expire_old_commands()
        async with self._lock:
            return [
                cmd
                for cmd in self._commands.values()
                if cmd.device_hash == device_hash
                and cmd.status in (CommandStatus.pending, CommandStatus.awaiting_confirmation)
            ]

    async def wait_for_commands(self, device_hash: str, timeout: float) -> list[Command]:
        """Long polling : attend jusqu'à `timeout` secondes qu'une commande
        soit disponible pour ce device, ou retourne immédiatement si une
        commande est déjà présente."""
        commands = await self.get_pending_for_device(device_hash)
        if commands:
            return commands

        event = self._get_or_create_event(device_hash)
        event.clear()
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

        return await self.get_pending_for_device(device_hash)

    # ------------------------------------------------------------------
    # Résultats / confirmation
    # ------------------------------------------------------------------

    async def get_command(self, command_id: str) -> Optional[Command]:
        async with self._lock:
            return self._commands.get(command_id)

    async def set_result(
        self, command_id: str, status: CommandStatus, data: Optional[dict], error: Optional[str]
    ) -> Optional[Command]:
        """Enregistre le résultat d'une commande renvoyé par un device."""
        async with self._lock:
            command = self._commands.get(command_id)
            if command is None:
                return None

            command.status = status
            if status == CommandStatus.failed:
                command.result = {"error": error} if error else (data or {})
            else:
                command.result = data or {}

            logger.info("Résultat reçu pour %s : status=%s", command_id, status.value)
            return command

    async def confirm_command(self, command_id: str, approved: bool) -> Optional[Command]:
        """Traite une réponse de confirmation (auth_required)."""
        async with self._lock:
            command = self._commands.get(command_id)
            if command is None:
                return None

            if command.status != CommandStatus.awaiting_confirmation:
                return command

            command.confirmed_at = utcnow()
            if approved:
                command.status = CommandStatus.pending
                logger.info("Commande confirmée par l'utilisateur : %s", command_id)
            else:
                command.status = CommandStatus.failed
                command.result = {"error": "refused by user"}
                logger.info("Commande refusée par l'utilisateur : %s", command_id)

        if approved:
            self._notify_device(command.device_hash)

        return command

    # ------------------------------------------------------------------
    # Attente du résultat (côté MCP / orchestrateur)
    # ------------------------------------------------------------------

    async def wait_for_result(
        self, command_id: str, timeout: float, poll_interval_ms: int
    ) -> Optional[Command]:
        """Attend qu'une commande passe en status terminal (done/failed/expired).

        Polling interne toutes les `poll_interval_ms` millisecondes.
        Retourne la commande dans son état final, ou None si elle n'existe pas.
        """
        elapsed = 0.0
        interval = poll_interval_ms / 1000.0

        while elapsed < timeout:
            command = await self.get_command(command_id)
            if command is None:
                return None

            if command.status in (CommandStatus.done, CommandStatus.failed, CommandStatus.expired):
                return command

            if utcnow() >= command.expires_at:
                async with self._lock:
                    command.status = CommandStatus.expired
                return command

            await asyncio.sleep(interval)
            elapsed += interval

        # Timeout atteint sans réponse
        async with self._lock:
            command = self._commands.get(command_id)
            if command and command.status not in (
                CommandStatus.done,
                CommandStatus.failed,
                CommandStatus.expired,
            ):
                command.status = CommandStatus.expired
        return command

    # ------------------------------------------------------------------
    # Nettoyage
    # ------------------------------------------------------------------

    async def _expire_old_commands(self) -> None:
        """Marque comme expirées les commandes dont le TTL est dépassé."""
        now = utcnow()
        async with self._lock:
            for command in self._commands.values():
                if (
                    command.status in (CommandStatus.pending, CommandStatus.awaiting_confirmation)
                    and now >= command.expires_at
                ):
                    command.status = CommandStatus.expired
                    logger.debug("Commande expirée : %s", command.command_id)

    async def cleanup_loop(self, interval_seconds: int = 60) -> None:
        """Tâche de fond : purge périodiquement les vieilles commandes terminées
        pour éviter une croissance illimitée de la mémoire."""
        while True:
            await asyncio.sleep(interval_seconds)
            await self._expire_old_commands()
            cutoff = utcnow() - timedelta(minutes=10)
            async with self._lock:
                to_remove = [
                    cid
                    for cid, cmd in self._commands.items()
                    if cmd.status in (CommandStatus.done, CommandStatus.failed, CommandStatus.expired)
                    and cmd.created_at < cutoff
                ]
                for cid in to_remove:
                    del self._commands[cid]
            if to_remove:
                logger.debug("Purge de %d commande(s) terminée(s) depuis >10min", len(to_remove))


# Instance globale partagée par l'application
command_queue = CommandQueue()
