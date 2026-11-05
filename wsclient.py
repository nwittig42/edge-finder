"""Minimal WebSocket client (RFC 6455) on the standard library, plus Ed25519 request signing
for Kalshi and Polymarket US. No third-party packages, so the 24/7 service has nothing to break."""

import base64
import json
import os
import socket
import ssl
import struct
import subprocess
import time

import secrets_env
from pathlib import Path
from urllib.parse import urlparse

KALSHI_DIR = Path.home() / ".kalshi"
PM_DIR = Path.home() / ".polymarket_us"


# ---------- signing (openssl, Ed25519) ----------

def _openssl():
    """OpenSSL 3 (Homebrew) is needed for Ed25519; macOS's /usr/bin/openssl is LibreSSL and can't do it.
    The LaunchAgent's PATH doesn't include Homebrew, so look in the usual places explicitly."""
    for p in ("/opt/homebrew/bin/openssl", "/usr/local/bin/openssl", "/opt/homebrew/opt/openssl@3/bin/openssl"):
        if os.path.exists(p):
            return p
    return "openssl"


OPENSSL = _openssl()


def _sign(pem_path, message):
    out = subprocess.run([OPENSSL, "pkeyutl", "-sign", "-inkey", str(pem_path), "-rawin", "-in", "/dev/stdin"],
                         input=message.encode(), capture_output=True, check=True).stdout
    return base64.b64encode(out).decode()


def _pm_pem():
    """Polymarket US gives a base64 secret whose first 32 bytes are the Ed25519 seed; wrap it as PKCS#8."""
    pem = PM_DIR / "ed25519.pem"
    if not pem.exists():
        seed = base64.b64decode((PM_DIR / "secret.txt").read_text().strip())[:32]
        der = bytes.fromhex("302e020100300506032b657004220420") + seed
        old = os.umask(0o077)
        try:
            pem.write_text("-----BEGIN PRIVATE KEY-----\n" + base64.b64encode(der).decode() + "\n-----END PRIVATE KEY-----\n")
        finally:
            os.umask(old)
    return pem


def kalshi_headers(method, path):
    ts = str(int(time.time() * 1000))
    return {"KALSHI-ACCESS-KEY": secrets_env.kalshi_key_id(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": _sign(secrets_env.kalshi_pem(), ts + method + path)}


def pm_headers(method, path):
    ts = str(int(time.time() * 1000))
    return {"X-PM-Access-Key": secrets_env.polymarket_key_id(),
            "X-PM-Timestamp": ts,
            "X-PM-Signature": _sign(secrets_env.polymarket_pem(), ts + method + path)}


# ---------- websocket ----------

class WSClosed(Exception):
    pass


class WebSocket:
    def __init__(self, url, headers=None, timeout=30):
        u = urlparse(url)
        self.host, self.path = u.hostname, (u.path or "/") + (f"?{u.query}" if u.query else "")
        raw = socket.create_connection((self.host, u.port or 443), timeout=timeout)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host)
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [f"GET {self.path} HTTP/1.1", f"Host: {self.host}", "Upgrade: websocket", "Connection: Upgrade",
                 f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13", "User-Agent: edge-finder/1.0"]
        lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WSClosed("handshake: connection closed")
            resp += chunk
        head, self._buf = resp.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 101 " not in status + " ":
            raise WSClosed(f"handshake failed: {status} {self._buf[:200]!r}")

    def _read(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WSClosed("connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode, payload):
        mask = os.urandom(4)
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send_json(self, obj):
        self._send_frame(0x1, json.dumps(obj).encode())

    def recv_json(self):
        """Blocks until one complete text message; answers pings; raises WSClosed on close."""
        parts = []
        while True:
            b1, b2 = self._read(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x9:
                self._send_frame(0xA, data)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x8:
                raise WSClosed(f"closed by server {data[:2].hex()} {data[2:200]!r}")
            parts.append(data)
            if fin:
                return json.loads(b"".join(parts))

    def close(self):
        try:
            self._send_frame(0x8, b"")
            self.sock.close()
        except Exception:
            pass
