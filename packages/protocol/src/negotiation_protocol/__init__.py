"""Shared protocol definitions: schemas, ABI artefacts, fixtures and reason tables.

The normative source is docs/protocol.md; this package is its machine-readable form and nothing
more. It imports nothing from services or apps (docs/contributing.md section 1), and `web3` is an
optional extra so that installing it cannot give the agent service an RPC client (ADR-035).
"""

from negotiation_protocol.eip712 import (
    ACCEPT_TYPE,
    CLOSE_TYPE,
    DOMAIN_NAME,
    DOMAIN_VERSION,
    EIP712_DOMAIN_TYPE,
    OFFER_TYPE,
    Accept,
    Close,
    Domain,
    Offer,
    ProtocolValueError,
    SessionConfig,
    recover_signer,
    signature_is_canonical,
    to_bytes32,
    to_hex32,
)
from negotiation_protocol.reasons import (
    ABORT_REASON_CODES,
    ABORT_REASONS,
    CLOSE_REASON_CODES,
    CLOSE_REASONS,
    AbortReason,
    CloseReason,
    UnknownReasonCodeError,
    abort_reason_code,
    abort_reason_name,
    close_reason_code,
    close_reason_name,
)
from negotiation_protocol.resources import (
    SCHEMA_FILES,
    abi_dir,
    fixtures_dir,
    iter_errors,
    load_abi,
    load_fixture,
    load_schema,
    schemas_dir,
    validate,
    validator_for,
)

__all__ = [
    "ABORT_REASONS",
    "ABORT_REASON_CODES",
    "ACCEPT_TYPE",
    "CLOSE_REASONS",
    "CLOSE_REASON_CODES",
    "CLOSE_TYPE",
    "DOMAIN_NAME",
    "DOMAIN_VERSION",
    "EIP712_DOMAIN_TYPE",
    "OFFER_TYPE",
    "PROTOCOL_VERSION",
    "SCHEMA_FILES",
    "AbortReason",
    "Accept",
    "Close",
    "CloseReason",
    "Domain",
    "Offer",
    "ProtocolValueError",
    "SessionConfig",
    "UnknownReasonCodeError",
    "abi_dir",
    "abort_reason_code",
    "abort_reason_name",
    "close_reason_code",
    "close_reason_name",
    "fixtures_dir",
    "iter_errors",
    "load_abi",
    "load_fixture",
    "load_schema",
    "recover_signer",
    "schemas_dir",
    "signature_is_canonical",
    "to_bytes32",
    "to_hex32",
    "validate",
    "validator_for",
]

# docs/protocol.md section 15. Frozen at release v0.1.
PROTOCOL_VERSION = 1
