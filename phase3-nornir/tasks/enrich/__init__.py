"""tasks.enrich package - per-domain NetBox enrichment collectors.

Module layout:
  models.py       pydantic models for the enriched host data
  helpers.py      pure helpers (lo0 unit parser, description mapper)
  auth.py         derive_login_hash (env -> $6$ crypt)
  interfaces.py   fabric P2P, access, LAG members, ESI-LAG, IRB
  loopbacks.py    lo0.* unit collection
  bgp.py          underlay + overlay neighbor derivation
  tenants.py      tenants + MAC-VRF (VLAN list, extended-vni-list)
  main.py         enrich_from_netbox - the Nornir task entry point

Public API re-exported here so deploy.py and tests/ keep their
existing imports (`from tasks.enrich import enrich_from_netbox`,
`from tasks.enrich import derive_login_hash`, etc).
"""

from .auth import derive_login_hash
from .helpers import _lo0_unit_from_iface_name, _loopback_description
from .main import enrich_from_netbox

__all__ = [
    "enrich_from_netbox",
    "derive_login_hash",
    "_lo0_unit_from_iface_name",
    "_loopback_description",
]
