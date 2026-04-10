"""Pydantic models for the enriched host data shapes.

Every collector in tasks/enrich/ returns a list (or single instance)
of these models. The orchestrator validates them, then converts to
plain dicts via .model_dump() before assigning to task.host[...] so
Jinja templates keep using bracket access (no template change needed).

Benefits:
- The contract between enrich and templates is explicit and typed.
- NetBox schema drift (missing field, wrong type) is caught at enrich
  time with a clear ValidationError, not as a Jinja UndefinedError
  during render or worse - as a malformed config on a device.
- Tests can construct fixture host data by instantiating these
  classes instead of building loose dicts.
- Future Phase 7 multi-tenant work has a single place to add tenant
  fields without grepping through enrich code.
"""

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    """Base for all enrich models. Forbids extra fields - typo in
    a collector raises immediately rather than silently dropping data
    on its way to a template that won't find it."""
    model_config = ConfigDict(extra="forbid", frozen=False)


class LoopbackUnit(_Strict):
    unit: int
    address: str
    description: str


class FabricLink(_Strict):
    name: str
    description: str
    address: str
    peer_ip: str
    peer_asn: int
    peer_name: str


class AccessPort(_Strict):
    name: str
    vlan_name: str


class LagMember(_Strict):
    name: str
    lag_name: str


class Lag(_Strict):
    name: str
    ae_index: int
    vlan_name: Optional[str] = None
    admin_key: int
    system_id: str


class Irb(_Strict):
    unit: int
    leaf_ip: Optional[str] = None
    gateway_ip: Optional[str] = None
    anycast_mac: Optional[str] = None


class Tenant(_Strict):
    name: str
    tenant_id: int
    l3vni: int
    anycast_mac: Optional[str] = None
    rt: str
    t5_prefixes: List[str] = Field(default_factory=list)


class VlanInMacVrf(_Strict):
    name: str
    vid: int
    vni: Optional[int] = None
    l3_interface: str


class BgpUnderlayNeighbor(_Strict):
    ip: str
    asn: int


class HostData(_Strict):
    """Top-level enriched intent for one device. The orchestrator
    builds this, validates it once, then writes each field to
    task.host as a plain dict/list (templates use bracket access)."""
    role_slug: Optional[str] = None
    router_id: Optional[str] = None
    asn: Optional[int] = None

    fabric_links: List[FabricLink] = Field(default_factory=list)
    access_ports: List[AccessPort] = Field(default_factory=list)
    lag_members: List[LagMember] = Field(default_factory=list)
    lags: List[Lag] = Field(default_factory=list)
    irbs: List[Irb] = Field(default_factory=list)

    loopbacks: List[LoopbackUnit] = Field(default_factory=list)
    tenants: List[Tenant] = Field(default_factory=list)

    mgmt_gw_v4: Optional[str] = None
    mgmt_gw_v6: Optional[str] = None

    mac_vrf_interfaces: List[str] = Field(default_factory=list)
    vlans_in_mac_vrf: List[VlanInMacVrf] = Field(default_factory=list)
    extended_vni_list: List[int] = Field(default_factory=list)

    underlay_neighbors: List[BgpUnderlayNeighbor] = Field(default_factory=list)
    overlay_neighbors: List[str] = Field(default_factory=list)
