"""tasks.enrich package - per-domain NetBox enrichment collectors.

Module layout:
  models.py       pydantic models for the enriched host data
                  (HostData + 9 component types, all extra="forbid")
  helpers.py      pure helpers (lo0 unit parser, description mapper)
  auth.py         derive_login_hash - reads JUNOS_LOGIN_PASSWORD +
                  JUNOS_LOGIN_SALT from env and runs them through
                  passlib.hash.sha512_crypt (builtin backend,
                  rounds=5000) to produce a deterministic
                  $6$<salt>$<86char> hash. Hard-fails if either env
                  var is missing - never returns a placeholder.
  interfaces.py   fabric P2P (role-based peer detection), access,
                  LAG members, ESI-LAG parents, IRB
  loopbacks.py    lo0.* unit collection
  bgp.py          underlay + overlay neighbor derivation
  tenants.py      tenants + MAC-VRF (VLAN list, extended-vni-list).
                  overlay_asn is passed in by main.py from
                  vars/junos_defaults.yml - the single source of truth.
  main.py         enrich_from_netbox - the Nornir task entry point.
                  Loads vars/junos_defaults.yml once, calls each
                  collector, builds a HostData, validates ONCE with
                  pydantic, then writes plain dicts to task.host.

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
