# PrivadoVPN SOCKS5 Proxy Container

A Docker container that runs PrivadoVPN CLI and exposes a SOCKS5 proxy bound to the VPN tunnel interface (`tun0`). Zero host routing changes required.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Host Machine                                               │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  Docker Container (privado-socks)                     │  │
│  │  ┌──────────────┐     ┌────────────────────────────┐  │  │
│  │  │ PrivadoVPN   │────▶│  tun0 (10.x.x.x)           │  │  │
│  │  │ CLI          │     │                            │  │  │
│  │  └──────────────┘     └─────────────┬──────────────┘  │  │
│  │                                      │                 │  │
│  │  ┌───────────────────────────────────▼──────────────┐  │  │
│  │  │  SOCKS5 Server (asyncio, stdlib only)            │  │  │
│  │  │  Listen: 0.0.0.0:1080                            │  │  │
│  │  │  Outbound bind: tun0 IP                          │  │  │
│  │  └──────────────────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────────────┘  │
│                         │                                     │
│                   127.0.0.1:1080                              │
└─────────────────────────┼─────────────────────────────────────┘
                          ▼
              Any app configured to use
              socks5h://127.0.0.1:1080
```

## Features

- **Zero host routing changes** — VPN tunnel stays inside container
- **Auto-reconnect** — Watchdog monitors `tun0`, restarts VPN + SOCKS5 on drop
- **Privacy-preserving** — SOCKS5 outbound traffic forced through `tun0` IP
- **Stdlib only** — No Python dependencies beyond standard library
- **Cached Docker layers** — Dependencies downloaded once, not on every rebuild

## Quick Start

```bash
# 1. Clone and enter
git clone https://github.com/ignatius00/privado-socks.git
cd privado-socks

# 2. Configure credentials
cp .env.example .env
# Edit .env with your PrivadoVPN username/password

# 3. Build and run
docker compose up -d --build

# 4. Test
curl --proxy socks5h://127.0.0.1:1080 https://api.ipify.org && echo
# Should return your PrivadoVPN exit IP
```

## Usage with SearXNG / Other Apps

Point any SOCKS5-capable app to `socks5h://127.0.0.1:1080` (the `h` suffix means DNS resolution happens through the proxy).

Example `docker-compose.yml` for SearXNG:

```yaml
services:
  searxng:
    image: searxng/searxng:latest
    environment:
      - SEARXNG_PROXY_URL=socks5h://host.docker.internal:1080
    # ... rest of config
```

## Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PRIVADO_USERNAME` | Yes | — | PrivadoVPN account username |
| `PRIVADO_PASSWORD` | Yes | — | PrivadoVPN account password |
| `PRIVADO_CMD` | No | `privado connect --auto` | Custom connect command |

## How It Works

1. **Entrypoint** validates `/dev/net/tun` and `NET_ADMIN` capability
2. **Watchdog** starts PrivadoVPN CLI → creates `tun0` interface
3. **Watchdog** reads `tun0` IP (e.g., `10.8.0.2`)
4. **SOCKS5 server** starts, binds outbound sockets to `tun0` IP via `socket.bind((tun0_ip, 0))`
5. **Watchdog** polls `tun0` every 5s; on drop, kills SOCKS5, restarts VPN, restarts SOCKS5 with new IP

## Security Notes

- SOCKS5 binds to `0.0.0.0:1080` inside container, but docker-compose maps only `127.0.0.1:1080`
- No authentication on SOCKS5 by default — rely on Docker port binding for access control
- Container runs as non-root user (`appuser`, uid 1000)
- Requires `--cap-add=NET_ADMIN --device=/dev/net/tun` — standard for VPN containers

## Building Without Re-downloading Deps

The Dockerfile uses layered caching:

```dockerfile
# System deps (cached)
RUN apt-get update && apt-get install -y ...

# PrivadoVPN CLI (cached)
RUN curl -fsSL https://linux.privadovpn.com/install.sh | bash

# Python deps (cached - requirements.txt is empty, stdlib only)
COPY requirements.txt .
RUN pip install -r requirements.txt

# App code (rebuilt on changes)
COPY *.py ./
```

After first build, subsequent `docker compose up -d --build` only re-copies Python files (~instant).