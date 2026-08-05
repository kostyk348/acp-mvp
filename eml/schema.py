"""JSON Schema validation for agent contracts."""
import json
import jsonschema
from pathlib import Path


def load_schema(name: str = "agent-contract-v1") -> dict:
    """Load the JSON schema from the contracts directory."""
    schema_path = Path(__file__).parent / "contract_schema.json"
    with open(schema_path, "r") as f:
        return json.load(f)


def validate_contract(payload: dict) -> list[str]:
    """Validate a contract payload against the schema.

    Returns an empty list if valid, or a list of error messages.
    """
    schema = load_schema()
    try:
        jsonschema.validate(payload, schema)
        return []
    except jsonschema.ValidationError as e:
        return [f"{e.message} at {' -> '.join(str(p) for p in e.absolute_path)}"]
