# Hasan Orchestrator

Orchestrateur MCP multi-devices en Python/FastAPI faisant le lien entre
**Hermes** (assistant vocal local) et plusieurs appareils (mobile, desktop,
laptop) via une API REST avec polling.

```
Hermes (localhost) → MCP localhost:8643
                          ↓
                    Orchestrateur FastAPI (localhost:8080)
                          ↓ HTTPS + Bearer token
              ┌───────────┼───────────┐
           "phone"     "desk"      "laptop"
         (polling)   (polling)   (polling)
```

Les devices initient **toujours** la connexion (register, heartbeat, long
polling). L'orchestrateur ne contacte jamais directement un device.

---

## 1. Installation

```bash
cd ~/hasan-orchestrator
./install.sh
```

Le script :
1. Crée un environnement virtuel Python (`venv/`)
2. Installe les dépendances (`requirements.txt`)
3. Crée `~/.hasan-orchestrator/` pour la base SQLite (`registry.db`)
4. Génère `ORCHESTRATOR_ADMIN_KEY` dans `.env` si absent
5. Installe et active le service systemd utilisateur `hasan-orchestrator.service`

Pour une installation manuelle :

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# éditer .env et définir ORCHESTRATOR_ADMIN_KEY (openssl rand -hex 32)
python main.py
```

L'API REST écoute par défaut sur `http://127.0.0.1:8080`.

---

## 2. Configuration Hermes

Ajouter dans `~/.hermes/config.yaml` :

### Option A — MCP en stdio (recommandé)

```yaml
mcp:
  servers:
    - name: hasan-orchestrator
      type: stdio
      command: /chemin/vers/hasan-orchestrator/venv/bin/python
      args: ["/chemin/vers/hasan-orchestrator/mcp_server.py"]
      description: "Contrôle multi-devices (phone, desk, laptop)"
```

> Le serveur MCP en stdio doit être lancé alors que l'orchestrateur FastAPI
> tourne déjà (il s'appuie sur l'API interne `/internal/*` pour créer les
> commandes et lire les résultats).

### Option B — MCP exposé via HTTP

```yaml
mcp:
  servers:
    - name: hasan-orchestrator
      url: "http://localhost:8643/mcp"
      auth:
        type: bearer
        token: "${ORCHESTRATOR_ADMIN_KEY}"
```

---

## 3. Connecter un nouveau device

Chaque device (agent mobile/desktop/laptop) doit :

1. **Calculer un `device_hash` unique et immuable** — un SHA256 généré une
   seule fois (par exemple à partir d'un identifiant matériel + sel
   aléatoire) et conservé localement.

2. **S'enregistrer** :

```bash
curl -X POST http://localhost:8080/register \
  -H "Content-Type: application/json" \
  -d '{
    "device_name": "phone",
    "device_hash": "a3f2c8...",
    "device_type": "mobile_agent",
    "version": "1.0.0",
    "capabilities": {
      "send_sms":     {"enabled": true,  "auth_required": false},
      "make_call":    {"enabled": true,  "auth_required": true},
      "screenshot":   {"enabled": true,  "auth_required": false},
      "get_battery":  {"enabled": true,  "auth_required": false}
    }
  }'
```

Réponse :

```json
{
  "status": "registered",
  "session_token": "tok_...",
  "heartbeat_interval": 30,
  "polling_interval": 30,
  "server_time": "..."
}
```

3. **Conserver `session_token`** et l'utiliser dans le header
   `Authorization: Bearer tok_...` pour tous les appels suivants.

4. **Envoyer un heartbeat** toutes les `heartbeat_interval` secondes :

```bash
curl -X POST http://localhost:8080/heartbeat \
  -H "Authorization: Bearer tok_..." \
  -H "Content-Type: application/json" \
  -d '{
    "device_hash": "a3f2c8...",
    "capabilities_version": "<hash renvoyé/calculé>",
    "network": {"ip": "192.168.1.42", "transport": "https", "nat": true, "carrier": "WiFi"}
  }'
```

Si `capabilities_refresh_needed: true` est retourné, refaire un `/register`
complet (les capabilities ont été modifiées côté orchestrateur).

5. **Boucler sur `GET /commands`** (long polling, jusqu'à 55s) pour recevoir
   les commandes à exécuter, puis poster le résultat sur `/results`.

Un device sans heartbeat depuis `2 × heartbeat_interval` est marqué `offline`.

---

## 4. Tools MCP disponibles

| Tool | Description |
|---|---|
| `device_list()` | Liste les devices online et leurs capabilities activées |
| `device_info(device_name)` | Détails complets d'un device |
| `send_sms(numero, message, device_name?)` | Envoie un SMS depuis un mobile |
| `make_call(numero, device_name?)` | Lance un appel téléphonique |
| `get_location(device_name?)` | Récupère la position GPS |
| `screenshot(device_name?)` | Capture d'écran |
| `open_file(path, device_name?)` | Ouvre un fichier |
| `run_terminal(command, device_name?)` | Exécute une commande terminal (confirmation requise) |
| `launch_app(app_name, device_name?)` | Lance une application |
| `get_battery(device_name?)` | Niveau de batterie |
| `set_volume(level, device_name?)` | Règle le volume (0-100) |
| `send_notification(title, message, device_name?)` | Notification push |
| `set_capability(device_name, capability, enabled, auth_required?)` | Active/désactive une capability |

Si `device_name` n'est pas précisé, l'orchestrateur choisit automatiquement
l'unique device online disposant de la capability demandée. S'il y en a
plusieurs, il demande de préciser.

---

## 5. Exemples de commandes vocales pour Hermes

- "Liste mes appareils"
- "Prends un screenshot du desk"
- "Envoie un SMS au 06 12 34 56 78 depuis le phone : *je serai en retard*"
- "Quel est le niveau de batterie du phone ?"
- "Lance Spotify sur le laptop"
- "Mets le volume à 50 sur le desk"
- "Ouvre le fichier rapport.pdf sur le laptop"
- "Active la capability run_terminal sur desk"
- "Désactive get_location sur phone"

---

## 6. Gestion des capabilities depuis l'orchestrateur (admin)

Modifier (merge) les capabilities d'un device :

```bash
curl -X PATCH http://localhost:8080/api/devices/phone/capabilities \
  -H "Authorization: Bearer ${ORCHESTRATOR_ADMIN_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"run_terminal": {"enabled": true, "auth_required": true}}'
```

Ajouter/modifier une seule capability :

```bash
curl -X POST http://localhost:8080/api/devices/desk/capabilities/run_terminal \
  -H "Authorization: Bearer ${ORCHESTRATOR_ADMIN_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"enabled": true, "auth_required": true}'
```

Supprimer une capability :

```bash
curl -X DELETE http://localhost:8080/api/devices/phone/capabilities/get_location \
  -H "Authorization: Bearer ${ORCHESTRATOR_ADMIN_KEY}"
```

Au prochain heartbeat du device concerné, `capabilities_refresh_needed: true`
sera retourné et le device devra refaire un `/register` complet.

---

## 7. Endpoints admin (Hermes)

| Endpoint | Description |
|---|---|
| `GET /api/devices` | Liste tous les devices et leur statut |
| `GET /api/devices/{device_name}` | Détails d'un device |
| `GET /api/commands/{command_id}` | Statut d'une commande |
| `PATCH /api/devices/{device_name}/capabilities` | Merge de capabilities |
| `POST /api/devices/{device_name}/capabilities/{capability_name}` | Ajoute/modifie une capability |
| `DELETE /api/devices/{device_name}/capabilities/{capability_name}` | Supprime une capability |

Tous nécessitent `Authorization: Bearer ${ORCHESTRATOR_ADMIN_KEY}`.

---

## 8. Ajouter un nouveau type d'appareil

1. Choisir un `device_type` parmi `mobile_agent`, `desktop_agent`,
   `laptop_agent` (ou étendre l'enum `DeviceType` dans `models.py` pour un
   nouveau type, ex. `tablet_agent`).
2. Implémenter côté agent : `/register`, `/heartbeat`, boucle `GET /commands`
   + `POST /results` (+ `/confirm` si des capabilities `auth_required`).
3. Définir les `capabilities` pertinentes pour ce type d'appareil (un sous-
   ensemble de la liste standard, ou de nouvelles actions — dans ce cas,
   ajouter le tool MCP correspondant dans `mcp_server.py`).
4. Enregistrer le device : il apparaîtra automatiquement dans
   `device_list()` et sera routable par nom ou par capability.

---

## 9. Logs

```bash
journalctl --user -u hasan-orchestrator -f
```

Niveau de log configurable via `LOG_LEVEL` dans `.env` (`DEBUG`, `INFO`,
`ERROR`).

---

## 10. Architecture des fichiers

```
hasan-orchestrator/
  main.py           # FastAPI : endpoints REST devices + admin
  registry.py       # Gestion SQLite des devices (registry persistant)
  command_queue.py  # File de commandes en mémoire avec TTL
  mcp_server.py     # Serveur MCP (stdio) exposé à Hermes
  auth.py           # Validation des Bearer tokens (admin + sessions device)
  models.py         # Schémas Pydantic
  config.py         # Configuration (.env)
  install.sh        # Installation + service systemd
  requirements.txt
  .env.example
```
