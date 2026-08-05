"""Simplified escrow client for MVP — interacts with AgentEscrow contract."""

import logging
from typing import Any

logger = logging.getLogger("acp.settlement")


class EscrowClient:
    """Client for the AgentEscrow smart contract.

    For MVP, this is a simulation layer that validates escrow state
    and records settlement events. In production, this would use
    web3.py to interact with the real contract on Sepolia/Anvil.
    """

    def __init__(
        self,
        contract_address: str = "0x5FbDB2315678afecb367f032d93F642f64180aa3",
        rpc_url: str = "http://localhost:8545",
        min_rate: int = 1,  # Minimum budget in tokens
    ):
        self.contract_address = contract_address
        self.rpc_url = rpc_url
        self.min_rate = min_rate
        self._simulated_balances: dict[str, int] = {}

    def is_valid(self, escrow_info: dict) -> bool:
        """Check if escrow is valid for this contract."""
        budget = escrow_info.get("budget")
        if budget is None:
            logger.warning("No budget in escrow info")
            return False

        try:
            budget_val = float(budget)
        except (ValueError, TypeError):
            logger.warning(f"Invalid budget value: {budget}")
            return False

        if budget_val < self.min_rate:
            logger.warning(f"Budget {budget_val} < minimum rate {self.min_rate}")
            return False

        contract = escrow_info.get("contract")
        if not contract or contract == "0x00000000000000000000000000000000000000":
            logger.warning("No escrow contract address")
            return False

        return True

    def create_task(
        self,
        message_id: str,
        budget: int,
        token: str,
        timeout: str,
        customer_address: str,
        agent_address: str,
    ) -> dict[str, Any]:
        """Create an escrow task (simulated for MVP)."""
        task = {
            "messageId": message_id,
            "budget": budget,
            "token": token,
            "timeout": timeout,
            "customer": customer_address,
            "agent": agent_address,
            "status": "CREATED",
        }
        self._simulated_balances[message_id] = budget
        logger.info(f"Escrow task created: {message_id} — {budget} {token}")
        return task

    def submit_result(
        self,
        message_id: str,
        execution_hash: str,
        success: bool,
    ) -> dict[str, Any]:
        """Submit execution result to escrow."""
        if message_id not in self._simulated_balances:
            return {"status": "ERROR", "reason": "task_not_found"}

        status = "RELEASED" if success else "REFUNDED"
        logger.info(f"Escrow result submitted for {message_id}: {status}")
        return {
            "messageId": message_id,
            "executionHash": execution_hash,
            "status": status,
        }

    def get_balance(self, message_id: str) -> int | None:
        """Get simulated escrow balance."""
        return self._simulated_balances.get(message_id)
