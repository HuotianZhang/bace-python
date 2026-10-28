"""Exercise the bench instruments and write a staged check report.

    python -m bace.bench --help
"""
from .report import Report
from .session import RecordingResource

__all__ = ["Report", "RecordingResource"]
