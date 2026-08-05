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
        contract_type: str = "ci.compile",
        domain: str = "rust",
        source_type: str = "base64",
        source_data: str = "",
        source_cid: str | None = None,
        source_url: str | None = None,
        source_commit: str | None = None,
        language: str = "rust",
        verification_tests: list[dict] | None = None,
        artifacts_expected: list[dict] | None = None,
        timeout_seconds: int = 300,
        escrow_contract: str = "0x00000000000000000000000000000000000000",
        escrow_budget: int = 100,
        escrow_token: str = "USDC",
        timeout_utc: str | None = None,
        requirements: dict | None = None,
        royalty_human: int = 90,
        royalty_agent: int = 10,
        auto_verify: bool = True,
        project_tag: str = "acp-mvp",
        required_confidence: float = 0.95,
        attachments: list[tuple[str, bytes]] | None = None,
    ) -> str:
        """Generate a complete .eml contract message string per expanded ACP spec."""
        message_id = f"{uuid.uuid4()}@agent.local"
        timeout_utc = timeout_utc or datetime.now(timezone.utc).isoformat()

        # Build source object
        source = {"type": source_type}
        if source_type == "base64":
            source["data"] = source_data
        elif source_type == "ipfs":
            source["cid"] = source_cid or ""
        elif source_type == "git":
            source["url"] = source_url or ""
            if source_commit:
                source["commit"] = source_commit

        # Build verification tests
        if verification_tests is None:
            if language == "rust":
                verification_tests = [
                    {"cmd": "cargo test", "timeout": 120},
                    {"cmd": "cargo build --release", "timeout": 180},
                ]
            else:
                verification_tests = [
                    {"cmd": "python -m pytest", "timeout": 120},
                ]

        # Build artifacts_expected
        if artifacts_expected is None:
            if language == "rust":
                artifacts_expected = [
                    {"path": "target/wasm32-unknown-unknown/release/*.wasm", "min_size": 1024}
                ]
            else:
                artifacts_expected = []

        payload = {
            "schema": "agent-contract/v1",
            "schema_version": "0.1.0",
            "intent": {
                "type": contract_type,
                "domain": domain,
                "language": language,
                "source": source,
                "requirements": requirements or {},
            },
            "verification": {
                "tests": verification_tests,
                "artifacts_expected": artifacts_expected,
            },
            "settlement": {
                "on_success": "release_escrow",
                "on_failure": "refund",
                "royalty_split": {
                    "human_principal": royalty_human,
                    "agent_executor": royalty_agent,
                },
                "auto_verify": auto_verify,
            },
            "sint_ua": {
                "project_tag": project_tag,
                "required_confidence": required_confidence,
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
        msg["To"] = f"broadcast+capability.{contract_type}.{domain}@mesh.local"
        msg["Subject"] = f"[CONTRACT] {contract_type}.{domain}: {language}"
        msg["X-Agent-Protocol"] = ACP_VERSION
        msg["X-Agent-Contract-Type"] = f"{contract_type}.{language}"
        msg["X-Agent-Capabilities-Required"] = f"{contract_type}.{language}"
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
