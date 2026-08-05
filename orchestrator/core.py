"""Orchestrator — core logic for the ACP agent node."""

import logging
import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

from eml.parser import ContractParser
from eml.generator import ContractGenerator
from sandbox.executor import SandboxExecutor
from settlement.escrow import EscrowClient
from sint_ua.fact_chain import FactChain
from identity.keystore import KeyStore

logger = logging.getLogger("acp.orchestrator")

SANDBOX_BASE = Path("/tmp/agent-sandbox")


class Orchestrator:
    """Main orchestrator for the ACP agent node."""

    def __init__(
        self,
        agent_id: str,
        inbox_dir: str = "/var/spool/agent/inbox",
        sandbox_base: str = "/tmp/agent-sandbox",
        knowledge_db: str = "~/.agent/knowledge.db",
        keystore_path: str = "~/.agent/keystore.json",
    ):
        self.agent_id = agent_id
        self.inbox_dir = Path(inbox_dir)
        self.sandbox_base = Path(sandbox_base)
        self.parser = ContractParser()
        self.generator = ContractGenerator(agent_id)
        self.sandbox = SandboxExecutor(sandbox_base)
        self.escrow = EscrowClient()
        self.facts = FactChain(knowledge_db)
        self.keystore = KeyStore(keystore_path)
        self._processed: set[str] = set()

    def process_inbox(self) -> list[dict[str, Any]]:
        """Process all .eml files in the inbox."""
        results = []
        self.inbox_dir.mkdir(parents=True, exist_ok=True)

        for eml_file in sorted(self.inbox_dir.glob("*.eml")):
            contract_id = self._contract_id(eml_file)
            if contract_id in self._processed:
                continue

            logger.info(f"Processing contract: {contract_id}")
            try:
                result = self.process_contract(eml_file)
                results.append(result)
                self._processed.add(contract_id)
            except Exception as e:
                logger.error(f"Failed to process {contract_id}: {e}")
                results.append({"contract_id": contract_id, "status": "error", "error": str(e)})

        return results

    def process_contract(self, eml_path: Path) -> dict[str, Any]:
        """Process a single contract .eml file."""
        # Parse
        parsed = self.parser.parse(eml_path)
        if not parsed["valid"]:
            return {
                "contract_id": parsed["message_id"],
                "status": "rejected",
                "errors": parsed["errors"],
            }

        payload = parsed["payload"]
        contract_id = parsed["message_id"] or hashlib.sha256(str(parsed).encode()).hexdigest()[:16]

        # Check escrow
        escrow_info = {
            "contract": parsed["headers"].get("X-Escrow-Contract"),
            "budget": parsed["headers"].get("X-Escrow-Budget"),
            "token": parsed["headers"].get("X-Escrow-Token"),
        }

        if not self.escrow.is_valid(escrow_info):
            return {
                "contract_id": contract_id,
                "status": "rejected",
                "reason": "escrow_invalid",
            }

        # Extract source
        intent = payload.get("intent", {})
        source_ref = intent.get("source_ref", "")
        language = intent.get("language", "rust")
        verification = payload.get("verification", {})
        tests = verification.get("tests", [])
        timeout = verification.get("timeout_seconds", 300)

        # Cross-reference with MCP knowledge bases
        mcp_sources = self._enrich_with_mcp(
            content=f"ci.compile.{language} contract {contract_id}",
            domain="code",
        )

        # Sandbox execution
        work_dir = self.sandbox_base / contract_id
        exec_result = self.sandbox.execute(
            source_ref=source_ref,
            language=language,
            tests=tests,
            timeout=timeout,
            work_dir=work_dir,
        )

        # Generate result
        if exec_result["success"]:
            result_eml = self._build_result_eml(
                contract_id=contract_id,
                original_message_id=parsed["message_id"],
                status="SUCCESS",
                exec_hash=exec_result["exec_hash"],
                proof=exec_result["proof"],
                artifact=exec_result.get("artifact"),
            )
            # Record fact with MCP enrichment
            self.facts.record(
                content=f"ci.compile.{language} succeeded for contract {contract_id}",
                tags=["ci", language, "compile", "success"],
                confidence=1.0,
                source=f"sandbox run {datetime.now(timezone.utc).isoformat()}",
                mcp_sources=mcp_sources,
            )
            return {
                "contract_id": contract_id,
                "status": "SUCCESS",
                "result_eml": result_eml,
                "proof": exec_result["proof"],
            }
        else:
            self.facts.record(
                content=f"ci.compile.{language} FAILED for contract {contract_id}",
                tags=["ci", language, "compile", "failure"],
                confidence=1.0,
                source=f"sandbox run {datetime.now(timezone.utc).isoformat()}",
                mcp_sources=mcp_sources,
            )
            return {
                "contract_id": contract_id,
                "status": "FAILED",
                "exit_codes": exec_result.get("exit_codes", []),
                "stderr": exec_result.get("stderr", ""),
            }

    def _enrich_with_mcp(self, content: str, domain: str = "general") -> list[dict]:
        """Cross-reference content against MCP knowledge bases for enrichment."""
        return self.facts.cross_reference(content, domain)

    def _build_result_eml(
        self,
        contract_id: str,
        original_message_id: str,
        status: str,
        exec_hash: str,
        proof: dict,
        artifact: str | None = None,
    ) -> str:
        """Build a result .eml response."""
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        from email.mime.base import MIMEBase
        from email import encoders
        from email.utils import formatdate

        msg = MIMEMultipart("mixed")
        msg["Message-ID"] = f"{contract_id}-result@agent.local"
        msg["From"] = f"{self.agent_id}@agent.local"
        msg["In-Reply-To"] = original_message_id
        msg["X-Agent-Status"] = status
        msg["X-Execution-Hash"] = exec_hash
        msg["Date"] = formatdate(localtime=True)

        # Proof JSON
        proof_json = json.dumps(proof, indent=2)
        msg.attach(MIMEText(proof_json, "plain", "utf-8"))

        # Artifact attachment
        if artifact and Path(artifact).exists():
            with open(artifact, "rb") as f:
                data = f.read()
            part = MIMEBase("application", "octet-stream")
            part.set_payload(data)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f'attachment; filename="{Path(artifact).name}"')
            msg.attach(part)

        return msg.as_string()

    def _contract_id(self, eml_path: Path) -> str:
        """Generate a unique contract ID from the file."""
        h = hashlib.sha256(eml_path.read_bytes()).hexdigest()[:16]
        return h
