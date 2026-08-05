"""End-to-end test for ACP MVP — full contract lifecycle."""

import os
import sys
import json
import tempfile
import hashlib
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from eml.generator import ContractGenerator
from eml.parser import ContractParser
from eml.schema import validate_contract
from identity.keystore import KeyStore
from sint_ua.fact_chain import FactChain
from settlement.escrow import EscrowClient


def test_eml_roundtrip():
    """Test: generate .eml → parse .eml → validate payload."""
    print("TEST: EML roundtrip")

    gen = ContractGenerator("test-agent")
    eml_content = gen.generate(
        contract_type="ci.compile",
        to_agent="executor",
        source_ref="base64://aW1wb3J0IG9zCgpkZWYgbWFpbigpOgogICAgcmV0dXJuICJIZWxsbyBXb3JsZCI=",
        language="python",
        verification_tests=["python -m pytest", "python -m build"],
        timeout_seconds=300,
        escrow_contract="0x5FbDB2315678afecb367f032d93F642f64180aa3",
        escrow_budget=100,
        escrow_token="USDC",
    )

    assert isinstance(eml_content, str)
    assert "Message-ID:" in eml_content
    assert "From:" in eml_content
    assert "To:" in eml_content
    assert "Subject:" in eml_content
    assert "X-Agent-Protocol: ACP/0.1" in eml_content
    assert "X-Agent-Contract-Type: ci.compile.python" in eml_content
    assert "X-Escrow-Budget: 100" in eml_content
    assert "X-Escrow-Token: USDC" in eml_content
    assert "Content-Type: multipart/mixed" in eml_content

    # Parse it back
    parser = ContractParser()
    parsed = parser.parse(eml_content)

    assert parsed["valid"], f"Parse validation failed: {parsed['errors']}"
    assert parsed["payload"] is not None
    assert parsed["payload"]["schema"] == "agent-contract/v1"
    assert parsed["payload"]["intent"]["type"] == "ci.compile"
    assert parsed["payload"]["intent"]["language"] == "python"
    assert parsed["payload"]["settlement"]["royaltySplit"]["human"] == 90
    assert parsed["payload"]["settlement"]["royaltySplit"]["agent"] == 10


def test_schema_validation():
    """Test: valid contract passes, invalid contract fails."""
    print("TEST: Schema validation")

    # Valid contract
    valid = {
        "schema": "agent-contract/v1",
        "intent": {
            "type": "ci.compile",
            "language": "rust",
            "source_ref": "ipfs://QmTest",
            "requirements": {"target": "wasm32-unknown-unknown", "features": ["std"]},
        },
        "verification": {
            "tests": ["cargo test", "cargo build --release"],
            "timeout_seconds": 300,
        },
        "settlement": {
            "onSuccess": "release-escrow",
            "onFailure": "refund",
            "royaltySplit": {"human": 90, "agent": 10},
        },
    }
    errors = validate_contract(valid)
    assert errors == [], f"Valid contract should have no errors: {errors}"

    # Invalid: missing required field
    invalid = {
        "schema": "agent-contract/v1",
        "intent": {
            "type": "ci.compile",
            # missing language and source_ref
        },
        "verification": {
            "tests": ["cargo test"],
            "timeout_seconds": 300,
        },
        "settlement": {
            "onSuccess": "release-escrow",
            "onFailure": "refund",
            "royaltySplit": {"human": 90, "agent": 10},
        },
    }
    errors = validate_contract(invalid)
    assert len(errors) > 0, "Invalid contract should have errors"



def test_escrow_client():
    """Test: escrow client validates and tracks tasks."""
    print("TEST: Escrow client")

    escrow = EscrowClient(min_rate=1)

    # Valid escrow
    valid = escrow.is_valid({
        "contract": "0x5FbDB2315678afecb367f032d93F642f64180aa3",
        "budget": "100",
        "token": "USDC",
    })
    assert valid, "Valid escrow should pass"

    # Too low budget
    low = escrow.is_valid({
        "contract": "0x5FbDB2315678afecb367f032d93F642f64180aa3",
        "budget": "0.001",
        "token": "USDC",
    })
    assert not low, "Low budget should fail"

    # No contract address
    no_addr = escrow.is_valid({
        "contract": "",
        "budget": "100",
        "token": "USDC",
    })
    assert not no_addr, "Missing contract should fail"

    # Create task
    task = escrow.create_task(
        message_id="msg-001",
        budget=100,
        token="USDC",
        timeout="2026-01-01T00:00:00Z",
        customer_address="0xCustomer",
        agent_address="0xAgent",
    )
    assert task["status"] == "CREATED"
    assert escrow.get_balance("msg-001") == 100

    # Submit result
    result = escrow.submit_result("msg-001", "0xhash123", True)
    assert result["status"] == "RELEASED"



def test_identity_keystore():
    """Test: keypair generation, signing, verification."""
    print("TEST: Identity keystore")

    with tempfile.TemporaryDirectory() as tmpdir:
        ks = KeyStore(keystore_path=f"{tmpdir}/keystore.json")

        # Generate keypair
        info = ks.generate_keypair()
        assert "agent_id" in info
        assert info["agent_id"].startswith("0x")
        assert len(info["agent_id"]) == 132  # 0x + 65 bytes uncompressed key

        # Sign message
        message = b"test message for signing"
        signature = ks.sign(message)
        assert len(signature) > 0

        # Verify signature with full uncompressed public key
        public_key_hex = info["public_key"]
        assert ks.verify(message, signature, public_key_hex) is True

        # Wrong signature should fail
        assert ks.verify(b"wrong message", signature, public_key_hex) is False

        # Load keypair from disk
        ks2 = KeyStore(keystore_path=f"{tmpdir}/keystore.json")
        loaded = ks2.load_keypair()
        assert loaded is not None
        assert loaded["agent_id"] == info["agent_id"]



def test_fact_chain():
    """Test: FACT chain recording and querying."""
    print("TEST: Fact chain")

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = f"{tmpdir}/test.db"
        fc = FactChain(db_path)

        # Record facts
        id1 = fc.record(
            content="ci.compile.rust succeeded for contract 0xabc",
            tags=["ci", "rust", "compile", "success"],
            confidence=1.0,
        )
        id2 = fc.record(
            content="ci.compile.python failed for contract 0xdef",
            tags=["ci", "python", "compile", "failure"],
            confidence=1.0,
        )

        assert id1 == 1
        assert id2 == 2

        # Query by tag
        ci_facts = fc.query(tag="ci", min_confidence=0.0, limit=10)
        assert len(ci_facts) == 2

        rust_facts = fc.query(tag="rust", min_confidence=0.0, limit=10)
        assert len(rust_facts) == 1
        assert "rust" in rust_facts[0]["tags"]

        # Verify chain integrity
        integrity = fc.verify_chain()
        assert integrity["valid"], f"Chain broken: {integrity['broken_links']}"
        assert integrity["total"] == 2



def test_sandbox_executor():
    """Test: sandbox executor creates work directory and handles source ref."""
    print("TEST: Sandbox executor")

    with tempfile.TemporaryDirectory() as tmpdir:
        from sandbox.executor import SandboxExecutor

        executor = SandboxExecutor(
            sandbox_base=tmpdir,
            docker_image="alpine:latest",  # Use lightweight image for test
            timeout=10,
        )

        # Test with base64 source ref (valid base64)
        result = executor.execute(
            source_ref="base64://aW1wb3J0IHN5czsgZXhpc3RzID0gWzEyM10=",
            language="python",
            tests=["echo 'test'"],
            timeout=10,
            work_dir=Path(tmpdir) / "test-contract",
        )

        assert "success" in result
        assert "exec_hash" in result
        assert "proof" in result
        assert "exit_codes" in result["proof"]



def run_all_tests():
    """Run all e2e tests."""
    print("=" * 60)
    print("ACP MVP — End-to-End Tests")
    print("=" * 60)

    tests = [
        test_eml_roundtrip,
        test_schema_validation,
        test_escrow_client,
        test_identity_keystore,
        test_fact_chain,
        test_sandbox_executor,
    ]

    passed = 0
    failed = 0

    for test_fn in tests:
        try:
            test_fn()
            passed += 1
        except Exception as e:
            print(f"  ✗ {test_fn.__name__} FAILED: {e}")
            failed += 1

    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 60)

    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
