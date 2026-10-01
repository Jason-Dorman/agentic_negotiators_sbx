"""What a key reference may look like, checked before anything reads it (ADR-049).

The grammar is `negotiation_protocol.key_refs` since stage 2.3, when the backend became the second
service to hold keys by reference; this module re-exports it so the agent's own imports, and the
reasoning recorded there, stay where they were. See that module for the grammar and why it exists.
"""

from __future__ import annotations

from negotiation_protocol.key_refs import KEY_REF_FORMS, check_key_reference, is_key_reference

__all__ = ["KEY_REF_FORMS", "check_key_reference", "is_key_reference"]
