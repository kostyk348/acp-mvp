"""EML contract generator — builds valid MIME messages per RFC 822."""

import uuid
import json
import base64
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from email.utils import formatdate
from pathlib import Path

from .schema import validate_contract


ACP_VERSION = "ACP/0.1"


class ContractGenerator:
    """Generates .eml contract messages."""

    def __init__(self, agent_id: str, agent_private_key_hex: str | None = None):
        self.agent_id = agent_id
        self.agent_private_key_hex = agent_private_key_hex

    def generate(
        self,
        contract_type: str,
        to_agent: str,
        source_ref: str,
        language: str = "rust",
        verification_tests: list[str] | None = None,
        timeout_seconds: int = 300,
        escrow_contract: str = "0x0000000000000000000000000000000000000000",
        escrow_budget: int = 100,
        escrow_token: str = "USDC",
        timeout_utc: str | None = None,
        requirements: dict | None = None,
        attachments: list[tuple[str, bytes]] | None = None,
    ) -> str:
        """Generate a complete .eml contract message string."""
        message_id = f"{uuid.uuid4()}@agent.local"
        timeout_utc = timeout_utc or datetime.now(timezone.utc).isoformat()

        verification_tests = verification_tests or [
            f"cargo test" if language == "rust" else "python -m pytest",
            f"cargo build --release" if language == "rust" else "python -m build",
        ]

        requirements = requirements or {
            "target": "wasm32-unknown-unknown" if language == "rust" else "auto",
            "features": ["std"],
        }

        payload = {
            "schema": "agent-contract/v1",
            "intent": {
                "type": "ci.compile",
                "language": language,
                "source_ref": source_ref,
                "requirements": requirements,
            },
            "verification": {
                "tests": verification_tests,
                "timeout_seconds": timeout_seconds,
            },
            "settlement": {
                "onSuccess": "release-escrow",
                "onFailure": "refund",
                "royaltySplit": {"human": 90, "agent": 10},
            },
        }

        # Validate before generating
        errors = validate_contract(payload)
        if errors:
            raise ValueError(f"Contract validation failed: {errors}")

        # Build MIME message
        msg = MIMEMultipart("mixed")
        msg["Message-ID"] = message_id
        msg["From"] = f"{self.agent_id}@agent.local"
        msg["To"] = f"broadcast+capability.{contract_type}@mesh.local"
        msg["Subject"] = f"[CONTRACT] {contract_type}: {language} compile"
        msg["X-Agent-Protocol"] = ACP_VERSION
        msg["X-Agent-Contract-Type"] = f"ci.compile.{language}"
        msg["X-Escrow-Contract"] = escrow_contract
        msg["X-Escrow-Budget"] = str(escrow_budget)
        msg["X-Escrow-Token"] = escrow_token
        msg["X-Timeout-UTC"] = timeout_utc
        msg["X-Proof-of-Stake"] = "null"  # MVP: no PoS
        msg["Date"] = formatdate(localtime=True)

        # JSON payload as main body
        payload_json = json.dumps(payload, indent=2)
        msg.attach(MIMEText(payload_json, "plain", "utf-8"))

        # Attachments
        if attachments:
            for filename, data in attachments:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(data)
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
                msg.attach(part)

        return msg.as_string()

    @staticmethod
    def extract_message_id(eml_content: str) -> str | None:
        """Extract Message-ID from raw .eml content."""
        for line in eml_content.splitlines():
            if line.lower().startswith("message-id:"):
                return line.split(":", 1)[1].strip()
        return None
