"""CLI entry point for the ACP agent node."""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core import Orchestrator
from transport.mta import AgentMTA
from identity.keystore import KeyStore


def main():
    parser = argparse.ArgumentParser(
        description="ACP Agent Node — Agent Contract Protocol MVP"
    )
    parser.add_argument(
        "--role",
        choices=["customer", "executor", "relay", "all"],
        default="all",
        help="Agent role (default: all)",
    )
    parser.add_argument(
        "--agent-id",
        default="agent-1",
        help="Agent identifier",
    )
    parser.add_argument(
        "--inbox",
        default="/var/spool/agent/inbox",
        help="Inbox directory for .eml files",
    )
    parser.add_argument(
        "--smtp-port",
        type=int,
        default=2525,
        help="SMTP MTA port",
    )
    parser.add_argument(
        "--sandbox",
        default="/tmp/agent-sandbox",
        help="Sandbox working directory",
    )
    parser.add_argument(
        "--knowledge-db",
        default="~/.agent/knowledge.db",
        help="Path to knowledge SQLite database",
    )
    parser.add_argument(
        "--keystore",
        default="~/.agent/keystore.json",
        help="Path to encrypted keystore",
    )
    parser.add_argument(
        "--password",
        default=None,
        help="Keystore password (or set ACP_AGENT_PASSWORD env var)",
    )
    parser.add_argument(
        "--process-inbox",
        action="store_true",
        help="Process inbox and exit",
    )
    parser.add_argument(
        "--generate-contract",
        action="store_true",
        help="Generate a sample contract and save to disk",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    if args.password:
        import os
        os.environ["ACP_AGENT_PASSWORD"] = args.password

    async def run():
        # Initialize identity
        keystore = KeyStore(keystore_path=args.keystore)
        keypair = keystore.load_keypair()
        if keypair is None:
            print(f"Generating new keypair for {args.agent_id}...")
            keypair = keystore.generate_keypair()
        print(f"Agent ID: {keypair['agent_id']}")

        # Initialize orchestrator
        orchestrator = Orchestrator(
            agent_id=args.agent_id,
            inbox_dir=args.inbox,
            sandbox_base=args.sandbox,
            knowledge_db=args.knowledge_db,
            keystore_path=args.keystore,
        )

        if args.generate_contract:
            # Generate a sample contract
            from eml.generator import ContractGenerator
            gen = ContractGenerator(args.agent_id)
            eml = gen.generate(
                contract_type="ci.compile",
                to_agent="executor",
                source_ref="base64://aW1wb3J0IHN5cwpleGlzdHMgPSBbMTIzXQ==",
                language="python",
                escrow_budget=100,
                escrow_token="USDC",
            )
            out_path = Path(args.inbox) / "sample-contract.eml"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "w") as f:
                f.write(eml)
            print(f"Sample contract written to {out_path}")
            return

        if args.process_inbox:
            results = orchestrator.process_inbox()
            for r in results:
                print(f"Contract {r.get('contract_id', '?')}: {r.get('status', '?')}")
            return

        # Start MTA + process loop
        mta = AgentMTA(port=args.smtp_port, inbox_dir=args.inbox)
        await mta.start()
        print(f"SMTP MTA listening on port {args.smtp_port}")

        try:
            while True:
                await asyncio.sleep(5)
                results = orchestrator.process_inbox()
                for r in results:
                    status = r.get("status", "?")
                    cid = r.get("contract_id", "?")
                    print(f"Processed {cid}: {status}")
        except KeyboardInterrupt:
            print("\nShutting down...")
        finally:
            await mta.stop()

    asyncio.run(run())


if __name__ == "__main__":
    main()
