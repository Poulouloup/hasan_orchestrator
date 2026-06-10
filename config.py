"""
Configuration de l'orchestrateur.
Charge les variables depuis le fichier .env (ou l'environnement système).
"""

import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

# Charge le fichier .env situé à la racine du projet
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _get_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


# Clé d'administration utilisée par Hermes pour les endpoints /api/* et MCP
ORCHESTRATOR_ADMIN_KEY: str = os.getenv("ORCHESTRATOR_ADMIN_KEY", "")
if not ORCHESTRATOR_ADMIN_KEY:
    # Génère une clé temporaire si absente (utile en dev, mais à fixer en prod)
    ORCHESTRATOR_ADMIN_KEY = secrets.token_hex(32)

# Hôte et ports
ORCHESTRATOR_HOST: str = os.getenv("ORCHESTRATOR_HOST", "127.0.0.1")
ORCHESTRATOR_PORT: int = int(os.getenv("ORCHESTRATOR_PORT", "8080"))
MCP_PORT: int = int(os.getenv("MCP_PORT", "8643"))

# Paramètres de la file de commandes et du polling
COMMAND_TTL_SECONDS: int = int(os.getenv("COMMAND_TTL_SECONDS", "30"))
HEARTBEAT_INTERVAL: int = int(os.getenv("HEARTBEAT_INTERVAL", "30"))
HEARTBEAT_TIMEOUT_MULTIPLIER: int = int(os.getenv("HEARTBEAT_TIMEOUT_MULTIPLIER", "2"))
LONG_POLL_TIMEOUT: int = int(os.getenv("LONG_POLL_TIMEOUT", "55"))

# Timeout d'attente du résultat d'une commande (côté MCP)
COMMAND_RESULT_TIMEOUT: int = int(os.getenv("COMMAND_RESULT_TIMEOUT", "25"))
COMMAND_POLL_INTERVAL_MS: int = int(os.getenv("COMMAND_POLL_INTERVAL_MS", "500"))

# Base de données SQLite
DATABASE_PATH: str = os.path.expanduser(
    os.getenv("DATABASE_PATH", "~/.hasan-orchestrator/registry.db")
)

# Niveau de log
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
