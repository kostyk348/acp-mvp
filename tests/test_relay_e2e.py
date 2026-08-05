"""End-to-end test of the ACP Relay internet bridge.

Simulates three OpenCode agent nodes on the internet:
  - publisher (customer)  — sends a contract to the relay
  - relay                 — routes by capability subscription
  - subscriber (executor) — receives the contract in its spool

Run: python tests/test_relay_e2e.py
"""

import os
import sys
import time
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from aiosmtpd.controller import Controller

from eml.generator import ContractGenerator
from eml.parser import ContractParser


class SpoolHandler:
    """Minimal SMTP endpoint that dumps incoming .eml into a spool dir."""

    def __init__(self, spool_dir: str):
        self.spool_dir = Path(spool_dir)
        self.spool_dir.mkdir(parents=True, exist_ok=True)

    async def handle_DATA(self, server, session, envelope):
        idx = len(list(self.spool_dir.glob("*.eml")))
        (self.spool_dir / f"{idx:06d}.eml").write_bytes(envelope.content)
        return "250 OK"


def test_relay_internet_bridge():
    print("TEST: relay internet bridge (publisher -> relay -> subscriber)")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        relay_db = str(tmp / "relay.db")
        sub_spool = str(tmp / "subscriber" / "inbox")

        # 1. Subscriber SMTP endpoint (an OpenCode node)
        sub_handler = SpoolHandler(sub_spool)
        sub_controller = Controller(sub_handler, hostname="127.0.0.1", port=2531)
        sub_controller.start()

        # 2. Relay with subscription: executor subscribes to ci.compile
        from transport.relay import RelayServer
        relay = RelayServer(host="127.0.0.1", port=2530, db_path=relay_db)
        relay.registry.subscribe("executor", "ci.compile", "127.0.0.1", 2531)
        relay.start()

        time.sleep(0.5)

        # 3. Publisher (customer OpenCode node) sends a contract via relay
        gen = ContractGenerator("customer")
        eml_content = gen.generate(
            contract_type="ci.compile",
            domain="python",
            source_type="base64",
            source_data="cHJpbnQoJ2hpJyk=",
            language="python",
            verification_tests=[{"cmd": "python -m pytest", "timeout": 60}],
            escrow_contract="0x5FbDB2315678afecb367f032d93F642f64180aa3",
            escrow_budget=100,
            escrow_token="USDC",
        )

        import smtplib
        with smtplib.SMTP("127.0.0.1", 2530, timeout=10) as smtp:
            smtp.sendmail(
                "customer@mesh.local",
                ["broadcast+capability.ci.compile@mesh.local"],
                eml_content,
            )

        # 4. Wait for fan-out, then check subscriber spool
        deadline = time.time() + 10
        delivered = False
        while time.time() < deadline:
            files = list(Path(sub_spool).glob("*.eml"))
            if files:
                delivered = True
                break
            time.sleep(0.3)

        assert delivered, "contract was NOT delivered to subscriber"

        # 5. Parse the delivered contract
        parser = ContractParser()
        parsed = parser.parse_file(files[0])
        assert parsed["valid"], f"delivered contract invalid: {parsed['errors']}"
        assert parsed["payload"]["intent"]["type"] == "ci.compile"
        assert parsed["payload"]["settlement"]["royalty_split"]["agent_executor"] == 10
        print(f"  ✓ contract delivered: {parsed['message_id']}")
        print(f"  ✓ capability routing: broadcast+capability.ci.compile → executor")

        relay.stop()
        sub_controller.stop()


if __name__ == "__main__":
    test_relay_internet_bridge()
    print("PASS")
