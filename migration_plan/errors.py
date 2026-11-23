"""Shared exception type for the rollout planner."""


class PlanError(ValueError):
    """Rejected input: malformed manifest, bad arguments, unreadable files.

    No plan is produced and nothing outside the temporary work area is
    touched.
    """
