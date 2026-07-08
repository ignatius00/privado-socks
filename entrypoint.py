#!/usr/bin/env python3
"""
Entrypoint: validates environment and starts watchdog.
"""

import logging
import os
import sys

log = logging.getLogger("entrypoint")

def check_requirements():
    """Validate runtime requirements."""
    # Check /dev/net/tun
    if not os.path.exists("/dev/net/tun"):
        log.error("/dev/net/tun not found -- run container with --device=/dev/net/tun")
        return False

    # Check NET_ADMIN capability (rough check)
    try:
        with open("/proc/self/status") as f:
            content = f.read()
            if "CapEff:" in content:
                # Could parse but just warn
                pass
    except Exception:
        pass

    # Check credentials
    if not os.getenv("PRIVADO_USERNAME") or not os.getenv("PRIVADO_PASSWORD"):
        log.warning("PRIVADO_USERNAME/PRIVADO_PASSWORD not set - PrivadoVPN may prompt interactively")

    return True

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    if not check_requirements():
        sys.exit(1)

    # Import and run watchdog
    from watchdog import main as watchdog_main

    try:
        import asyncio
        asyncio.run(watchdog_main())
    except KeyboardInterrupt:
        log.info("Shutdown requested")