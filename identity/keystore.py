"""ECDSA identity and encrypted keystore for ACP agents."""

import json
import os
import logging
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.backends import default_backend

logger = logging.getLogger("acp.identity")


class KeyStore:
    """Manages ECDSA keypair for agent identity."""

    def __init__(self, keystore_path: str = "~/.agent/keystore.json", password: str | None = None):
        self.keystore_path = Path(keystore_path).expanduser()
        self.password = password or os.environ.get("ACP_AGENT_PASSWORD", "change-me")
        self._private_key: ec.EllipticCurvePrivateKey | None = None
        self._public_key: ec.EllipticCurvePublicKey | None = None

    def _derive_key(self, salt: bytes) -> bytes:
        """Derive AES key from password using PBKDF2."""
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=100_000,
            backend=default_backend(),
        )
        return kdf.derive(self.password.encode("utf-8"))

    def generate_keypair(self) -> dict:
        """Generate a new ECDSA keypair and store it encrypted."""
        private_key = ec.generate_private_key(ec.SECP256R1(), default_backend())
        public_key = private_key.public_key()

        # Use full uncompressed public key: 0x04 + x (32 bytes) + y (32 bytes) = 65 bytes
        public_numbers = public_key.public_numbers()
        public_bytes = b"\x04" + public_numbers.x.to_bytes(32, "big") + public_numbers.y.to_bytes(32, "big")
        agent_id = "0x" + public_bytes.hex()

        # Encrypt and store
        self._private_key = private_key
        self._public_key = public_key
        self._save(agent_id)

        return {"agent_id": agent_id, "public_key": public_bytes.hex()}

    def load_keypair(self) -> dict | None:
        """Load existing keypair from keystore."""
        if not self.keystore_path.exists():
            return None

        try:
            with open(self.keystore_path, "r") as f:
                data = json.load(f)

            salt = bytes.fromhex(data["salt"])
            ciphertext = bytes.fromhex(data["ciphertext"])
            nonce = bytes.fromhex(data["nonce"])

            aes_key = self._derive_key(salt)
            aesgcm = AESGCM(aes_key)
            private_bytes = aesgcm.decrypt(nonce, ciphertext, None)

            self._private_key = serialization.load_der_private_key(
                private_bytes, password=None, backend=default_backend()
            )
            self._public_key = self._private_key.public_key()

            # Reconstruct full uncompressed public key
            public_numbers = self._public_key.public_numbers()
            public_bytes = b"\x04" + public_numbers.x.to_bytes(32, "big") + public_numbers.y.to_bytes(32, "big")
            agent_id = "0x" + public_bytes.hex()

            return {"agent_id": agent_id, "public_key": public_bytes.hex()}
        except Exception as e:
            logger.error(f"Failed to load keystore: {e}")
            return None

    def sign(self, message: bytes) -> str:
        """Sign a message with the agent's private key. Returns hex signature."""
        if self._private_key is None:
            loaded = self.load_keypair()
            if loaded is None:
                raise ValueError("No keypair available — generate first")

        signature = self._private_key.sign(message, ec.ECDSA(hashes.SHA256()))
        return signature.hex()

    def verify(self, message: bytes, signature_hex: str, public_key_hex: str) -> bool:
        """Verify a signature against a public key.

        public_key_hex: full uncompressed key (0x04 + x + y, 130 hex chars).
        """
        try:
            public_bytes = bytes.fromhex(public_key_hex)
            if public_bytes[0] == 0x04 and len(public_bytes) == 65:
                x_bytes = public_bytes[1:33]
                y_bytes = public_bytes[33:65]
                x = int.from_bytes(x_bytes, "big")
                y = int.from_bytes(y_bytes, "big")
                curve = ec.SECP256R1()
                public_numbers = ec.EllipticCurvePublicNumbers(x, y, curve)
                public_key = public_numbers.public_key(default_backend())
            else:
                raise ValueError("Unsupported public key format")

            signature = bytes.fromhex(signature_hex)
            public_key.verify(signature, message, ec.ECDSA(hashes.SHA256()))
            return True
        except Exception:
            return False

    def get_agent_id(self) -> str | None:
        """Get the agent ID (public key hex)."""
        keypair = self.load_keypair()
        if keypair is None:
            return None
        return keypair["agent_id"]

    def _save(self, agent_id: str):
        """Encrypt and save the private key."""
        private_bytes = self._private_key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

        salt = os.urandom(16)
        nonce = os.urandom(12)
        aes_key = self._derive_key(salt)
        aesgcm = AESGCM(aes_key)
        ciphertext = aesgcm.encrypt(nonce, private_bytes, None)

        self.keystore_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "agent_id": agent_id,
            "salt": salt.hex(),
            "nonce": nonce.hex(),
            "ciphertext": ciphertext.hex(),
            "public_key": self._public_key.public_numbers().x.to_bytes(32, "big").hex(),
            "public_key_y": self._public_key.public_numbers().y.to_bytes(32, "big").hex(),
        }
        with open(self.keystore_path, "w") as f:
            json.dump(data, f, indent=2)

        # Restrict file permissions
        os.chmod(self.keystore_path, 0o600)
