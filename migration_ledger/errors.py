"""Shared exception type for the ledger protocol.

Kept in its own module so the storage, refresh and delivery layers can raise the
same error without importing each other.
"""

from __future__ import annotations


class LedgerError(ValueError):
    """Any rejected input or protocol violation.

    The ledger and every already published delivery stay unchanged.
    """
