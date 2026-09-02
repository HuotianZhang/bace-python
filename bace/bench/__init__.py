"""Bench check: exercise the port against the real rig and write a report.

    python -m bace.bench --help
"""
from .report import Report
from .session import RecordingResource

__all__ = ["Report", "RecordingResource"]
