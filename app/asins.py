"""The ASIN set every report is requested for: config/asins.txt, deduplicated.

The file lists ASIN/product-name pairs as they appear in the table. An ASIN may be listed
under several names; it is requested once and every name is kept for the log. Names are
reference only: nothing here writes them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .exceptions import ConfigurationError
from .report import validate_asins


@dataclass
class AsinList:
    path: Path
    lines: int
    asins: list[str]  # sorted, unique
    names: dict[str, list[str]] = field(default_factory=dict)  # ASIN -> names in file order
    invalid: list[tuple[int, str]] = field(default_factory=list)  # (line number, text)

    def repeated(self) -> dict[str, list[str]]:
        """ASINs listed more than once, with every name they were listed under."""
        return {asin: names for asin, names in self.names.items() if len(names) > 1}

    def summary(self, expected: int) -> list[str]:
        lines = [f"{self.path.name}: {self.lines} lines, {len(self.asins)} unique ASINs (expected {expected})"]
        for asin, names in self.repeated().items():
            lines.append(f"  {asin} listed {len(names)} times: {' | '.join(names)}")
        for number, text in self.invalid:
            lines.append(f"  line {number} is not 'ASIN  name': {text!r}")
        return lines


def load_asin_list(path: Path) -> AsinList:
    if not path.is_file():
        raise ConfigurationError(f"ASIN list {path} does not exist")
    count, names, invalid = 0, {}, []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        count += 1
        token, _, name = text.partition(" ")
        valid, _ = validate_asins([token])
        if not valid:
            invalid.append((number, text))
            continue
        names.setdefault(valid[0], []).append(name.strip())
    if not names:
        raise ConfigurationError(f"ASIN list {path} has no ASINs")
    return AsinList(path, count, sorted(names), names, invalid)
