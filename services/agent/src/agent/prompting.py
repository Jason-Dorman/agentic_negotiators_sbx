"""The model policy's prompts: versioned template files, rendered per run (ADR-029).

The templates live in `services/agent/prompts/<version>/`, reviewed like any other source. The
system prompt is rendered once per run — the role, the protocol rules, the output schema, then the
mandate's `instructions` in a delimited section introduced as the agent's own private guidance — so
it is static for the run and the model client can put it behind a cache breakpoint. The
observation is the user message that follows it. A repair attempt adds one more text block, this
agent's own validation code and feedback, and nothing else.

The delimiter around the instructions is named by a hash of the instructions themselves. Text that
tries to close the section early would have to contain the hash of a string that contains it, so
the section ends where the template ends it.

`version` is what every decision records as `prompt_template_version`: the directory's name and a
hash of both template files and the schema the prompt embeds. The same recorded version means the
same text, whether or not someone remembered to make a new directory.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Final

import anthropic
from pydantic import BaseModel

from agent.observation import Role

#: The template every model run uses today.
CURRENT_VERSION: Final = "v1.0.0"
_PACKAGED: Final = Path(__file__).resolve().parent / "_prompts"
_SOURCE: Final = Path(__file__).resolve().parents[2] / "prompts"


class PromptTemplateError(Exception):
    """A template directory is missing a file, or a file names a placeholder it is not given."""


def template_root() -> Path:
    """The wheel's copy when installed, the source tree's otherwise."""
    return _PACKAGED if _PACKAGED.is_dir() else _SOURCE


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    name: str
    version: str
    _system: Template = field(repr=False)
    _repair: Template = field(repr=False)
    _schema: str = field(repr=False)

    @classmethod
    def load(
        cls,
        schema: type[BaseModel],
        *,
        name: str = CURRENT_VERSION,
        root: Path | None = None,
    ) -> PromptTemplate:
        directory = (root or template_root()) / name
        try:
            system = (directory / "system.md").read_text(encoding="utf-8")
            repair = (directory / "repair.md").read_text(encoding="utf-8")
        except OSError as error:
            raise PromptTemplateError(f"cannot read prompt template {name}: {error}") from None
        rendered_schema = json.dumps(anthropic.transform_schema(schema), indent=2)
        digest = hashlib.sha256()
        for part in (system, repair, rendered_schema):
            encoded = part.encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        template = cls(
            name,
            f"{name}+{digest.hexdigest()[:16]}",
            Template(system),
            Template(repair),
            rendered_schema,
        )
        template._check()
        return template

    def system_prompt(self, role: Role, instructions: str) -> str:
        """The run's system prompt. `instructions` is inserted as a value and never scanned for
        placeholders, so it cannot reach into the template."""
        return self._system.substitute(
            role=role,
            role_upper=role.upper(),
            decision_schema=self._schema,
            guidance_id=hashlib.sha256(instructions.encode("utf-8")).hexdigest()[:16],
            instructions=instructions,
        )

    def repair_message(self, code: str, feedback: str) -> str:
        """The repair block: this agent's own refusal of the attempt before, and nothing else."""
        return self._repair.substitute(code=code, feedback=feedback)

    def _check(self) -> None:
        """Every placeholder each file names is one it is given, so a typo fails at start-up."""
        try:
            self.system_prompt("buyer", "")
            self.repair_message("schema_error", "")
        except (KeyError, ValueError) as error:
            raise PromptTemplateError(
                f"prompt template {self.name} has an unknown or malformed placeholder: {error}"
            ) from None


__all__ = ["CURRENT_VERSION", "PromptTemplate", "PromptTemplateError", "template_root"]
