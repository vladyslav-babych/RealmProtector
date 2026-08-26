"""Realm Protector Discord bot application package."""

from __future__ import annotations

import sys
from collections.abc import Sequence

MINIMUM_PYTHON = (3, 12)
MAXIMUM_PYTHON = (3, 15)
__version__ = "2.0.2"


def validate_python_version(version_info: Sequence[int]) -> None:
    """Stop before third-party imports when the interpreter is unsupported."""

    version = tuple(version_info[:2])
    if MINIMUM_PYTHON <= version < MAXIMUM_PYTHON:
        return
    detected = ".".join(str(part) for part in version_info[:3])
    raise SystemExit(
        "Realm Protector requires Python 3.12, 3.13, or 3.14; "
        f"this virtual environment uses Python {detected}. "
        "Recreate .venv with Python 3.14 and reinstall requirements.lock."
    )


validate_python_version((sys.version_info.major, sys.version_info.minor, sys.version_info.micro))


__all__ = ["MAXIMUM_PYTHON", "MINIMUM_PYTHON", "__version__", "validate_python_version"]
