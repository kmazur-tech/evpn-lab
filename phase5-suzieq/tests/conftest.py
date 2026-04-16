"""Pytest path setup. Adds tests/ to sys.path so test modules can
`from helpers import ...`. The actual helper code lives in
tests/helpers.py - keeping it out of conftest.py because pytest's
collection magic does not put conftest on the import path."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
