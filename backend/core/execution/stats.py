"""Execution support."""
from dataclasses import dataclass


def completed_units(task, updated):
    """Count confirmed repetition advances, never the row's advertised total."""
    if updated is not None and updated.total == task.total and updated.current > task.current:
        return max(1, min(updated.current, task.total) - task.current)
    # A verified disappearing row or immediately replaced batch proves this
    # attempt only, not all unseen repetitions in its new contract.
    return 1

@dataclass
class RunStats:
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    claims: int = 0
    cross_app: int = 0
    attempts: int = 0
    elapsed: int = 0
