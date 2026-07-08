#!/usr/bin/env python3
"""
PrivadoVPN + SOCKS5 Watchdog

Monitors tun0 interface, auto-reconnects VPN, restarts SOCKS5 server bound to VPN IP.
All traffic routed through tun0 via bind_ip on outgoing connections.
Zero host network changes.
"""

import asyncio
import logging
import os
import signal
import subprocess
import sys
from typing import Optional

log = logging.getLogger("watchdog")


class VPNAndSOCKSWatchdog:
    def __init__(
        self,
        vpn_cmd: list[str],
        tun_interface: str = "tun0",
        socks_port: int = 1080,
        check_interval: int = 10,
        restart_delay: int = 5,
    ):
        self.vpn_cmd = vpn_cmd
        self.tun_interface = tun_interface
        self.socks_port = socks_port
        self.check_interval = check_interval
        self.restart_delay = restart_delay

        self.vpn_proc: Optional[subprocess.Popen] = None
        self.socks_task: Optional[asyncio.Task] = None
        self.socks_module = None
        self.current_bind_ip: Optional[str] = None
        self._shutdown = False

    def get_tun0_ip(self) -> Optional[str]:
        """Get the IPv4 address of tun0 interface."""
        try:
            result = subprocess.run(
                ["ip", "-4", "addr", "show", "dev", self.tun_interface],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode != 0:
                return None
            for line in result.stdout.splitlines():
                if "inet " in line:
                    parts = line.strip().split()
                    for p in parts:
                        if p.startswith("inet "):
                            return p.split("/")[0].replace("inet ", "")
        except Exception as e:
            log.debug("Failed to get tun0 IP: %s", e)
        return None

    def is_tun0_up(self) -> bool:
        """Check if tun0 interface exists and is UP."""
        try:
            result = subprocess.run(
                ["ip", "link", "show", "dev", self.tun_interface],
                capture_output=True,
                text=True,
                timeout=2,
            )
            return "UP" in result.stdout
        except Exception:
            return False

    async def start_vpn(self) -> bool:
        """Start the VPN process and wait for tun0."""
        log.info("Starting VPN: %s", " ".join(self.vpn_cmd))
        try:
            self.vpn_proc = subprocess.Popen(
                self.vpn_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            # Wait for tun0 to appear
            for _ in range(30):
                await asyncio.sleep(1)
                if self.is_tun0_up():
                    ip = self.get_tun0_ip()
                    if ip:
                        log.info("VPN connected: %s=%s", self.tun_interface, ip)
                        return True
            log.error("VPN started but %s never came up", self.tun_interface)
            return False
        except Exception as e:
            log.error("Failed to start VPN: %s", e)
            return False

    def stop_vpn(self):
        """Stop the VPN process."""
        if self.vpn_proc:
            log.info("Stopping VPN...")
            self.vpn_proc.terminate()
            try:
                self.vpn_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.vpn_proc.kill()
                self.vpn_proc.wait()
            self.vpn_proc = None

    async def start_socks(self, bind_ip: str):
        """Start SOCKS5 server bound to the VPN IP."""
        if self.socks_task and not self.socks_task.done():
            self.socks_task.cancel()
            try:
                await self.socks_task
            except asyncio.CancelledError:
                pass

        # Import socks5_server module
        if self.socks_module is None:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "socks5_server", "/app/socks5_server.py"
            )
            self.socks_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.socks_module)

        from socks5_server import SOCKS5Config, run_server

        config = SOCKS5Config(
            listen_host="0.0.0.0",
            listen_port=self.socks_port,
            bind_ip=bind_ip,
        )
        self.current_bind_ip = bind_ip
        self.socks_task = asyncio.create_task(run_server(config))
        log.info("SOCKS5 server started on %s:%d (outbound via %s)", "0.0.0.0", self.socks_port, bind_ip)

    async def stop_socks(self):
        """Stop SOCKS5 server."""
        if self.socks_task and not self.socks_task.done():
            self.socks_task.cancel()
            try:
                await self.socks_task
            except asyncio.CancelledError:
                pass
        self.socks_task = None
        self.current_bind_ip = None

    async def run(self):
        """Main watchdog loop."""
        # Initial VPN start
        if not await self.start_vpn():
            log.error("Initial VPN connection failed")
            return

        # Get initial IP and start SOCKS
        ip = self.get_tun0_ip()
        if not ip:
            log.error("No %s IP after VPN connect", self.tun_interface)
            return
        await self.start_socks(ip)

        # Watchdog loop
        while not self._shutdown:
            await asyncio.sleep(self.check_interval)

            if not self.is_tun0_up():
                log.warning("%s is down, reconnecting...", self.tun_interface)
                await self.stop_socks()
                self.stop_vpn()
                await asyncio.sleep(self.restart_delay)

                if await self.start_vpn():
                    new_ip = self.get_tun0_ip()
                    if new_ip:
                        if new_ip != self.current_bind_ip:
                            log.info("VPN reconnected with new IP: %s -> %s", self.current_bind_ip, new_ip)
                        else:
                            log.info("VPN reconnected, same IP: %s", new_ip)
                        await self.start_socks(new_ip)
                    else:
                        log.error("VPN up but no %s IP", self.tun_interface)
                else:
                    log.error("VPN reconnection failed")

    def shutdown(self):
        self._shutdown = True


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    # PrivadoVPN CLI command
    vpn_cmd = ["privado", "connect", "--auto"]

    # Allow override via env
    if os.getenv("PRIVADO_CMD"):
        vpn_cmd = os.getenv("PRIVADO_CMD").split()

    watchdog = VPNAndSOCKSWatchdog(vpn_cmd)

    # Handle signals
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, watchdog.shutdown)

    await watchdog.run()
    log.info("Watchdog stopped")


if __name__ == "__main__":
    asyncio.run(main())