"""ACP REST API — control plane for an agent node (aiohttp).

Endpoints (per expanded ACP spec):
  GET  /health                 — node liveness + escrow status
  GET  /inbox                  — list inbound contract messages
  GET  /inbox/{message_id}     — read one inbound message
  POST /outbox                 — publish a contract to the mesh (SMTP relay)
  GET  /escrow/{message_id}    — escrow task state
  GET  /facts                  — sint-ua fact chain query
  GET  /reputation/{agent_id}  — on-chain reputation of an agent
  POST /sandbox/test           — dry-run verification in sandbox (no funds)

Run:
  python -m api.server
"""

import asyncio
import json
import logging
import os
from pathlib import Path

from aiohttp import web

from eml.generator import ContractGenerator
from eml.parser import ContractParser
from settlement.escrow import EscrowClient
from sint_ua.fact_chain import FactChain

logger = logging.getLogger("acp.api")

SPOOL_DIR = Path(os.environ.get("ACP_INBOX", "spool/executor"))
SMTP_RELAY = os.environ.get("ACP_SMTP_RELAY", "localhost")
SMTP_PORT = int(os.environ.get("ACP_RELAY_PORT", "2525"))
ESCROW_CONTRACT = os.environ.get("ACP_ESCROW_CONTRACT", "0x5FbDB2315678afecb367f032d93F642f64180aa3")
RPC_URL = os.environ.get("ACP_RPC_URL", "http://localhost:8545")
DB_PATH = os.environ.get("ACP_DB_PATH", os.path.expanduser("~/.agent/knowledge.db"))


def _read_spool() -> list[dict]:
    """List .eml files in the spool inbox."""
    if not SPOOL_DIR.exists():
        return []
    parser = ContractParser()
    messages = []
    for eml_file in sorted(SPOOL_DIR.glob("*.eml")):
        parsed = parser.parse_file(eml_file)
        messages.append({
            "message_id": parsed.get("message_id"),
            "from": parsed.get("from"),
            "to": parsed.get("to"),
            "subject": parsed.get("subject"),
            "valid": parsed.get("valid", False),
            "contract_type": (parsed.get("payload") or {}).get("intent", {}).get("type"),
            "path": str(eml_file),
        })
    return messages


async def handle_health(request: web.Request) -> web.Response:
    escrow = EscrowClient(contract_address=ESCROW_CONTRACT, rpc_url=RPC_URL)
    return web.json_response({
        "status": "ok",
        "agent_id": os.environ.get("ACP_AGENT_ID", "unknown"),
        "role": os.environ.get("ACP_AGENT_ROLE", "unknown"),
        "escrow_contract": ESCROW_CONTRACT,
        "rpc_url": RPC_URL,
        "spool_messages": len(_read_spool()),
    })


async def handle_inbox(request: web.Request) -> web.Response:
    return web.json_response({"messages": _read_spool()})


async def handle_inbox_one(request: web.Request) -> web.Response:
    message_id = request.match_info["message_id"]
    if not SPOOL_DIR.exists():
        return web.json_response({"error": "not_found"}, status=404)
    parser = ContractParser()
    for eml_file in SPOOL_DIR.glob("*.eml"):
        parsed = parser.parse_file(eml_file)
        if parsed.get("message_id") and message_id in parsed["message_id"]:
            return web.json_response(parsed)
    return web.json_response({"error": "not_found"}, status=404)


async def handle_outbox(request: web.Request) -> web.Response:
    """Publish a contract to the mesh via SMTP relay."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid_json"}, status=400)

    gen = ContractGenerator(agent_id=os.environ.get("ACP_AGENT_ID", "customer"))
    try:
        eml_content = gen.generate(
            contract_type=body.get("contract_type", "ci.compile"),
            domain=body.get("domain", "rust"),
            source_type=body.get("source_type", "base64"),
            source_data=body.get("source_data", ""),
            language=body.get("language", "rust"),
            verification_tests=body.get("verification_tests"),
            escrow_contract=body.get("escrow_contract", ESCROW_CONTRACT),
            escrow_budget=body.get("escrow_budget", 100),
            escrow_token=body.get("escrow_token", "USDC"),
            royalty_human=body.get("royalty_human", 90),
            royalty_agent=body.get("royalty_agent", 10),
            auto_verify=body.get("auto_verify", True),
        )
    except ValueError as e:
        return web.json_response({"error": "contract_invalid", "detail": str(e)}, status=400)

    # MVP: drop into local outbox (SMTP relay wiring is transport/mta.py).
    out_dir = Path("spool/customer")
    out_dir.mkdir(parents=True, exist_ok=True)
    message_id = ContractGenerator.extract_message_id(eml_content)
    out_file = out_dir / f"{message_id or 'msg'}.eml"
    out_file.write_text(eml_content)

    return web.json_response({
        "status": "queued",
        "message_id": message_id,
        "path": str(out_file),
    }, status=201)


async def handle_escrow(request: web.Request) -> web.Response:
    message_id = request.match_info["message_id"]
    escrow = EscrowClient(contract_address=ESCROW_CONTRACT, rpc_url=RPC_URL)
    return web.json_response({
        "message_id": message_id,
        "status": "CREATED" if escrow.get_balance(message_id) is not None else "unknown",
        "balance": escrow.get_balance(message_id),
    })


async def handle_facts(request: web.Request) -> web.Response:
    fc = FactChain(DB_PATH)
    tag = request.query.get("tag")
    register = request.query.get("register")
    limit = int(request.query.get("limit", "20"))
    facts = fc.query(tag=tag, register=register, min_confidence=0.0, limit=limit)
    return web.json_response({"facts": facts, "total": len(facts)})


async def handle_reputation(request: web.Request) -> web.Response:
    agent_id = request.match_info["agent_id"]
    # MVP: simulated reputation (on-chain query via web3 in production)
    return web.json_response({"agent_id": agent_id, "reputation": 0, "source": "simulated"})


async def handle_sandbox_test(request: web.Request) -> web.Response:
    """Dry-run verification: execute in sandbox without touching funds."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid_json"}, status=400)

    from sandbox.executor import SandboxExecutor
    import tempfile
    executor = SandboxExecutor(docker_image=os.environ.get("ACP_SANDBOX_IMAGE", "alpine:latest"), timeout=30)
    with tempfile.TemporaryDirectory() as tmpdir:
        result = executor.execute(
            source_ref=body.get("source_ref", ""),
            language=body.get("language", "python"),
            tests=body.get("tests", ["echo 'no tests'"]),
            timeout=30,
            work_dir=Path(tmpdir) / "test",
        )
    return web.json_response(result)


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_get("/inbox", handle_inbox)
    app.router.add_get("/inbox/{message_id}", handle_inbox_one)
    app.router.add_post("/outbox", handle_outbox)
    app.router.add_get("/escrow/{message_id}", handle_escrow)
    app.router.add_get("/facts", handle_facts)
    app.router.add_get("/reputation/{agent_id}", handle_reputation)
    app.router.add_post("/sandbox/test", handle_sandbox_test)
    return app


def main():
    logging.basicConfig(level=logging.INFO)
    port = int(os.environ.get("ACP_API_PORT", "8080"))
    web.run_app(create_app(), host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
