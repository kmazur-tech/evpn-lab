"""pytest fixtures and path setup for phase4-batfish tests."""
import sys
from pathlib import Path

PHASE4 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PHASE4))
