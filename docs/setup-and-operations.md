# Setup and Operations

This document covers technical setup, deployment, troubleshooting, and development operations.

## Requirements

- Ubuntu 24.04 VPS (or similar Linux host)
- Docker Engine + Docker Compose plugin
- Domain name for TLS (recommended for Claude.ai remote MCP)
- Anthropic API key
- Markdown vault structure:
  - `raw/`
  - `wiki/`
  - `manifest.json`
  - `CLAUDE.md`

Recommended baseline VPS (what this project was tested on): DigitalOcean Basic Droplet with 1 vCPU, 2 GB RAM, 50 GB SSD, and 2 TB transfer. Ballpark cost is about $15/month with backups.

## Project layout

- `src/corpus_manager_mcp/server.py` - MCP server implementation
- `docker-compose.yml` - deployment stack
- `.env.example` - required environment variables
- `skill/` - uploadable Claude Skill files

## Deploy on VPS

### 1) Clone and prepare directories

```bash
sudo mkdir -p /opt
cd /opt

# Use your repository URL from GitHub.
sudo git clone <your-github-repo-url> corpus-manager
cd /opt/corpus-manager

cp .env.example .env
sudo mkdir -p /srv/vault
sudo mkdir -p /srv/vaults
sudo touch /opt/corpus-manager/corpus-registry.yaml
sudo mkdir -p /opt/corpus-manager/syncthing-config
sudo chown -R 1000:1000 /srv/vault /srv/vaults /opt/corpus-manager/syncthing-config /opt/corpus-manager/corpus-registry.yaml
sudo chmod -R 775 /srv/vault /srv/vaults /opt/corpus-manager/syncthing-config
```

### 2) Configure `.env`

```env
VAULT_ROOT=/data/vault
CLAUDE_MD_PATH=/data/vault/CLAUDE.md
HOST_VAULT_PATH=/srv/vault
HOST_VAULTS_PATH=/srv/vaults
HOST_CORPUS_REGISTRY_PATH=/opt/corpus-manager/corpus-registry.yaml
CORPUS_REGISTRY_PATH=/data/corpus-registry.yaml
HOST_SYNCTHING_CONFIG_PATH=/opt/corpus-manager/syncthing-config

ANTHROPIC_API_KEY=replace-me
ANTHROPIC_MODEL=claude-sonnet-4-5
ANTHROPIC_QUERY_MODEL=claude-sonnet-4-5
# Optional: model used by ingest/deprecate. Defaults to ANTHROPIC_MODEL.
ANTHROPIC_INGEST_MODEL=claude-opus-4-7
# Optional: ingest agent loop budgets.
INGEST_LOOP_MAX_TURNS=35
INGEST_LOOP_MAX_TOKENS=8192

HOST=0.0.0.0
PORT=8765
MCP_PATH=/mcp
MCP_BEARER_TOKEN=replace-me
```

Generate a bearer token:

```bash
openssl rand -hex 32
```

### 3) Start services

```bash
cd /opt/corpus-manager
docker compose up -d --build
docker compose ps
```

Expected services:

- `corpus-manager-mcp`
- `corpus-syncthing`

## TLS reverse proxy

Use a domain such as `mcp.example.com` pointing to your VPS public IP.

### Caddy (recommended)

```bash
sudo apt update
sudo apt install -y caddy
sudo tee /etc/caddy/Caddyfile >/dev/null <<'EOF'
mcp.example.com {
  reverse_proxy 127.0.0.1:8765
}
EOF
sudo systemctl reload caddy
```

MCP URL:

`https://mcp.example.com/mcp`

### Nginx alternative

```nginx
server {
    listen 80;
    server_name mcp.example.com;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Then issue TLS certs with certbot.

## Claude.ai setup

1. Add remote MCP server URL: `https://mcp.example.com/mcp`
2. Upload skill package from `skill/` in Claude customization settings.

## Initial wiki bootstrapping

If this is a brand-new vault, run your first `ingest` after setup. When there are no captured files yet, Corpus Manager can initialize a generic starter wiki structure, optionally guided by a topic you provide.

Important:

- Do **not** point this at an existing hand-curated wiki you want preserved unchanged. Ingest is a reconciliation workflow and may overwrite/restructure pages.
- If you have an existing wiki you want to preserve, migrate/copy source material into `raw/` and ingest from there.

> WARNING! Initial ingest cost may be expensive. Ballpark: 100k to 1M+ tokens. No guarantees are made, so be sure you have appropriate token limits in place before running an ingest.

## Multi-vault setup

Corpus Manager can manage multiple vault folders from one install. Registry entries map each `vault_id` to a vault root.

Example `corpus-registry.yaml`:

```yaml
corpora:
  main:
    vault_root: /data/vault
    label: "Primary vault"
  jungle:
    vault_root: /data/vaults/jungle
    label: "The Jungle"
```

When multiple vaults are configured, MCP calls must include `vault_id`.

### Bootstrap a new vault entry

Host:

```bash
cd /opt/corpus-manager
python3 scripts/new-vault.py
```

Container:

```bash
cd /opt/corpus-manager
docker compose run --rm corpus-manager-mcp python3 scripts/new-vault.py
```

After bootstrap:

1. Add the new folder to Syncthing on each device.
2. Restart the Corpus Manager container so it reloads registry YAML.

## Syncthing setup

### VPS side

- Open Syncthing UI at `http://<VPS-IP>:8384`
- Folder paths inside Syncthing container:
  - `/var/syncthing/Vault` (legacy primary vault)
  - `/var/syncthing/Vaults/<vault-id>` (additional vaults)

### Mac side

- Add your local vault path.
- Pair devices and accept shared folder.

### iPhone side

- Use Möbius Sync (or equivalent iOS Syncthing client).
- Pair with VPS and map the same vault folder.
- Open synced folder in Obsidian iOS.

## Common operations

### Pull latest and redeploy

(Better to set up your own git fork, rather than pulling from this one)

```bash
cd /opt/corpus-manager
git pull
docker compose up -d --build
```

### Rebuild only Corpus Manager service

```bash
cd /opt/corpus-manager
docker compose up -d --build --force-recreate corpus-manager-mcp
```

### Check logs

```bash
docker logs --tail 100 corpus-manager-mcp
docker logs --tail 100 corpus-syncthing
```

## Troubleshooting

- **MCP reachable but returns 406 with curl**
  - Normal for raw curl requests to streamable MCP endpoints.
- **Syncthing cannot create cert.pem / permission denied**
  - Ensure host folders are writable by UID/GID `1000:1000`:
    - `/srv/vault`
    - `/opt/corpus-manager/syncthing-config`
- **Tool list in Claude.ai appears stale**
  - Rebuild/restart container and reconnect MCP integration in Claude.ai.
- **Ingest progress confusion**
  - `ingest` is start-or-report. If a job is running, calling `ingest` returns status instead of starting another run.

## Development notes

- Main runtime entrypoint: `src/corpus_manager_mcp/server.py`
- Rebuild skill zip after editing `skill/SKILL.md`:

```bash
cd skill
zip -r corpus-manager-skill.zip SKILL.md
```

- Quick local syntax check:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -c "import ast; ast.parse(open('src/corpus_manager_mcp/server.py').read()); print('ok')"
```
