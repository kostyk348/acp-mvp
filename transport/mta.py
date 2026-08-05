"""Local SMTP MTA on port 2525 — receives .eml contracts and queues them."""

import asyncio
import os
import json
import logging
from pathlib import Path
from datetime import datetime, timezone

from aiosmtpd.controller import Controller
from aiosmtpd.smtp import SMTP as SMTPServer
from aiosmtpd.handlers import Message

logger = logging.getLogger("acp.mta")

INBOX_DIR = Path("/var/spool/agent/inbox")
CAPABILITIES = {"ci.compile"}  # Local agent capabilities (configurable)
MIN_BUDGET = 1  # Minimum budget in tokens (0.01 USDC equivalent for MVP)


class ContractMessageHandler(Message):
    """Handles incoming SMTP messages — validates and queues them."""

    def __init__(self, inbox_dir: Path):
        super().__init__()
        self.inbox_dir = inbox_dir

    async def handle_DATA(self, server, session, envelope):
        """Process incoming SMTP DATA command."""
        mail_from = envelope.mail_from
        rcpt_to = envelope.rcpt_tos
        data = envelope.content

        # Extract To header for capability routing
        to_header = None
        subject = None
        x_budget = None
        x_pstake = None

        for line in data.decode("utf-8", errors="replace").split("\n"):
            if line.lower().startswith("to:"):
                to_header = line.split(":", 1)[1].strip()
            if line.lower().startswith("subject:"):
                subject = line.split(":", 1)[1].strip()
            if line.lower().startswith("x-escrow-budget:"):
                x_budget = line.split(":", 1)[1].strip()
            if line.lower().startswith("x-proof-of-stake:"):
                x_pstake = line.split(":", 1)[1].strip()

        # Anti-spam: check proof-of-stake
        if x_pstake is None or x_pstake.lower() == "null":
            # MVP: allow null PoS but check budget
            pass

        # Anti-spam: check budget
        if x_budget is not None:
            try:
                budget_val = float(x_budget)
                if budget_val < MIN_BUDGET:
                    logger.warning(
                        f"Rejected: budget {budget_val} < minimum {MIN_BUDGET}"
                    )
                    return "550 Budget too low"
            except ValueError:
                pass

        # Capability-based routing
        if to_header:
            for cap in CAPABILITIES:
                if f"capability.{cap}" in to_header:
                    # Local delivery — save to inbox
                    self.inbox_dir.mkdir(parents=True, exist_ok=True)
                    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                    filename = f"{timestamp}_{envelope.mail_from.replace('@', '_')}.eml"
                    filepath = self.inbox_dir / filename
                    with open(filepath, "wb") as f:
                        f.write(data)
                    logger.info(f"Queued contract to inbox: {filepath}")
                    return "250 OK"

        # No matching capability — forward to relay (for MVP, just log)
        logger.info(f"No matching capability for {to_header}, forwarding would go here")
        return "250 OK (forwarded)"


class AgentMTA:
    """Local SMTP MTA server."""

    def __init__(self, host: str = "0.0.0.0", port: int = 2525, inbox_dir: str | None = None):
        self.host = host
        self.port = port
        self.inbox_dir = Path(inbox_dir or "/var/spool/agent/inbox")
        self.controller: Controller | None = None

    async def start(self):
        """Start the SMTP MTA."""
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        handler = ContractMessageHandler(self.inbox_dir)
        self.controller = Controller(handler, hostname=self.host, port=self.port)
        self.controller.start()
        logger.info(f"SMTP MTA started on {self.host}:{self.port}")

    async def stop(self):
        """Stop the SMTP MTA."""
        if self.controller:
            self.controller.stop()
            logger.info("SMTP MTA stopped")

    def is_running(self) -> bool:
        return self.controller is not None and self.controller.sessno is not None
