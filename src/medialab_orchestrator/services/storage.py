"""Disk usage of the media mount."""

import shutil
from pathlib import Path

from medialab_orchestrator.schemas.jobs import DiskUsageView

BYTES_PER_GB: float = 1024**3
_PERCENT = 100
_STATUS_SUCCESS = "success"


def disk_usage(path: Path) -> DiskUsageView:
    """Measure ``path`` with ``shutil.disk_usage``; raises ``OSError`` when it
    is missing or unreadable."""
    usage = shutil.disk_usage(path)
    return DiskUsageView(
        status=_STATUS_SUCCESS,
        path=str(path),
        total_gb=round(usage.total / BYTES_PER_GB, 2),
        used_gb=round(usage.used / BYTES_PER_GB, 2),
        free_gb=round(usage.free / BYTES_PER_GB, 2),
        used_percent=round(usage.used / usage.total * _PERCENT, 2),
    )
