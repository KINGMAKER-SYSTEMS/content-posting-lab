"""Compatibility phase A: read both layouts before new private-root production.

Phase B changes this internal constant only after phase A's deployed health is
qualified. A rollback to phase A retains private layout for existing B jobs.
Pre-phase-A images are not supported rollback targets for active B imports.
"""
PRIVATE_ATTEMPT_ROOTS = True


def attempt_root(parent, attempt_id, *, previous_root=None):
    private = PRIVATE_ATTEMPT_ROOTS or (previous_root is not None and previous_root != parent)
    return parent / attempt_id if private else parent
