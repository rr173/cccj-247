"""Independent brute-force reference used only by tests.

This deliberately re-implements the priority semantics by enumerating every
legal code, so the symbolic catalogue can be checked exhaustively on small
domains.  It is *not* part of the deliverable catalogue (it cannot scale), but
it is the oracle the tests trust.
"""

from __future__ import annotations

from migration_catalog.model import DISABLE, MAPPING, MigrationSpec


def _contains(domain, code):
    return any(code in part for part in domain)


def classify_source(spec: MigrationSpec, space: str, code: int):
    """Return ``(status, winning_rule_ids, target_space, target_value)``."""

    domain = spec.source_spaces[space]
    if not _contains(domain, code):
        return "out_of_domain", (), None, None
    applicable = [
        rule
        for rule in spec.rules
        if rule.source_space == space
        and code in rule.source_range
        and _contains(domain, code)
    ]
    if not applicable:
        return "missing", (), None, None
    top = max(rule.priority for rule in applicable)
    winners = tuple(
        sorted((rule for rule in applicable if rule.priority == top), key=lambda r: r.id)
    )
    ids = tuple(rule.id for rule in winners)
    if all(rule.action == DISABLE for rule in winners):
        return "disabled", ids, None, None
    if any(rule.action == DISABLE for rule in winners):
        return "conflict", ids, None, None
    outcomes = {
        (rule.target_space, rule.shift.apply(code))
        for rule in winners
        if rule.action == MAPPING
    }
    if len(outcomes) != 1:
        return "conflict", ids, None, None
    target_space, value = outcomes.pop()
    in_domain = _contains(spec.target_spaces[target_space], value)
    return ("mapped" if in_domain else "out_of_range"), ids, target_space, value


def all_legal_codes(spec: MigrationSpec, space: str):
    for part in spec.source_spaces[space]:
        yield from range(part.lo, part.hi + 1)


def all_target_codes(spec: MigrationSpec, space: str):
    for part in spec.target_spaces[space]:
        yield from range(part.lo, part.hi + 1)


def preimages(spec: MigrationSpec, target_space: str, code: int):
    """All legal source positions whose winning mapping is ``code``."""

    result = []
    for source_space in spec.source_spaces:
        for old in all_legal_codes(spec, source_space):
            status, ids, tspace, value = classify_source(spec, source_space, old)
            if status == "mapped" and tspace == target_space and value == code:
                result.append((source_space, old, ids))
    return result


def classify_target(spec: MigrationSpec, space: str, code: int):
    if not _contains(spec.target_spaces[space], code):
        return "out_of_domain", []
    found = preimages(spec, space, code)
    if not found:
        return "unused", []
    if len(found) == 1:
        return "unique", found
    return "ambiguous", found


def brute_force_catalog(spec: MigrationSpec):
    """Full dictionaries for small specs, keyed by code."""

    source = {}
    for space in spec.source_spaces:
        for code in all_legal_codes(spec, space):
            source[(space, code)] = classify_source(spec, space, code)
    target = {}
    for space in spec.target_spaces:
        for code in all_target_codes(spec, space):
            target[(space, code)] = classify_target(spec, space, code)
    return source, target
