"""pytest fixtures and path setup for phase5-suzieq tests.

gen-inventory.py is a hyphenated script (matches the deploy CLI
convention from Phase 4 validate.py), not a regular module name, so
we cannot `import gen-inventory`. Tests load it via importlib so the
script's filename stays operator-friendly.
"""
import importlib.util
import sys
from pathlib import Path

PHASE5 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PHASE5))


def _load_gen_inventory():
    spec = importlib.util.spec_from_file_location(
        "gen_inventory", PHASE5 / "gen-inventory.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Single shared module instance - imported once at collection time.
gen_inventory = _load_gen_inventory()


def fake_nb_device(name, model, oob_addr, site_slug):
    """Build a NetBox device dict shaped like /api/dcim/devices/.

    Only the fields gen-inventory.generate() reads. Real NetBox
    responses have ~80 fields; the script ignores all but four.
    """
    return {
        "name": name,
        "device_type": {"model": model} if model else None,
        "oob_ip": {"address": f"{oob_addr}/24"} if oob_addr else None,
        "site": {"slug": site_slug} if site_slug else None,
    }
