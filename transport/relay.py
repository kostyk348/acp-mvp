"""SMTP relay client — sends .eml contracts to remote agents."""

import smtplib
import logging
from email.policy import SMTP as SMTPPolicy
from pathlib import Path

logger = logging.getLogger("acp.relay")


class RelayClient:
    """Sends .eml messages through an SMTP relay."""

    def __init__(self, relay_host: str = "localhost", relay_port: int = 2525):
        self.relay_host = relay_host
        self.relay_port = relay_port

    def send(
        self,
        eml_content: str | bytes,
        to_address: str,
        from_address: str,
    ) -> bool:
        """Send an .eml contract through the relay."""
        try:
            with smtplib.SMTP(self.relay_host, self.relay_port, timeout=30) as server:
                server.sendmail(
                    from_address,
                    [to_address],
                    eml_content if isinstance(eml_content, bytes) else eml_content.encode("utf-8"),
                )
            logger.info(f"Sent contract to {to_address} via {self.relay_host}:{self.relay_port}")
            return True
        except Exception as e:
            logger.error(f"Failed to send via relay: {e}")
            return False

    def send_file(
        self,
        eml_path: str | Path,
        to_address: str,
        from_address: str,
    ) -> bool:
        """Send an .eml file from disk."""
        path = Path(eml_path)
        with open(path, "rb") as f:
            content = f.read()
        return self.send(content, to_address, from_address)
