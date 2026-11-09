"""Import third-party packages that Hermes's launcher keeps off ``sys.path``.

Hermes starts the CLI and the gateway in a mode where the usual
``site-packages`` entries are not on ``sys.path`` (its managed environment is
overlaid by the launcher), yet the packages are installed. ``import yaml`` or
``import edge_tts`` then fails inside an otherwise healthy plugin, and the
failure looks like a missing install.

``ensure_import`` retries the import after appending any candidate
``site-packages`` directory that actually contains the package. Each candidate
is only added when the package is present there, so a genuinely missing
dependency still raises ImportError with its original message.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import Any

_CACHE: dict[str, Any] = {}


def candidate_site_dirs() -> list[Path]:
    """Every ``site-packages`` this interpreter could plausibly use."""
    version = f"Python{sys.version_info.major}{sys.version_info.minor}"
    bases = [Path(sys.executable).parent, Path(sys.base_prefix), Path(sys.prefix)]
    candidates: list[Path] = []
    for base in bases:
        candidates.append(base / "Lib" / "site-packages")
        candidates.append(base / "lib" / version / "site-packages")
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "Python" / version / "site-packages")
        candidates.append(
            Path(appdata) / "Python" / f"Python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
        )
    unique: list[Path] = []
    for path in candidates:
        if path not in unique:
            unique.append(path)
    return unique


def ensure_import(name: str) -> Any:
    """``importlib.import_module(name)``, repairing a stripped ``sys.path`` first."""
    if name in _CACHE:
        return _CACHE[name]
    try:
        module = importlib.import_module(name)
    except ImportError as exc:
        last: BaseException = exc
        for directory in candidate_site_dirs():
            if not (directory / name).exists():
                continue
            entry = str(directory)
            if entry not in sys.path:
                sys.path.append(entry)
            try:
                module = importlib.import_module(name)
            except ImportError as retry_exc:
                last = retry_exc
                continue
            _CACHE[name] = module
            return module
        raise last
    _CACHE[name] = module
    return module
