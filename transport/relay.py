"""ACP Relay — the internet bridge between OpenCode agent nodes.

A public SMTP server that routes agent contracts across the global mesh:

  Inbound:  nodes (OpenCode instances) connect over SMTP/TLS and send
            contract .eml addressed to:
              broadcast+capability.{category}.{domain}@mesh.local  → fan-out to subscribers
              marketplace.{kind}@mesh.local                        → fan-out to marketplace subscribers
              {agent_id}@node.local                                → direct delivery to one node

  Routing:  a subscription registry maps capability prefixes to node
            SMTP endpoints (host:port). The relay forwards each message
            to every matching subscriber.

Run (public server, e.g. on a VPS):
  python -m transport.relay --port 25 --tls-cert cert.pem --tls-key key.pem

Local dev (no TLS):
  python -m transport.relay --port 2525
"""

import argparse
import asyncio
import logging
import os
import ssl
import json
import smtplib
import sqlite3
import threading
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from pathlib import Path

from aiosmtpd.controller import Controller
from aiosmtpd.smtp import SMTP

logger = logging.getLogger("acp.relay")

DEFAULT_DB = os.environ.get("ACP_RELAY_DB", str(Path.home() / ".acp" / "relay.db"))
MESH_DOMAIN = "mesh.local"
NODE_DOMAIN = "node.local"


# ── Subscription registry ─────────────────────────────────
@dataclass
class Subscriber:
    node_id: str          # OpenCode instance / agent id
    capability: str       # capability prefix, e.g. "ci.compile.rust" or "*" or "marketplace"
    smtp_host: str
    smtp_port: int
    active: bool = True


class SubscriptionRegistry:
    """Persistent registry: capability prefix -> subscriber SMTP endpoint."""

    def __init__(self, db_path: str = DEFAULT_DB):
        self.db_path = db_path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS subscribers (
                    node_id TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    smtp_host TEXT NOT NULL,
                    smtp_port INTEGER NOT NULL DEFAULT 2525,
                    active INTEGER NOT NULL DEFAULT 1,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (node_id, capability)
                )
            """)
            conn.commit()

    def subscribe(self, node_id: str, capability: str, smtp_host: str, smtp_port: int = 2525) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO subscribers (node_id, capability, smtp_host, smtp_port, active, updated_at)
                   VALUES (?, ?, ?, ?, 1, ?)""",
                (node_id, capability, smtp_host, smtp_port, __import__("time").time()),
            )
            conn.commit()
        logger.info("subscribed %s -> %s (%s:%s)", node_id, capability, smtp_host, smtp_port)
        return True

    def unsubscribe(self, node_id: str, capability: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "DELETE FROM subscribers WHERE node_id=? AND capability=?", (node_id, capability)
            )
            conn.commit()
            return cur.rowcount > 0

    def matches(self, capability: str) -> list[Subscriber]:
        """Subscribers whose prefix matches the requested capability."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM subscribers WHERE active=1"
            ).fetchall()
        out = []
        for r in rows:
            sub = Subscriber(r["node_id"], r["capability"], r["smtp_host"], r["smtp_port"])
            # exact, prefix (ci.compile matches ci.compile.rust), or wildcard
            if sub.capability == "*" or capability == sub.capability or capability.startswith(sub.capability + "."):
                out.append(sub)
        return out

    def all(self) -> list[dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(
                "SELECT node_id, capability, smtp_host, smtp_port, active FROM subscribers ORDER BY node_id"
            ).fetchall()]


# ── Relay handler ─────────────────────────────────────────
class RelayHandler:
    def __init__(self, registry: SubscriptionRegistry, forward_timeout: int = 10):
        self.registry = registry
        self.forward_timeout = forward_timeout

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        # Accept any address at mesh.local / node.local (routing decides later)
        if "@" in address:
            _, _, domain = address.rpartition("@")
            if domain in (MESH_DOMAIN, NODE_DOMAIN):
                envelope.rcpt_tos.append(address)
                envelope.rcpt_options.extend(rcpt_options)
                return "250 OK"
        return "550 not relaying for this domain"

    async def handle_DATA(self, server, session, envelope):
        raw = envelope.content
        try:
            msg = BytesParser(policy=policy.default).parsebytes(raw)
        except Exception as e:  # noqa: BLE001
            logger.error("parse failed: %s", e)
            return "550 parse error"

        to_address = envelope.rcpt_tos[0] if envelope.rcpt_tos else ""
        capability = self._parse_capability(to_address)
        logger.info("DATA from %s to %s (capability=%s)", envelope.mail_from, to_address, capability)

        if capability is None:
            # direct delivery to one node: {agent_id}@node.local → resolve node id
            node_id = to_address.split("@")[0]
            self._store_inbox(node_id, raw)
            return "250 queued for direct delivery"

        # Fan-out to subscribers
        subscribers = self.registry.matches(capability)
        if not subscribers:
            logger.info("no subscribers for capability %s", capability)
            return "250 no subscribers (queued)"

        delivered = 0
        for sub in subscribers:
            try:
                ok = await asyncio.to_thread(
                    self._forward, sub, raw, envelope.mail_from, to_address
                )
                if ok:
                    delivered += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("forward to %s failed: %s", sub.smtp_host, e)

        logger.info("delivered %d/%d subscribers for %s", delivered, len(subscribers), capability)
        return "250 delivered"

    def _parse_capability(self, to_address: str) -> str | None:
        """Extract capability from broadcast+capability.X@mesh.local or marketplace.X@mesh.local."""
        local, _, domain = to_address.rpartition("@")
        if domain != MESH_DOMAIN:
            return None
        if local.startswith("broadcast+capability."):
            return local[len("broadcast+capability."):]
        if local.startswith("marketplace."):
            return local  # e.g. marketplace.mcp_server
        if local.startswith("broadcast+marketplace."):
            return "marketplace." + local[len("broadcast+marketplace."):]
        return None

    def _forward(self, sub: Subscriber, raw: bytes, mail_from: str, to_address: str) -> bool:
        """Forward the message to a subscriber via SMTP."""
        try:
            with smtplib.SMTP(sub.smtp_host, sub.smtp_port, timeout=self.forward_timeout) as smtp:
                smtp.sendmail(mail_from, [to_address], raw)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("smtp forward error: %s", e)
            return False

    def _store_inbox(self, node_id: str, raw: bytes):
        """Direct-delivery fallback: store in relay inbox for later pickup."""
        inbox_dir = Path.home() / ".acp" / "inbox" / node_id
        inbox_dir.mkdir(parents=True, exist_ok=True)
        msg_id = str(len(list(inbox_dir.glob("*.eml")))).zfill(6)
        (inbox_dir / f"{msg_id}.eml").write_bytes(raw)


# ── Controller wrapper ────────────────────────────────────
class RelayServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 2525,
                 tls_cert: str | None = None, tls_key: str | None = None,
                 db_path: str = DEFAULT_DB):
        self.host = host
        self.port = port
        self.tls_cert = tls_cert
        self.tls_key = tls_key
        self.registry = SubscriptionRegistry(db_path)
        self.handler = RelayHandler(self.registry)
        self.controller: Controller | None = None

    def start(self):
        kwargs = {}
        if self.tls_cert and self.tls_key:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(self.tls_cert, self.tls_key)
            kwargs["server_hostname"] = self.host
            kwargs["tls_context"] = ctx
            kwargs["ssl_context"] = ctx
        self.controller = Controller(
            self.handler, hostname=self.host, port=self.port, **kwargs
        )
        self.controller.start()
        logger.info("ACP Relay listening on %s:%s (TLS=%s)", self.host, self.port, bool(self.tls_cert))

    def stop(self):
        if self.controller:
            self.controller.stop()

    @property
    def subscriptions(self) -> list[dict]:
        return self.registry.all()


def main():
    parser = argparse.ArgumentParser(description="ACP Relay — internet bridge for agent nodes")
    parser.add_argument("--port", type=int, default=int(os.environ.get("ACP_RELAY_PORT", "2525")))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--tls-cert", default=os.environ.get("ACP_RELAY_TLS_CERT"))
    parser.add_argument("--tls-key", default=os.environ.get("ACP_RELAY_TLS_KEY"))
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--subscribe", nargs=4, metavar=("NODE", "CAP", "HOST", "PORT"),
                        help="register a subscription at startup: relay --subscribe executor ci.compile.rust 1.2.3.4 2525")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    if args.subscribe:
        node, cap, host, port = args.subscribe
        registry = SubscriptionRegistry(args.db)
        registry.subscribe(node, cap, host, int(port))
        logger.info("registered subscription: %s %s %s:%s", node, cap, host, port)
        # keep running to also serve
        server = RelayServer(args.host, args.port, args.tls_cert, args.tls_key, args.db)
        server.start()
        try:
            import time
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            server.stop()
        return

    server = RelayServer(args.host, args.port, args.tls_cert, args.tls_key, args.db)
    server.start()
    try:
        import time
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":
    main()
