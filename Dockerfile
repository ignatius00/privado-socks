# PrivacyVPN + SOCKS5 Docker image
# Base: Ubuntu 22.04 (has PrivadoVPN CLI in apt)
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# Install: PrivadoVPN CLI, Python3, iproute2 (for ip command), curl (for health checks)
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    iproute2 \
    curl \
    ca-certificates \
    gnupg \
    lsb-release \
    && rm -rf /var/lib/apt/lists/*

# Install PrivadoVPN CLI from their repo
RUN curl -fsSL https://repo.privadovpn.com/privado.gpg | gpg --dearmor -o /usr/share/keyrings/privado.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/privado.gpg] https://repo.privadovpn.com/apt stable main" > /etc/apt/sources.list.d/privado.list && \
    apt-get update && apt-get install -y --no-install-recommends privado && \
    rm -rf /var/lib/apt/lists/*

# Create app user
RUN useradd -m -s /bin/bash appuser

# Copy app
WORKDIR /app
COPY socks5_server.py watchdog.py entrypoint.py ./

# Make sure appuser owns /app
RUN chown -R appuser:appuser /app

# Run as non-root (but needs NET_ADMIN capability from docker run)
USER appuser

# Health check endpoint
EXPOSE 1080

ENTRYPOINT ["python3", "entrypoint.py"]