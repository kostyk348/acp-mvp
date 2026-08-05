"""EML contract parser — extracts JSON payload and validates from raw .eml."""

import json
import re
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any

from .schema import validate_contract


class ContractParser:
    """Parses incoming .eml messages and extracts contract payloads."""

    def __init__(self):
        self._parser = BytesParser(policy=policy.default)

    def parse(self, eml_content: bytes | str) -> dict[str, Any]:
        """Parse a raw .eml message and return structured contract data.

        Returns dict with:
          - headers: dict of all MIME headers
          - payload: the JSON contract body
          - attachments: list of (filename, bytes) tuples
          - valid: bool
          - errors: list[str]
        """
        if isinstance(eml_content, str):
            eml_content = eml_content.encode("utf-8")

        msg = self._parser.parsebytes(eml_content)

        # Extract headers
        headers = {}
        for key in msg.keys():
            headers[key] = msg[key]

        # Extract JSON payload from the main body
        payload = None
        attachments = []
        errors = []

        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                filename = part.get_filename()

                if content_type == "application/json" or (
                    content_type == "text/plain" and not filename
                ):
                    body = part.get_payload(decode=True)
                    if body:
                        try:
                            payload = json.loads(body.decode("utf-8"))
                        except json.JSONDecodeError as e:
                            errors.append(f"JSON parse error: {e}")
                elif content_type == "application/octet-stream" or filename:
                    data = part.get_payload(decode=True)
                    if data is not None:
                        attachments.append((filename or "attachment.bin", data))
        else:
            body = msg.get_payload(decode=True)
            if body:
                try:
                    payload = json.loads(body.decode("utf-8"))
                except json.JSONDecodeError as e:
                    errors.append(f"JSON parse error: {e}")

        # Validate payload
        if payload is not None:
            validation_errors = validate_contract(payload)
            errors.extend(validation_errors)

        return {
            "headers": headers,
            "payload": payload,
            "attachments": attachments,
            "valid": len(errors) == 0 and payload is not None,
            "errors": errors,
            "message_id": headers.get("Message-ID"),
            "from": headers.get("From"),
            "to": headers.get("To"),
            "subject": headers.get("Subject"),
        }

    def parse_file(self, path: str | Path) -> dict[str, Any]:
        """Parse an .eml file from disk."""
        path = Path(path)
        with open(path, "rb") as f:
            return self.parse(f.read())
