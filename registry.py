"""
Gestion du registry des devices en SQLite (aiosqlite).

Ce module fournit une classe `Registry` qui encapsule toutes les opérations
de lecture/écriture sur la base de données :
- enregistrement / mise à jour des devices
- gestion des session tokens
- heartbeat et passage online/offline
- renommage et historique des noms
- gestion des capabilities
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import aiosqlite

import config
from models import (
    CapabilityFlag,
    Device,
    DeviceStatus,
    DeviceType,
    NetworkInfo,
    utcnow,
)

logger = logging.getLogger("hasan.registry")


SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_hash TEXT PRIMARY KEY,
    device_name TEXT NOT NULL UNIQUE,
    device_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'offline',
    last_seen TEXT NOT NULL,
    previous_names TEXT NOT NULL DEFAULT '[]',
    capabilities TEXT NOT NULL DEFAULT '{}',
    network TEXT NOT NULL DEFAULT '{}',
    version TEXT NOT NULL DEFAULT '0.0.0',
    capabilities_version TEXT NOT NULL DEFAULT '',
    session_token TEXT
);

CREATE INDEX IF NOT EXISTS idx_devices_session_token ON devices(session_token);
CREATE INDEX IF NOT EXISTS idx_devices_device_name ON devices(device_name);
"""


def compute_capabilities_version(capabilities: dict[str, Any]) -> str:
    """Calcule un hash stable représentant l'état des capabilities.

    Utilisé pour détecter côté device si ses capabilities ont changé
    depuis le dernier register (-> capabilities_refresh_needed).
    """
    serialized = json.dumps(capabilities, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class Registry:
    """Encapsule l'accès à la base SQLite contenant les devices."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or config.DATABASE_PATH
        # S'assure que le dossier parent existe
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    async def init(self) -> None:
        """Crée les tables si nécessaire. À appeler au démarrage."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            await db.commit()
        logger.info("Base de données initialisée : %s", self.db_path)

    # ------------------------------------------------------------------
    # Helpers de (dé)sérialisation
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_device(row: aiosqlite.Row) -> Device:
        capabilities_raw: dict[str, Any] = json.loads(row["capabilities"] or "{}")
        capabilities = {
            name: CapabilityFlag(**flag) for name, flag in capabilities_raw.items()
        }
        network_raw: dict[str, Any] = json.loads(row["network"] or "{}")
        return Device(
            device_hash=row["device_hash"],
            device_name=row["device_name"],
            device_type=DeviceType(row["device_type"]),
            status=DeviceStatus(row["status"]),
            last_seen=datetime.fromisoformat(row["last_seen"]),
            previous_names=json.loads(row["previous_names"] or "[]"),
            capabilities=capabilities,
            network=NetworkInfo(**network_raw),
            version=row["version"],
            capabilities_version=row["capabilities_version"],
            session_token=row["session_token"],
        )

    # ------------------------------------------------------------------
    # Enregistrement / mise à jour d'un device
    # ------------------------------------------------------------------

    async def register_device(
        self,
        device_hash: str,
        device_name: str,
        device_type: DeviceType,
        capabilities: dict[str, CapabilityFlag],
        version: str,
    ) -> tuple[Device, str]:
        """Enregistre un nouveau device ou met à jour un device existant.

        Retourne le device mis à jour ainsi que le nouveau session_token.
        """
        now = utcnow().isoformat()
        session_token = "tok_" + secrets.token_hex(32)
        capabilities_dict = {name: flag.model_dump() for name, flag in capabilities.items()}
        capabilities_version = compute_capabilities_version(capabilities_dict)

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            cursor = await db.execute(
                "SELECT * FROM devices WHERE device_hash = ?", (device_hash,)
            )
            existing = await cursor.fetchone()

            if existing is None:
                # Nouveau device : vérifie que le nom n'est pas déjà pris
                cursor = await db.execute(
                    "SELECT device_hash FROM devices WHERE device_name = ?",
                    (device_name,),
                )
                name_taken = await cursor.fetchone()
                if name_taken is not None:
                    # On suffixe le nom avec une partie du hash pour éviter le conflit
                    device_name = f"{device_name}_{device_hash[:6]}"
                    logger.warning(
                        "Nom déjà pris, renommage automatique en '%s'", device_name
                    )

                await db.execute(
                    """
                    INSERT INTO devices (
                        device_hash, device_name, device_type, status, last_seen,
                        previous_names, capabilities, network, version,
                        capabilities_version, session_token
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        device_hash,
                        device_name,
                        device_type.value,
                        DeviceStatus.online.value,
                        now,
                        json.dumps([]),
                        json.dumps(capabilities_dict),
                        json.dumps({}),
                        version,
                        capabilities_version,
                        session_token,
                    ),
                )
                logger.info("Nouveau device enregistré : %s (%s)", device_name, device_hash[:8])
            else:
                # Device existant : update (pas de doublon)
                await db.execute(
                    """
                    UPDATE devices
                    SET device_type = ?, status = ?, last_seen = ?,
                        capabilities = ?, version = ?, capabilities_version = ?,
                        session_token = ?
                    WHERE device_hash = ?
                    """,
                    (
                        device_type.value,
                        DeviceStatus.online.value,
                        now,
                        json.dumps(capabilities_dict),
                        version,
                        capabilities_version,
                        session_token,
                        device_hash,
                    ),
                )
                logger.info("Device mis à jour : %s (%s)", existing["device_name"], device_hash[:8])

            await db.commit()

            cursor = await db.execute(
                "SELECT * FROM devices WHERE device_hash = ?", (device_hash,)
            )
            row = await cursor.fetchone()
            return self._row_to_device(row), session_token

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def heartbeat(
        self, device_hash: str, capabilities_version: str, network: NetworkInfo
    ) -> tuple[bool, bool]:
        """Met à jour last_seen, status=online et les infos réseau.

        Retourne (device_existe, capabilities_refresh_needed).
        """
        now = utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT capabilities_version FROM devices WHERE device_hash = ?",
                (device_hash,),
            )
            row = await cursor.fetchone()
            if row is None:
                return False, False

            refresh_needed = row["capabilities_version"] != capabilities_version

            await db.execute(
                """
                UPDATE devices
                SET status = ?, last_seen = ?, network = ?
                WHERE device_hash = ?
                """,
                (
                    DeviceStatus.online.value,
                    now,
                    json.dumps(network.model_dump()),
                    device_hash,
                ),
            )
            await db.commit()
            return True, refresh_needed

    # ------------------------------------------------------------------
    # Lecture des devices
    # ------------------------------------------------------------------

    async def get_by_hash(self, device_hash: str) -> Optional[Device]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM devices WHERE device_hash = ?", (device_hash,)
            )
            row = await cursor.fetchone()
            return self._row_to_device(row) if row else None

    async def get_by_name(self, device_name: str) -> Optional[Device]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM devices WHERE device_name = ?", (device_name,)
            )
            row = await cursor.fetchone()
            return self._row_to_device(row) if row else None

    async def get_by_session_token(self, session_token: str) -> Optional[Device]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM devices WHERE session_token = ?", (session_token,)
            )
            row = await cursor.fetchone()
            return self._row_to_device(row) if row else None

    async def list_all(self) -> list[Device]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM devices ORDER BY device_name")
            rows = await cursor.fetchall()
            return [self._row_to_device(row) for row in rows]

    async def refresh_offline_status(self) -> None:
        """Marque comme offline tous les devices dont le dernier heartbeat
        date de plus de HEARTBEAT_INTERVAL * HEARTBEAT_TIMEOUT_MULTIPLIER secondes."""
        threshold = utcnow() - timedelta(
            seconds=config.HEARTBEAT_INTERVAL * config.HEARTBEAT_TIMEOUT_MULTIPLIER
        )
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT device_hash, device_name, last_seen FROM devices WHERE status = 'online'"
            )
            rows = await cursor.fetchall()
            for row in rows:
                last_seen = datetime.fromisoformat(row["last_seen"])
                if last_seen < threshold:
                    await db.execute(
                        "UPDATE devices SET status = 'offline' WHERE device_hash = ?",
                        (row["device_hash"],),
                    )
                    logger.info("Device passé offline (heartbeat expiré) : %s", row["device_name"])
            await db.commit()

    # ------------------------------------------------------------------
    # Renommage
    # ------------------------------------------------------------------

    async def rename_device(self, device_hash: str, new_name: str) -> Device:
        """Renomme un device et stocke l'ancien nom dans previous_names.

        Lève ValueError("not_found") ou ValueError("name_taken") en cas d'erreur.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            cursor = await db.execute(
                "SELECT * FROM devices WHERE device_hash = ?", (device_hash,)
            )
            row = await cursor.fetchone()
            if row is None:
                raise ValueError("not_found")

            if row["device_name"] == new_name:
                return self._row_to_device(row)

            cursor = await db.execute(
                "SELECT device_hash FROM devices WHERE device_name = ? AND device_hash != ?",
                (new_name, device_hash),
            )
            taken = await cursor.fetchone()
            if taken is not None:
                raise ValueError("name_taken")

            previous_names = json.loads(row["previous_names"] or "[]")
            previous_names.append(row["device_name"])

            await db.execute(
                "UPDATE devices SET device_name = ?, previous_names = ? WHERE device_hash = ?",
                (new_name, json.dumps(previous_names), device_hash),
            )
            await db.commit()

            cursor = await db.execute(
                "SELECT * FROM devices WHERE device_hash = ?", (device_hash,)
            )
            row = await cursor.fetchone()
            logger.info("Device renommé : %s -> %s", previous_names[-1], new_name)
            return self._row_to_device(row)

    # ------------------------------------------------------------------
    # Capabilities
    # ------------------------------------------------------------------

    async def update_capabilities(
        self, device_hash: str, capabilities: dict[str, CapabilityFlag], merge: bool = True
    ) -> Device:
        """Met à jour les capabilities d'un device.

        Si merge=True, fusionne avec les capabilities existantes (par défaut).
        Met à jour capabilities_version pour déclencher capabilities_refresh_needed
        au prochain heartbeat.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            cursor = await db.execute(
                "SELECT * FROM devices WHERE device_hash = ?", (device_hash,)
            )
            row = await cursor.fetchone()
            if row is None:
                raise ValueError("not_found")

            current = json.loads(row["capabilities"] or "{}")
            old_capabilities = json.loads(json.dumps(current))  # copie pour le log

            new_caps = {name: flag.model_dump() for name, flag in capabilities.items()}
            if merge:
                current.update(new_caps)
            else:
                current = new_caps

            new_version = compute_capabilities_version(current)

            await db.execute(
                "UPDATE devices SET capabilities = ?, capabilities_version = ? WHERE device_hash = ?",
                (json.dumps(current), new_version, device_hash),
            )
            await db.commit()

            logger.info(
                "Capabilities mises à jour pour %s : %s -> %s",
                row["device_name"],
                old_capabilities,
                current,
            )

            cursor = await db.execute(
                "SELECT * FROM devices WHERE device_hash = ?", (device_hash,)
            )
            row = await cursor.fetchone()
            return self._row_to_device(row)

    async def set_single_capability(
        self, device_hash: str, capability_name: str, flag: CapabilityFlag
    ) -> Device:
        """Ajoute ou modifie une seule capability."""
        return await self.update_capabilities(device_hash, {capability_name: flag}, merge=True)

    async def delete_capability(self, device_hash: str, capability_name: str) -> Device:
        """Supprime complètement une capability d'un device."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            cursor = await db.execute(
                "SELECT * FROM devices WHERE device_hash = ?", (device_hash,)
            )
            row = await cursor.fetchone()
            if row is None:
                raise ValueError("not_found")

            current = json.loads(row["capabilities"] or "{}")
            if capability_name not in current:
                raise ValueError("capability_not_found")

            old_value = current.pop(capability_name)
            new_version = compute_capabilities_version(current)

            await db.execute(
                "UPDATE devices SET capabilities = ?, capabilities_version = ? WHERE device_hash = ?",
                (json.dumps(current), new_version, device_hash),
            )
            await db.commit()

            logger.info(
                "Capability '%s' supprimée pour %s (ancienne valeur : %s)",
                capability_name,
                row["device_name"],
                old_value,
            )

            cursor = await db.execute(
                "SELECT * FROM devices WHERE device_hash = ?", (device_hash,)
            )
            row = await cursor.fetchone()
            return self._row_to_device(row)


# Instance globale partagée par l'application
registry = Registry()
