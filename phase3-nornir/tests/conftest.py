"""pytest fixtures and path setup for phase3-nornir tests."""
import sys
from pathlib import Path

# Make `import deploy` and `import tasks.X` work when pytest is run
# from anywhere. phase3-nornir/ is the package root.
PHASE3 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PHASE3))
