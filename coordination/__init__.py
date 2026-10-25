"""Opt-in, provider-neutral Aster agent coordination.

The coordinator owns assignments and receipts. AI providers and machine tools
remain execution adapters; a delivered event is never a completed assignment.
"""

CONTRACT_VERSION = "aster.coordination.v1"
