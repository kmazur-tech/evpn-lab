"""Pytest path setup for phase1-netbox.

populate.py is the only Python file in this phase. It imports
pynetbox unconditionally at module top, which is fine in production
but means tests need pynetbox installed even when only testing the
pure helpers (slugify / ensure_slug / load_config). Tests load the
script via importlib so the operator-friendly bare filename stays.
"""
import importlib.util
import sys
from pathlib import Path

PHASE1 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PHASE1))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _load_populate():
    spec = importlib.util.spec_from_file_location(
        "populate", PHASE1 / "populate.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


populate = _load_populate()
