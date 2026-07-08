#!/usr/bin/env python3
"""
SOCKS5 Server (asyncio, stdlib only)
Binds outbound connections to a specific interface IP (tun0).
"""

import asyncio
import logging
import struct
import socket
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("socks5")

# SOCKS5 constants
VER = 0x05
CMD_CONNECT = 0x01
ATYP_IPV4 = 0x01
ATYP_DOMAIN = 0x03
ATYP_IPV6 = 0x04
AUTH_NONE = 0x00
AUTH_USERPASS = 0x02
REP_SUCCESS = 0x00
REP_FAILED = 0x01
REP_NOT_ALLOWED = 0x02
REP_NET_UNREACH = 0x03
REP_HOST_UNREACH = 0x04
REP_CONN_REFUSED = 0x05
REP_TTL_EXPIRED = 0x06
REP_CMD_UNSUPPORTED = 0x07
REP_ATYP_UNSUPPORTED = 0x08


@dataclass
class SOCKS5Config:
    listen_host: str = "0.0.0.0"
    listen_port: int = 1080
    bind_ip: str = "0.0.0.0"  # Source IP for outbound connections (tun0 IP)
    username: Optional[str] = None
    password: Optional[str] = None


class SOCKS5Server:
    def __init__(self, config: SOCKS5Config):
        self.config = config
        self.server: Optional[asyncio.Server] = None

    async def start(self):
        self.server = await asyncio.start_server(
            self.handle_client,
            self.config.listen_host,
            self.config.listen_port,
        )
        addrs = ", ".join(str(s.getsockname()) for s in self.server.sockets)
        log.info("SOCKS5 listening on %s (outbound via %s)", addrs, self.config.bind_ip)

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        log.debug("Client connected: %s", peer)
        try:
            await self._socks5_handshake(reader, writer)
        except Exception as e:
            log.warning("Client %s error: %s", peer, e)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _socks5_handshake(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        # ── Greeting ──
        ver, nmethods = await self._read_exact(reader, 2)
        if ver != VER:
            raise ValueError(f"Unsupported SOCKS version: {ver}")

        methods = await self._read_exact(reader, nmethods)

        # Choose auth method
        if self.config.username and AUTH_USERPASS in methods:
            chosen = AUTH_USERPASS
        elif AUTH_NONE in methods:
            chosen = AUTH_NONE
        else:
            writer.write(bytes([VER, 0xFF]))
            await writer.drain()
            raise ValueError("No acceptable auth method")

        writer.write(bytes([VER, chosen]))
        await writer.drain()

        # ── Authentication ──
        if chosen == AUTH_USERPASS:
            await self._handle_auth(reader, writer)

        # ── Request ──
        ver, cmd, rsv, atyp = await self._read_exact(reader, 4)
        if ver != VER or cmd != CMD_CONNECT:
            await self._send_reply(writer, REP_CMD_UNSUPPORTED)
            raise ValueError(f"Unsupported command: {cmd}")

        # Parse target address
        if atyp == ATYP_IPV4:
            addr_bytes = await self._read_exact(reader, 4)
            target_host = socket.inet_ntoa(addr_bytes)
        elif atyp == ATYP_DOMAIN:
            domain_len = await self._read_exact(reader, 1)
            domain = await self._read_exact(reader, domain_len[0])
            target_host = domain.decode()
        elif atyp == ATYP_IPV6:
            addr_bytes = await self._read_exact(reader, 16)
            target_host = socket.inet_ntop(socket.AF_INET6, addr_bytes)
        else:
            await self._send_reply(writer, REP_ATYP_UNSUPPORTED)
            raise ValueError(f"Unsupported address type: {atyp}")

        target_port = struct.unpack("!H", await self._read_exact(reader, 2))[0]
        log.info("CONNECT %s:%d", target_host, target_port)

        # ── Connect to target via bind_ip ──
        try:
            # Create socket bound to tun0 IP
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if self.config.bind_ip != "0.0.0.0":
                sock.bind((self.config.bind_ip, 0))
            sock.setblocking(False)

            # Async connect
            await asyncio.get_event_loop().sock_connect(sock, (target_host, target_port))

            # Wrap in asyncio streams
            upstream_reader, upstream_writer = await asyncio.open_connection(
                sock=sock,
            )

            # Send success reply with bind address
            bind_ip = self.config.bind_ip if self.config.bind_ip != "0.0.0.0" else "0.0.0.0"
            await self._send_reply(writer, REP_SUCCESS, bind_ip, sock.getsockname()[1])

            # ── Relay data ──
            await self._relay(reader, writer, upstream_reader, upstream_writer)

        except ConnectionRefusedError:
            await self._send_reply(writer, REP_CONN_REFUSED)
        except asyncio.TimeoutError:
            await self._send_reply(writer, REP_HOST_UNREACH)
        except OSError as e:
            log.warning("Connection failed: %s", e)
            await self._send_reply(writer, REP_FAILED)

    async def _handle_auth(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        ver = await self._read_exact(reader, 1)
        if ver[0] != 0x01:
            raise ValueError("Unsupported auth sub-version")

        ulen = await self._read_exact(reader, 1)
        username = (await self._read_exact(reader, ulen[0])).decode()
        plen = await self._read_exact(reader, 1)
        password = (await self._read_exact(reader, plen[0])).decode()

        if username != self.config.username or password != self.config.password:
            writer.write(bytes([0x01, 0x01]))
            await writer.drain()
            raise ValueError("Auth failed")

        writer.write(bytes([0x01, 0x00]))
        await writer.drain()

    async def _send_reply(
        self,
        writer: asyncio.StreamWriter,
        rep: int,
        bind_host: str = "0.0.0.0",
        bind_port: int = 0,
    ):
        # IPv4 bind address
        reply = struct.pack("!BBBB", VER, rep, 0x00, ATYP_IPV4)
        reply += socket.inet_aton(bind_host)
        reply += struct.pack("!H", bind_port)
        writer.write(reply)
        await writer.drain()

    async def _relay(
        self,
        client_r: asyncio.StreamReader,
        client_w: asyncio.StreamWriter,
        upstream_r: asyncio.StreamReader,
        upstream_w: asyncio.StreamWriter,
    ):
        async def pipe(r, w, label):
            try:
                while True:
                    data = await r.read(65536)
                    if not data:
                        break
                    w.write(data)
                    await w.drain()
            except Exception:
                pass
            finally:
                try:
                    w.close()
                except Exception:
                    pass

        await asyncio.gather(
            pipe(client_r, upstream_w, "client->upstream"),
            pipe(upstream_r, client_w, "upstream->client"),
        )

    @staticmethod
    async def _read_exact(reader: asyncio.StreamReader, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = await reader.read(n - len(buf))
            if not chunk:
                raise ConnectionError("Connection closed")
            buf.extend(chunk)
        return bytes(buf)


async def run_server(config: SOCKS5Config):
    server = SOCKS5Server(config)
    await server.start()
    try:
        await asyncio.Event().wait()  # Run forever
    finally:
        await server.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = SOCKS5Config(bind_ip="10.8.0.2")  # Example tun0 IP
    asyncio.run(run_server(cfg))