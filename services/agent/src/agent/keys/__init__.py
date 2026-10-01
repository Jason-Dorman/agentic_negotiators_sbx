"""Key holder: the instance's root secret and each run's derived signing key (ADR-039).

docs/architecture.md section 3.3. Exposes signing, never key material.
"""

from agent.keys.derivation import SCHEME, KeyDerivation, KeyRefError
from agent.keys.holder import (
    PASSWORD_VARIABLE,
    KeyHolder,
    RootKeyHolder,
    RunSigner,
    SignedTransaction,
)
from agent.keys.references import KEY_REF_FORMS, check_key_reference, is_key_reference

__all__ = [
    "KEY_REF_FORMS",
    "PASSWORD_VARIABLE",
    "SCHEME",
    "KeyDerivation",
    "KeyHolder",
    "KeyRefError",
    "RootKeyHolder",
    "RunSigner",
    "SignedTransaction",
    "check_key_reference",
    "is_key_reference",
]
