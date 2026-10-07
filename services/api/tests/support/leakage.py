"""The isolation suite's scanner: named private values, looked for in any text (A12).

A `Needle` is one private thing — a party's reservation price, its instructions, one of its
validation feedback sentences, its explanation, a key — under a label that says whose it is and
what it is. `Scanner.found(text)` returns the labels found, never the text around them, so a
failure message points at the leak without repeating it into the test log.

Every surface the suite scans is JSON — a request whose observation is JSON inside JSON, an SSE
data line, a run event, the export, a log line — so a text needle is looked for as written and as
JSON writes it inside a string: escaped once and twice, with and without `ensure_ascii`. A private
sentence holding a quote, a backslash or a non-ASCII character is found wherever it travels (stage
3.3 review). Amounts are looked for as whole numbers, in minor units, as six-place decimals and as
the product renders them in a sentence (`format_minor`, which trims trailing zeros), so `97531246`
is not found inside `197531246` and is found in `97.531246 mUSD`. An amount that is a whole number
of tokens is refused as a needle: its rendering, `99`, is a number any text may hold, so the suite
chooses private amounts with a fractional part. Hex keys are looked for in any case, with or
without `0x`. The suite chooses its mandates so that no public value — an offer, a balance, a
timestamp — can equal a private one: a scan that cannot be confused is the point.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from api.projection.sentences import format_minor


@dataclass(frozen=True)
class Needle:
    label: str
    pattern: re.Pattern[str] = field(repr=False)

    @classmethod
    def text(cls, label: str, value: str) -> Needle:
        assert value, f"{label}: an empty needle would match everything"
        forms = {value}
        for ascii_only in (False, True):
            once = json.dumps(value, ensure_ascii=ascii_only)[1:-1]
            forms |= {once, json.dumps(once, ensure_ascii=ascii_only)[1:-1]}
        pattern = "|".join(re.escape(form) for form in sorted(forms, key=len, reverse=True))
        return cls(label, re.compile(pattern))

    @classmethod
    def amount(cls, label: str, minor: str | int, decimals: int = 6) -> Needle:
        value = int(minor)
        assert value % 10**decimals, f"{label}: a whole number of tokens cannot be scanned for"
        decimal = f"{Decimal(value).scaleb(-decimals):.{decimals}f}"
        forms = "|".join(re.escape(form) for form in {str(value), decimal, format_minor(value)})
        return cls(label, re.compile(rf"(?<!\d)(?:{forms})(?!\d)"))

    @classmethod
    def hex_key(cls, label: str, key: str) -> Needle:
        bare = key.lower().removeprefix("0x")
        assert len(bare) == 64, f"{label}: not a 32-byte key"
        return cls(label, re.compile(re.escape(bare), re.IGNORECASE))


class Scanner:
    def __init__(self, needles: Iterable[Needle]) -> None:
        self.needles: Sequence[Needle] = tuple(needles)
        labels = [needle.label for needle in self.needles]
        assert len(labels) == len(set(labels)), "every needle needs its own label"

    def found(self, text: str) -> list[str]:
        """The labels of the needles in `text`, in the scanner's order."""
        return [needle.label for needle in self.needles if needle.pattern.search(text)]

    def only(self, prefix: str) -> Scanner:
        """The needles whose label starts with `prefix`: one party's, say."""
        return Scanner(needle for needle in self.needles if needle.label.startswith(prefix))

    def __add__(self, other: Scanner) -> Scanner:
        return Scanner([*self.needles, *other.needles])


CREDENTIAL_SHAPES = Scanner(
    [
        Needle("credential:anthropic_key_shaped", re.compile(r"sk-ant-[A-Za-z0-9_-]+")),
        Needle("credential:pem_private_key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY")),
        Needle("credential:keystore_document", re.compile(r'\\*"(?:ciphertext|kdfparams)\\*"')),
    ]
)
