"""Locate and load the package's data files: schemas, fixtures and ABI artefacts.

`schemas/`, `fixtures/` and `abi/` sit beside `src/` rather than inside the import package,
because docs/contributing.md section 1 gives them those paths and three languages read them
there. That means they are not automatically part of the wheel, so `pyproject.toml` force-includes
them under the package directory; this module looks in the packaged location first and falls back
to the repository layout, so an editable checkout and an installed wheel behave the same.

Schema validation is here rather than in each caller so there is one place that knows the schemas
cross-reference each other. `observation.v1.json` refs `mandate.v1.json`, `scenario.v1.json` refs
it too, and `export.v1.json` refs both plus the deployment manifest — a caller that built its own
validator without a registry would get a resolution error, or worse, silently skip the referenced
subschema.
"""

from __future__ import annotations

import json
from copy import deepcopy
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

_PACKAGE_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _PACKAGE_DIR.parents[1]

#: Every schema in the package, by file name. Used to build the reference registry.
SCHEMA_FILES = (
    "agent_decision.v1.json",
    "mandate.v1.json",
    "observation.v1.json",
    "scenario.v1.json",
    "deployment_manifest.v1.json",
    "export.v1.json",
)


def _resource_dir(name: str) -> Path:
    packaged = _PACKAGE_DIR / name
    if packaged.is_dir():
        return packaged
    project = _PROJECT_DIR / name
    if project.is_dir():
        return project
    raise FileNotFoundError(
        f"{name}/ not found beside {_PACKAGE_DIR} or under {_PROJECT_DIR}. "
        "An installed wheel carries it via force-include; a checkout has it in the repository."
    )


def schemas_dir() -> Path:
    return _resource_dir("schemas")


def fixtures_dir() -> Path:
    return _resource_dir("fixtures")


def abi_dir() -> Path:
    return _resource_dir("abi")


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# The cached loaders are private, and the public ones hand out copies.
#
# `@cache` on a function returning a dict gives every caller the *same* object. One caller popping a
# key out of a schema to build a variant, or a test mutating a fixture in place, would silently
# rewrite what every later caller sees -- including the validators, which are built from these. The
# copy is paid on load, not on validation: `validator_for` and `_registry` use the cached originals
# and are themselves cached, so the hot path copies nothing.


@cache
def _cached_schema(name: str) -> dict[str, Any]:
    schema: dict[str, Any] = _load_json(schemas_dir() / name)
    return schema


@cache
def _cached_fixture(name: str) -> dict[str, Any]:
    fixture: dict[str, Any] = _load_json(fixtures_dir() / name)
    return fixture


@cache
def _cached_abi(name: str) -> list[dict[str, Any]]:
    artefact: dict[str, Any] = _load_json(abi_dir() / f"{name}.json")
    abi: list[dict[str, Any]] = artefact["abi"]
    return abi


def load_schema(name: str) -> dict[str, Any]:
    """Load one schema by file name, for example `observation.v1.json`. Returns a fresh copy."""
    return deepcopy(_cached_schema(name))


def load_fixture(name: str) -> dict[str, Any]:
    """Load one fixture by file name, for example `eip712.v1.json`. Returns a fresh copy."""
    return deepcopy(_cached_fixture(name))


def load_abi(name: str) -> list[dict[str, Any]]:
    """Load one ABI artefact by contract name, for example `NegotiationExchange`. A fresh copy."""
    return deepcopy(_cached_abi(name))


@cache
def _registry() -> Registry[Any]:
    """Every schema, addressable both by its `$id` and by its bare file name.

    The file name matters: the schemas reference each other as `mandate.v1.json` rather than by
    absolute `$id`, which is what keeps them loadable from disk by a TypeScript or Solidity
    consumer that has no notion of the `$id` host.
    """
    registry: Registry[Any] = Registry()
    for name in SCHEMA_FILES:
        schema = _cached_schema(name)
        resource = Resource.from_contents(schema)
        registry = resource @ registry
        registry = registry.with_resource(uri=name, resource=resource)
    return registry


@cache
def validator_for(name: str) -> Draft202012Validator:
    """A validator for one schema, with every sibling schema resolvable."""
    Draft202012Validator.check_schema(_cached_schema(name))
    return Draft202012Validator(_cached_schema(name), registry=_registry())


def validate(instance: Any, schema_name: str) -> None:
    """Raise `jsonschema.ValidationError` if `instance` does not match the named schema."""
    validator_for(schema_name).validate(instance)


def iter_errors(instance: Any, schema_name: str) -> list[str]:
    """Every validation message, in document order. For reporting, not for control flow."""
    return [
        f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
        for error in validator_for(schema_name).iter_errors(instance)
    ]
