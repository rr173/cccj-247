"""Validated migration specification: spaces, rules and affine code shifts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .intervals import Interval, intersect_pieces, normalize, total_width

MAPPING = "map"
DISABLE = "disable"


def _require_int(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{what} must be an integer, got {value!r}")
    return value


@dataclass(frozen=True)
class Shift:
    """Affine old-code -> new-code transform ``direction * old + offset``.

    ``direction`` is restricted to +1 or -1, so the transform is always a
    bijection on the integers and has an exact inverse.
    """

    direction: int
    offset: int

    def __post_init__(self) -> None:
        _require_int(self.direction, "shift direction")
        _require_int(self.offset, "shift offset")
        if self.direction not in (1, -1):
            raise ValueError(f"shift direction must be +1 or -1, got {self.direction}")

    def apply(self, old: int) -> int:
        return self.direction * old + self.offset

    def inverse_point(self, new: int) -> int:
        """Exact inverse of :meth:`apply`."""
        return self.direction * new - self.direction * self.offset

    def inverse_shift(self) -> "Shift":
        return Shift(self.direction, -self.direction * self.offset)

    def image(self, source: Interval) -> Interval:
        a = self.apply(source.lo)
        b = self.apply(source.hi)
        return Interval(min(a, b), max(a, b))

    def inverse_interval(self, target: Interval) -> Interval:
        a = self.inverse_point(target.lo)
        b = self.inverse_point(target.hi)
        return Interval(min(a, b), max(a, b))

    def describe(self) -> str:
        sign = "+" if self.direction == 1 else "-"
        return f"new = {sign}old {'+' if self.offset >= 0 else '-'} {abs(self.offset)}"

    def inverse_describe(self) -> str:
        shift = self.inverse_shift()
        sign = "+" if shift.direction == 1 else "-"
        return f"old = {sign}new {'+' if shift.offset >= 0 else '-'} {abs(shift.offset)}"


@dataclass(frozen=True)
class Rule:
    """One priority rule.

    The rule applies only on ``source_range`` **intersected with** the legal
    domain of ``source_space``.  A mapping rule produces
    ``direction * old + offset`` in ``target_space``; a disable rule says that
    those old codes are deliberately retired.
    """

    id: str
    source_space: str
    source_range: Interval
    priority: int
    action: str
    target_space: str | None = None
    shift: Shift | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("rule id must be a non-empty string")
        if not isinstance(self.source_space, str) or not self.source_space:
            raise ValueError("rule source_space must be a non-empty string")
        if not isinstance(self.source_range, Interval):
            raise TypeError("rule source_range must be an Interval")
        _require_int(self.priority, f"priority of rule {self.id!r}")
        if self.action not in (MAPPING, DISABLE):
            raise ValueError(f"rule {self.id!r}: unknown action {self.action!r}")
        if self.action == MAPPING:
            if not isinstance(self.target_space, str) or not self.target_space:
                raise ValueError(f"mapping rule {self.id!r} needs a target_space")
            if not isinstance(self.shift, Shift):
                raise ValueError(f"mapping rule {self.id!r} needs a shift")
        else:
            if self.target_space is not None or self.shift is not None:
                raise ValueError(f"disable rule {self.id!r} must not have target fields")

    @property
    def is_disable(self) -> bool:
        return self.action == DISABLE

    def applies(self, value: int, domain: tuple[Interval, ...]) -> bool:
        return value in self.source_range and any(value in part for part in domain)


@dataclass(frozen=True)
class MigrationSpec:
    """A complete, declared migration input.

    ``source_spaces`` and ``target_spaces`` map a namespace name to its legal
    domain as a normalized union of closed integer intervals.  Every mapping
    rule's target space must be declared, because its allowed target domain is
    needed to decide whether the winning mapping is in range.
    """

    source_spaces: dict[str, tuple[Interval, ...]]
    target_spaces: dict[str, tuple[Interval, ...]]
    rules: tuple[Rule, ...]

    def __post_init__(self) -> None:
        for label, spaces in (
            ("source", self.source_spaces),
            ("target", self.target_spaces),
        ):
            if not isinstance(spaces, dict):
                raise TypeError(f"{label}_spaces must be a dict")
            for name, domain in spaces.items():
                if not isinstance(name, str) or not name:
                    raise ValueError(f"{label} space name must be a non-empty string")
                if not domain:
                    raise ValueError(f"{label} space {name!r} has an empty domain")
                if normalize(domain) != tuple(domain):
                    raise ValueError(f"{label} space {name!r} domain is not normalized")
        seen: set[str] = set()
        for rule in self.rules:
            if rule.id in seen:
                raise ValueError(f"duplicate rule id {rule.id!r}")
            seen.add(rule.id)
            if rule.source_space not in self.source_spaces:
                raise ValueError(
                    f"rule {rule.id!r} refers to undeclared source space "
                    f"{rule.source_space!r}"
                )
            if rule.action == MAPPING and rule.target_space not in self.target_spaces:
                raise ValueError(
                    f"rule {rule.id!r} refers to undeclared target space "
                    f"{rule.target_space!r}"
                )
        object.__setattr__(
            self,
            "source_spaces",
            {name: tuple(domain) for name, domain in self.source_spaces.items()},
        )
        object.__setattr__(
            self,
            "target_spaces",
            {name: tuple(domain) for name, domain in self.target_spaces.items()},
        )
        object.__setattr__(self, "rules", tuple(self.rules))

    def rules_for(self, source_space: str) -> tuple[Rule, ...]:
        return tuple(rule for rule in self.rules if rule.source_space == source_space)

    def legal_pieces(self, rule: Rule) -> tuple[Interval, ...]:
        return intersect_pieces(rule.source_range, self.source_spaces[rule.source_space])

    def source_domain_width(self, space: str) -> int:
        return total_width(self.source_spaces[space])

    def target_domain_width(self, space: str) -> int:
        return total_width(self.target_spaces[space])


def _parse_domain(raw: Any, what: str) -> tuple[Interval, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{what} must be a non-empty list of [lo, hi] pairs")
    intervals: list[Interval] = []
    for pair in raw:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError(f"{what} entries must be [lo, hi] pairs, got {pair!r}")
        lo = _require_int(pair[0], f"{what} lo")
        hi = _require_int(pair[1], f"{what} hi")
        intervals.append(Interval(lo, hi))
    return normalize(intervals)


def spec_from_dict(data: dict[str, Any]) -> MigrationSpec:
    if not isinstance(data, dict):
        raise TypeError("migration spec must be a JSON object")
    allowed = {"description", "source_spaces", "target_spaces", "rules"}
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"unknown top-level spec fields: {sorted(unknown)}")
    for required in ("source_spaces", "target_spaces", "rules"):
        if required not in data:
            raise ValueError(f"missing top-level field {required!r}")

    source_spaces = {
        name: _parse_domain(raw, f"source space {name!r}")
        for name, raw in _visit_spaces(data["source_spaces"], "source_spaces")
    }
    target_spaces = {
        name: _parse_domain(raw, f"target space {name!r}")
        for name, raw in _visit_spaces(data["target_spaces"], "target_spaces")
    }

    raw_rules = data["rules"]
    if not isinstance(raw_rules, list):
        raise TypeError("rules must be a list")
    rules: list[Rule] = []
    for index, raw in enumerate(raw_rules):
        rules.append(_parse_rule(raw, index))

    spec = MigrationSpec(source_spaces, target_spaces, tuple(rules))
    return spec


def _visit_spaces(raw: Any, label: str):
    if not isinstance(raw, dict):
        raise TypeError(f"{label} must be a JSON object")
    for name, domain in raw.items():
        if not isinstance(name, str) or not name:
            raise ValueError(f"{label} keys must be non-empty strings")
        yield name, domain


def _parse_rule(raw: Any, index: int) -> Rule:
    if not isinstance(raw, dict):
        raise TypeError(f"rule #{index} must be a JSON object")
    allowed = {
        "id",
        "source_space",
        "source_range",
        "priority",
        "action",
        "target_space",
        "direction",
        "offset",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"rule #{index} has unknown fields: {sorted(unknown)}")
    for required in ("id", "source_space", "source_range", "priority"):
        if required not in raw:
            raise ValueError(f"rule #{index} is missing {required!r}")

    rule_id = raw["id"]
    if not isinstance(rule_id, str) or not rule_id:
        raise ValueError(f"rule #{index}: id must be a non-empty string")
    source_space = raw["source_space"]
    if not isinstance(source_space, str) or not source_space:
        raise ValueError(f"rule {rule_id!r}: source_space must be a non-empty string")
    pair = raw["source_range"]
    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
        raise ValueError(f"rule {rule_id!r}: source_range must be [lo, hi]")
    source_range = Interval(
        _require_int(pair[0], f"rule {rule_id!r} source lo"),
        _require_int(pair[1], f"rule {rule_id!r} source hi"),
    )
    priority = _require_int(raw["priority"], f"rule {rule_id!r} priority")
    action = raw.get("action", MAPPING if "target_space" in raw else None)
    if action == DISABLE:
        if "target_space" in raw or "direction" in raw or "offset" in raw:
            raise ValueError(f"disable rule {rule_id!r} must not have target fields")
        return Rule(rule_id, source_space, source_range, priority, DISABLE)
    if action not in (None, MAPPING):
        raise ValueError(f"rule {rule_id!r}: unknown action {action!r}")
    if "target_space" not in raw or "direction" not in raw or "offset" not in raw:
        raise ValueError(
            f"rule {rule_id!r} must be either a complete mapping rule or "
            "an explicit disable rule"
        )
    target_space = raw["target_space"]
    if not isinstance(target_space, str) or not target_space:
        raise ValueError(f"rule {rule_id!r}: target_space must be a non-empty string")
    direction = _require_int(raw["direction"], f"rule {rule_id!r} direction")
    offset = _require_int(raw["offset"], f"rule {rule_id!r} offset")
    return Rule(
        rule_id,
        source_space,
        source_range,
        priority,
        MAPPING,
        target_space,
        Shift(direction, offset),
    )


def load_spec(path: str | Path) -> MigrationSpec:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    return spec_from_dict(data)
