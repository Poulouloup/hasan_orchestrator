#!/usr/bin/env bash
#
# Script d'installation de l'orchestrateur Hasan.
# Cible : Ubuntu 24.04 (PC fixe Lenovo H520S, i3-3220, 8 Go RAM)
#
# Étapes :
#   1. Création du venv Python
#   2. Installation des dépendances
#   3. Création de ~/.hasan-orchestrator/ pour la base SQLite
#   4. Génération de ORCHESTRATOR_ADMIN_KEY si absent
#   5. Installation du service systemd utilisateur
#   6. Activation et démarrage du service

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${PROJECT_DIR}/venv"
DATA_DIR="${HOME}/.hasan-orchestrator"
ENV_FILE="${PROJECT_DIR}/.env"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"
SERVICE_FILE="${SYSTEMD_USER_DIR}/hasan-orchestrator.service"

echo "==> Installation de l'orchestrateur Hasan dans ${PROJECT_DIR}"

# ---------------------------------------------------------------------------
# 1. Création du venv Python
# ---------------------------------------------------------------------------
if [ ! -d "${VENV_DIR}" ]; then
    echo "==> Création de l'environnement virtuel Python (${VENV_DIR})"
    python3 -m venv "${VENV_DIR}"
else
    echo "==> Environnement virtuel déjà présent, on le réutilise"
fi

# ---------------------------------------------------------------------------
# 2. Installation des dépendances
# ---------------------------------------------------------------------------
echo "==> Installation des dépendances Python"
"${VENV_DIR}/bin/pip" install --upgrade pip
"${VENV_DIR}/bin/pip" install -r "${PROJECT_DIR}/requirements.txt"

# ---------------------------------------------------------------------------
# 3. Création du dossier de données
# ---------------------------------------------------------------------------
echo "==> Création du dossier de données ${DATA_DIR}"
mkdir -p "${DATA_DIR}"

# ---------------------------------------------------------------------------
# 4. Génération de ORCHESTRATOR_ADMIN_KEY si absent
# ---------------------------------------------------------------------------
if [ ! -f "${ENV_FILE}" ]; then
    echo "==> Création du fichier .env à partir de .env.example"
    cp "${PROJECT_DIR}/.env.example" "${ENV_FILE}"
fi

if ! grep -q "^ORCHESTRATOR_ADMIN_KEY=.\+" "${ENV_FILE}"; then
    echo "==> Génération d'une clé ORCHESTRATOR_ADMIN_KEY"
    NEW_KEY="$(openssl rand -hex 32)"
    if grep -q "^ORCHESTRATOR_ADMIN_KEY=" "${ENV_FILE}"; then
        sed -i "s|^ORCHESTRATOR_ADMIN_KEY=.*|ORCHESTRATOR_ADMIN_KEY=${NEW_KEY}|" "${ENV_FILE}"
    else
        echo "ORCHESTRATOR_ADMIN_KEY=${NEW_KEY}" >> "${ENV_FILE}"
    fi
    echo "    Clé générée et écrite dans ${ENV_FILE}"
else
    echo "==> ORCHESTRATOR_ADMIN_KEY déjà configurée, on ne la modifie pas"
fi

# ---------------------------------------------------------------------------
# 5. Installation du service systemd utilisateur
# ---------------------------------------------------------------------------
echo "==> Installation du service systemd utilisateur"
mkdir -p "${SYSTEMD_USER_DIR}"

cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=Hasan Orchestrator - Orchestrateur multi-devices pour Hermes
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${PROJECT_DIR}
ExecStart=${VENV_DIR}/bin/python ${PROJECT_DIR}/main.py
Restart=on-failure
RestartSec=5
EnvironmentFile=${ENV_FILE}

[Install]
WantedBy=default.target
EOF

echo "    Service écrit dans ${SERVICE_FILE}"

# ---------------------------------------------------------------------------
# 6. Activation et démarrage du service
# ---------------------------------------------------------------------------
echo "==> Activation et démarrage du service"
systemctl --user daemon-reload
systemctl --user enable hasan-orchestrator.service
systemctl --user restart hasan-orchestrator.service

echo ""
echo "==> Installation terminée."
echo ""
echo "Vérifier le statut       : systemctl --user status hasan-orchestrator"
echo "Voir les logs            : journalctl --user -u hasan-orchestrator -f"
echo "Clé admin (.env)         : ${ENV_FILE}"
echo ""
echo "Pour que le service démarre même sans session ouverte :"
echo "  sudo loginctl enable-linger \$USER"
