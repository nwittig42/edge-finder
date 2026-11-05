"""Reads ~/edge-finder/.env (the one place API keys live) and turns the Ed25519 keys into the PEM files
openssl needs, under ~/edge-finder/.keys (owner-only). Falls back to the old per-service key files."""

import base64
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_PATH = HERE / ".env"
KEY_DIR = HERE / ".keys"


def env():
    vals = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip()
    return vals


def get(name, default=None):
    return os.environ.get(name) or env().get(name) or default


def _write_pem(name, der_b64):
    KEY_DIR.mkdir(mode=0o700, exist_ok=True)
    p = KEY_DIR / name
    body = "-----BEGIN PRIVATE KEY-----\n" + der_b64 + "\n-----END PRIVATE KEY-----\n"
    if not p.exists() or p.read_text() != body:
        old = os.umask(0o077)
        try:
            p.write_text(body)
        finally:
            os.umask(old)
    return p


def kalshi_pem():
    k = get("KALSHI_PRIVATE_KEY")
    return _write_pem("kalshi.pem", k) if k else Path.home() / ".kalshi" / "private_key.pem"


def polymarket_pem():
    s = get("POLYMARKET_US_SECRET_KEY") or (Path.home() / ".polymarket_us" / "secret.txt").read_text().strip()
    seed = base64.b64decode(s)[:32]
    der = bytes.fromhex("302e020100300506032b657004220420") + seed
    return _write_pem("polymarket_us.pem", base64.b64encode(der).decode())


def kalshi_key_id():
    return get("KALSHI_KEY_ID") or (Path.home() / ".kalshi" / "key_id.txt").read_text().strip()


def polymarket_key_id():
    return get("POLYMARKET_US_KEY_ID") or (Path.home() / ".polymarket_us" / "key_id.txt").read_text().strip()
