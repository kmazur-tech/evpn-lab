#!/bin/bash
# DC1 EVPN-VXLAN Fabric - Smoke Tests
# Run after setup-hosts.sh completes.
#
# Tests cover: control plane, data plane, failover, operational checks.
# Exit code: 0 = all pass, 1 = failures detected.

set -u

LAB_NAME="${1:-dc1}"
GW_MAC="00:00:5e:00:01:01"
FAILURES=0
MAX_WAIT=120  # max seconds to wait for any single recovery

pass() { echo "  PASS: $1"; }
fail() { echo "  FAIL: $1"; FAILURES=$((FAILURES+1)); }

# Wait for a Junos device to have 0 BGP down peers.
# Polls every 10s, gives up after MAX_WAIT seconds.
wait_bgp_converged() {
  local ip=$1 name=$2
  local waited=0
  while [ $waited -lt $MAX_WAIT ]; do
    DOWN=$(junos_cmd $ip "show bgp summary" | grep "Down peers" | awk '{print $NF}')
    if [ "$DOWN" = "0" ]; then
      echo "  $name BGP converged (${waited}s)"
      return 0
    fi
    sleep 10
    waited=$((waited+10))
  done
  echo "  $name BGP NOT converged after ${MAX_WAIT}s ($DOWN down)"
  return 1
}

ping_test() {
  local src=$1 dst_ip=$2 label=$3
  local pid=$(docker inspect -f '{{.State.Pid}}' clab-${LAB_NAME}-${src})
  if nsenter -t $pid -n ping -c 3 -W 2 $dst_ip >/dev/null 2>&1; then
    pass "$label"
  else
    fail "$label"
  fi
}

junos_cmd() {
  local host=$1 cmd=$2
  sshpass -p 'TestLabPass1' ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 admin@$host "$cmd | no-more" 2>&1
}

# junos_json: run a Junos CLI command and return JSON-formatted output.
# Used by parsers that need stable structured fields (BGP neighbor state,
# BFD diag, interface counters). Plain `junos_cmd` is still used for the
# many checks that grep on stable semantic prefixes (e.g. "^2:" for EVPN
# Type-2 routes), where JSON-walking would be more code without being
# more robust.
junos_json() {
  local host=$1 cmd=$2
  sshpass -p 'TestLabPass1' ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 admin@$host "$cmd | display json | no-more" 2>&1
}

# wait_until: poll a shell command until it prints the expected value, or
# until timeout (default 120s) elapses. Replaces hardcoded `sleep N` waits
# in failover scenarios so the suite reacts to actual system state instead
# of guessing how long convergence takes.
#
# Usage: wait_until "<cmd>" <expected-value> [timeout-seconds] [poll-interval]
# Returns 0 on match, 1 on timeout. Echoes how long it actually waited.
wait_until() {
  local probe=$1 expected=$2 timeout=${3:-120} interval=${4:-3}
  local waited=0 actual
  while [ $waited -lt $timeout ]; do
    actual=$(eval "$probe" 2>/dev/null)
    if [ "$actual" = "$expected" ]; then
      echo $waited
      return 0
    fi
    sleep $interval
    waited=$((waited+interval))
  done
  echo $waited
  return 1
}

echo "============================================"
echo "  DC1 EVPN-VXLAN Smoke Tests"
echo "============================================"

# ---------------------------------------------------------------
echo ""
echo "=== Pre-flight: waiting for fabric convergence ==="
# ---------------------------------------------------------------

# Wait until all 4 devices have 0 BGP down peers before starting tests.
# On fresh deploy, BGP needs 30-60s after switches become healthy.
for name_ip in "dc1-spine1:172.16.18.160" "dc1-spine2:172.16.18.161" \
               "dc1-leaf1:172.16.18.162" "dc1-leaf2:172.16.18.163"; do
  name=${name_ip%%:*}; ip=${name_ip##*:}
  wait_bgp_converged $ip $name
done
echo ""

# ---------------------------------------------------------------
echo "=== 1. Control Plane ==="
# ---------------------------------------------------------------

# BGP underlay - all 4 devices should have 0 down peers
for name_ip in "dc1-spine1:172.16.18.160" "dc1-spine2:172.16.18.161" \
               "dc1-leaf1:172.16.18.162" "dc1-leaf2:172.16.18.163"; do
  name=${name_ip%%:*}; ip=${name_ip##*:}
  DOWN=$(junos_cmd $ip "show bgp summary" | grep "Down peers" | awk '{print $NF}')
  if [ "$DOWN" = "0" ]; then
    pass "$name BGP: 0 down peers"
  else
    fail "$name BGP: $DOWN down peers"
  fi
done

# EVPN routes on leaf1
EVPN_LINE=$(junos_cmd 172.16.18.162 "show route table EVPN-VXLAN.evpn.0" | grep "destinations")
EVPN_ROUTES=$(echo "$EVPN_LINE" | sed 's/.*: \([0-9]*\) destinations.*/\1/')
if [ -n "$EVPN_ROUTES" ] && [ "$EVPN_ROUTES" -gt 0 ] 2>/dev/null; then
  pass "leaf1 EVPN routes: $EVPN_ROUTES destinations"
else
  fail "leaf1 EVPN routes: none or parse error ($EVPN_LINE)"
fi

# VTEP tunnel
VTEP=$(junos_cmd 172.16.18.162 "show ethernet-switching vxlan-tunnel-end-point remote" | grep "RVTEP-IP" -A1 | tail -1 | awk '{print $1}')
if [ "$VTEP" = "10.1.0.4" ]; then
  pass "leaf1 VTEP tunnel to leaf2 (10.1.0.4)"
else
  fail "leaf1 VTEP tunnel: expected 10.1.0.4, got '$VTEP'"
fi

# Remote MAC learning
REMOTE_MACS=$(junos_cmd 172.16.18.162 "show ethernet-switching table" | grep -c "DR" 2>/dev/null | tail -1 || echo "0")
if [ "$REMOTE_MACS" -gt 0 ]; then
  pass "leaf1 remote MACs learned via EVPN: $REMOTE_MACS"
else
  fail "leaf1 no remote MACs (DR entries)"
fi

# LACP on leaf2
LACP_STATE=$(junos_cmd 172.16.18.163 "show lacp interfaces ae0" | grep "Collecting distributing" | wc -l)
if [ "$LACP_STATE" -gt 0 ]; then
  pass "leaf2 ae0 LACP: Collecting/Distributing"
else
  fail "leaf2 ae0 LACP: not distributing"
fi

# BFD sessions
BFD_UP=$(junos_cmd 172.16.18.162 "show bfd session" | grep -c "Up" 2>/dev/null | tail -1 || echo "0")
if [ "$BFD_UP" -gt 0 ]; then
  pass "leaf1 BFD sessions up: $BFD_UP"
else
  # BFD may not show on vjunos, skip gracefully
  echo "  SKIP: leaf1 BFD (may not be active on vjunos)"
fi

# LLDP neighbors
for name_ip in "dc1-spine1:172.16.18.160" "dc1-leaf1:172.16.18.162"; do
  name=${name_ip%%:*}; ip=${name_ip##*:}
  LLDP_COUNT=$(junos_cmd $ip "show lldp neighbors" | grep "ge-" | wc -l)
  if [ "$LLDP_COUNT" -ge 2 ]; then
    pass "$name LLDP neighbors: $LLDP_COUNT"
  else
    fail "$name LLDP neighbors: $LLDP_COUNT (expected >= 2)"
  fi
done

# ESI state
ESI_STATE=$(junos_cmd 172.16.18.162 "show evpn instance extensive" | grep "all-active" | wc -l)
if [ "$ESI_STATE" -gt 0 ]; then
  pass "leaf1 ESI all-active entries: $ESI_STATE"
else
  fail "leaf1 ESI: no all-active entries"
fi

# Core isolation
CORE_ISO=$(junos_cmd 172.16.18.162 "show configuration protocols network-isolation" | grep "core-isolation" | wc -l)
if [ "$CORE_ISO" -gt 0 ]; then
  pass "leaf1 core-isolation configured"
else
  fail "leaf1 core-isolation not configured"
fi

echo ""
# ---------------------------------------------------------------
echo "=== 2. Underlay Reachability ==="
# ---------------------------------------------------------------

# Every leaf should reach every other leaf/spine loopback
# Must specify source loopback - default source uses mgmt which can't route to underlay
for src_entry in "172.16.18.162:10.1.0.3:dc1-leaf1" "172.16.18.163:10.1.0.4:dc1-leaf2"; do
  src_ip=${src_entry%%:*}; rest=${src_entry#*:}; src_lo=${rest%%:*}; src_name=${rest#*:}
  for dst_lo in 10.1.0.1 10.1.0.2 10.1.0.3 10.1.0.4; do
    RESULT=$(junos_cmd $src_ip "ping $dst_lo source $src_lo count 1 rapid wait 2" | grep "received" | awk '{print $4}')
    if [ "$RESULT" = "1" ]; then
      pass "$src_name -> $dst_lo (loopback)"
    else
      fail "$src_name -> $dst_lo (loopback unreachable)"
    fi
  done
done

echo ""
# ---------------------------------------------------------------
echo "=== 3. Data Plane ==="
# ---------------------------------------------------------------

# L2 same VLAN cross-leaf (VXLAN)
ping_test dc1-host1 10.10.10.12 "L2: host1 (leaf1) -> host2 (leaf2) VLAN 10"

# L3 inter-VLAN same leaf
ping_test dc1-host1 10.10.20.13 "L3: host1 (VLAN10) -> host3 (VLAN20) inter-VLAN"

# L3 cross-VLAN cross-leaf
ping_test dc1-host2 10.10.20.14 "L3: host2 (leaf2 VLAN10) -> host4 (leaf1+2 VLAN20)"

# ESI-LAG same VLAN
ping_test dc1-host3 10.10.20.14 "ESI-LAG: host3 -> host4 (both dual-homed VLAN20)"

# Gateway reachability (static ARP)
ping_test dc1-host1 10.10.10.1 "GW: host1 -> 10.10.10.1 (virtual gateway)"
ping_test dc1-host3 10.10.20.1 "GW: host3 -> 10.10.20.1 (virtual gateway)"

echo ""
# ---------------------------------------------------------------
echo "=== 4. Failover: ESI-LAG ==="
# ---------------------------------------------------------------

# Pause the container to simulate hard failure (power loss / crash).
# LACP fast (1s PDU interval, 3s timeout) should detect the partner
# is gone and remove the slave from the bond on the host side.
# Baseline: count remote VTEP entries on leaf2 pointing at leaf1 (10.1.0.3).
# Used by the post-failure withdrawal check below.
VTEP_BEFORE=$(junos_cmd 172.16.18.163 "show ethernet-switching vxlan-tunnel-end-point remote" | grep -c "^ 10.1.0.3 ")

echo "  Pausing leaf1 container (hard failure simulation)..."
docker pause clab-${LAB_NAME}-dc1-leaf1

# Poll LACP aggregator port count instead of fixed sleep. With LACP fast
# (1s PDU, 3s timeout), the slave moves to a separate aggregator within
# ~3s of pause. Cap at 30s.
echo "  Waiting for LACP to detect leaf1 failure..."
WAITED=$(wait_until "docker exec clab-${LAB_NAME}-dc1-host3 grep 'Number of ports' /proc/net/bonding/bond0 | head -1 | awk '{print \$NF}'" "1" 30 1)
if [ $? = 0 ]; then
  pass "ESI-LAG: LACP detected leaf1 failure in ${WAITED}s (active aggregator: 1 port)"
else
  fail "ESI-LAG: aggregator still > 1 port after ${WAITED}s"
fi

ping_test dc1-host3 10.10.20.14 "ESI-LAG failover: host3 -> host4 (leaf1 crashed)"

# Post-failure withdrawal: poll until leaf2 drops the remote VTEP entry,
# instead of sleeping the full BGP hold timer (~90s). On a healthy fabric
# this typically happens in 60-100s (BGP hold + EVPN withdrawal flush).
# Cap at 150s to give margin for the full hold timer.
echo "  Polling for leaf2 VTEP withdrawal (BGP hold timer)..."
WAITED=$(wait_until "junos_cmd 172.16.18.163 'show ethernet-switching vxlan-tunnel-end-point remote' | grep -c '^ 10.1.0.3 '" "0" 150 5)
if [ $? = 0 ] && [ "$VTEP_BEFORE" = "1" ]; then
  pass "Post-failure withdrawal: leaf2 dropped remote VTEP 10.1.0.3 in ${WAITED}s"
else
  fail "Post-failure withdrawal: leaf2 still has VTEP 10.1.0.3 after ${WAITED}s (was=$VTEP_BEFORE)"
fi

echo "  Unpausing leaf1 container..."
docker unpause clab-${LAB_NAME}-dc1-leaf1

echo "  Waiting for leaf1 recovery..."
# Try SSH first, restart if unresponsive
waited=0
while [ $waited -lt $MAX_WAIT ]; do
  if sshpass -p 'TestLabPass1' ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 admin@172.16.18.162 "show system uptime" >/dev/null 2>&1; then
    break
  fi
  sleep 10
  waited=$((waited+10))
  if [ $waited -ge 60 ]; then
    echo "  WARN: leaf1 SSH not responding after ${waited}s, restarting..."
    docker restart clab-${LAB_NAME}-dc1-leaf1
    for i in $(seq 1 20); do
      sleep 15
      S=$(docker inspect clab-${LAB_NAME}-dc1-leaf1 --format '{{.State.Health.Status}}' 2>/dev/null)
      if [ "$S" = "healthy" ]; then break; fi
    done
    break
  fi
done

if wait_bgp_converged 172.16.18.162 "leaf1"; then
  pass "ESI-LAG restore: leaf1 recovered"
else
  fail "ESI-LAG restore: leaf1 BGP not converged"
fi

ping_test dc1-host3 10.10.20.14 "ESI-LAG restore: host3 -> host4 (both leaves back)"

# Reinstall: the same VTEP entry should reappear after BGP recovers.
VTEP_AFTER=$(junos_cmd 172.16.18.163 "show ethernet-switching vxlan-tunnel-end-point remote" | grep -c "^ 10.1.0.3 ")
if [ "$VTEP_AFTER" = "1" ]; then
  pass "Post-recovery reinstall: leaf2 re-learned remote VTEP 10.1.0.3"
else
  fail "Post-recovery reinstall: leaf2 VTEP for 10.1.0.3 = $VTEP_AFTER (expected 1)"
fi

echo ""
# ---------------------------------------------------------------
echo "=== 5. Failover: Core Isolation ==="
# ---------------------------------------------------------------

# Deactivate overlay BGP on leaf1 to simulate EVPN core loss.
# Core isolation should automatically bring down ALL ESI-LAG interfaces
# (ae0 and ae1) to prevent traffic blackholing through an isolated leaf.

# Baseline: leaf2 should currently see leaf1 as a remote VTEP.
ISO_VTEP_BEFORE=$(junos_cmd 172.16.18.163 "show ethernet-switching vxlan-tunnel-end-point remote" | grep -c "^ 10.1.0.3 ")

echo "  Deactivating overlay BGP on leaf1..."
junos_cmd 172.16.18.162 "configure; deactivate protocols bgp group OVERLAY; commit" >/dev/null 2>&1

# Poll for ae0 going down instead of fixed 15s sleep. Core-isolation
# detection runs every few seconds and brings the AEs down within ~5-10s.
echo "  Polling for core-isolation to bring ae0 down..."
WAITED=$(wait_until "junos_cmd 172.16.18.162 'show interfaces ae0 terse' | grep '^ae0 ' | awk '{print \$3}'" "down" 60 3)
AE0_LINK=$(junos_cmd 172.16.18.162 "show interfaces ae0 terse" | grep "^ae0 " | awk '{print $3}')
AE1_LINK=$(junos_cmd 172.16.18.162 "show interfaces ae1 terse" | grep "^ae1 " | awk '{print $3}')
if [ "$AE0_LINK" = "down" ] && [ "$AE1_LINK" = "down" ]; then
  pass "Core isolation: ae0 AND ae1 both brought down in ${WAITED}s"
else
  fail "Core isolation: ae0=$AE0_LINK ae1=$AE1_LINK after ${WAITED}s (expected both down)"
fi

ping_test dc1-host3 10.10.20.14 "Core isolation: host3 -> host4 (leaf1 isolated, via leaf2)"

# Post-isolation withdrawal: poll for VTEP drop instead of sleeping 80s.
echo "  Polling for VTEP withdrawal on leaf2 (BGP hold timer)..."
WAITED=$(wait_until "junos_cmd 172.16.18.163 'show ethernet-switching vxlan-tunnel-end-point remote' | grep -c '^ 10.1.0.3 '" "0" 150 5)
if [ $? = 0 ] && [ "$ISO_VTEP_BEFORE" = "1" ]; then
  pass "Core isolation withdrawal: leaf2 dropped remote VTEP 10.1.0.3 in ${WAITED}s"
else
  fail "Core isolation withdrawal: leaf2 still has VTEP 10.1.0.3 after ${WAITED}s"
fi

echo "  Restoring overlay BGP on leaf1..."
junos_cmd 172.16.18.162 "configure; activate protocols bgp group OVERLAY; commit" >/dev/null 2>&1

echo "  Waiting for BGP + LACP recovery (includes hold-time up 60s)..."
wait_bgp_converged 172.16.18.162 "leaf1"

# Core isolation has hold-time up (60s) - AEs stay down after BGP recovers
# to prevent flapping. Wait for both ae0 AND ae1 to come back.
waited=0
while [ $waited -lt $MAX_WAIT ]; do
  AE0_LINK=$(junos_cmd 172.16.18.162 "show interfaces ae0 terse" | grep "ae0 " | awk '{print $3}')
  AE1_LINK=$(junos_cmd 172.16.18.162 "show interfaces ae1 terse" | grep "ae1 " | awk '{print $3}')
  if [ "$AE0_LINK" = "up" ] && [ "$AE1_LINK" = "up" ]; then break; fi
  sleep 10
  waited=$((waited+10))
done

if [ "$AE0_LINK" = "up" ] && [ "$AE1_LINK" = "up" ]; then
  pass "Core isolation restore: ae0 AND ae1 both back up (${waited}s after BGP)"
else
  fail "Core isolation restore: ae0=$AE0_LINK ae1=$AE1_LINK after ${MAX_WAIT}s"
fi

# Post-recovery: VTEP entry must reappear and DF election must converge to
# the same answer on both leaves (no DF drift after a control-plane bounce).
ISO_VTEP_AFTER=$(junos_cmd 172.16.18.163 "show ethernet-switching vxlan-tunnel-end-point remote" | grep -c "^ 10.1.0.3 ")
if [ "$ISO_VTEP_AFTER" = "1" ]; then
  pass "Core isolation recovery: leaf2 re-learned remote VTEP 10.1.0.3"
else
  fail "Core isolation recovery: leaf2 VTEP for 10.1.0.3 = $ISO_VTEP_AFTER (expected 1)"
fi

iso_df_l1=$(junos_cmd 172.16.18.162 "show evpn instance designated-forwarder")
iso_df_l2=$(junos_cmd 172.16.18.163 "show evpn instance designated-forwarder")
iso_df_drift=0
while read -r esi; do
  [ -z "$esi" ] && continue
  d1=$(echo "$iso_df_l1" | awk -v e="$esi" '$0~e{getline; print $NF}')
  d2=$(echo "$iso_df_l2" | awk -v e="$esi" '$0~e{getline; print $NF}')
  [ -z "$d1" ] || [ "$d1" != "$d2" ] && iso_df_drift=$((iso_df_drift+1))
done <<< "$(echo "$iso_df_l1" | awk '/ESI: 01:/ {print $2}')"
if [ "$iso_df_drift" = "0" ]; then
  pass "Core isolation recovery: DF election still consistent across leaves"
else
  fail "Core isolation recovery: $iso_df_drift ESIs have DF drift after recovery"
fi

ping_test dc1-host3 10.10.20.14 "Core isolation restore: host3 -> host4 (both leaves)"

echo ""
# ---------------------------------------------------------------
echo "=== 6. Failover: Spine ==="
# ---------------------------------------------------------------

echo "  Disabling spine1 underlay interfaces..."
junos_cmd 172.16.18.160 "configure; set interfaces ge-0/0/0 disable; set interfaces ge-0/0/1 disable; commit" >/dev/null 2>&1

# Poll for leaf1 to lose its BGP session to spine1 (10.1.0.1) instead of
# fixed sleep. BFD detects failure within multiplier*interval = 3s.
echo "  Polling for leaf1 to mark spine1 BGP session down..."
WAITED=$(wait_until "junos_cmd 172.16.18.162 'show bgp summary' | awk '/^10.1.0.1/ {print \$NF}' | grep -qE '^[0-9]+\$' && echo up || echo down" "down" 30 2)
echo "  spine1 session marked down on leaf1 in ${WAITED}s"

ping_test dc1-host1 10.10.10.12 "Spine failover: host1 -> host2 (spine1 down, via spine2)"
ping_test dc1-host1 10.10.20.13 "Spine failover: host1 -> host3 L3 (spine1 down)"

echo "  Restoring spine1..."
junos_cmd 172.16.18.160 "configure; delete interfaces ge-0/0/0 disable; delete interfaces ge-0/0/1 disable; commit" >/dev/null 2>&1

echo "  Waiting for BGP reconvergence..."
if wait_bgp_converged 172.16.18.160 "spine1"; then
  pass "Spine restore: spine1 BGP re-established"
else
  fail "Spine restore: spine1 BGP not converged"
fi

ping_test dc1-host1 10.10.10.12 "Spine restore: host1 -> host2 (both spines)"

echo ""
# ---------------------------------------------------------------
echo "=== 7. Expected Failures ==="
# ---------------------------------------------------------------

# Single-homed host loses connectivity when its leaf port is disabled
echo "  Disabling leaf1 ge-0/0/2 (host1 access port)..."
junos_cmd 172.16.18.162 "configure; set interfaces ge-0/0/2 disable; commit" >/dev/null 2>&1
sleep 3

HOST1_PID=$(docker inspect -f '{{.State.Pid}}' clab-${LAB_NAME}-dc1-host1)
if nsenter -t $HOST1_PID -n ping -c 2 -W 2 10.10.10.12 >/dev/null 2>&1; then
  fail "Single-homed isolation: host1 should NOT reach host2 (leaf1 port down)"
else
  pass "Single-homed isolation: host1 correctly lost connectivity"
fi

echo "  Restoring leaf1 ge-0/0/2..."
junos_cmd 172.16.18.162 "configure; delete interfaces ge-0/0/2 disable; commit" >/dev/null 2>&1
sleep 5

echo ""
# ---------------------------------------------------------------
echo "=== 8. EVPN Deep Validation ==="
# ---------------------------------------------------------------
#
# Parsing strategy (deliberate, not accidental):
#  - JSON + jq is used for fields where the screen-text format is fragile:
#    BGP neighbor state and per-RIB counters, BFD per-session diag, and
#    interface error/drop counters. These all use awkward positional
#    parsers in the original implementation; the Junos JSON schema field
#    names (peer-state, local-diagnostic, input-errors, input-drops) are
#    stable across releases and survive output format changes.
#  - Plain grep is kept for asserts that match on stable semantic prefixes
#    (e.g. "^2:" for EVPN Type-2 routes, "ESI: 01:" for LACP-derived ESIs,
#    "0% packet loss" for ping). The JSON equivalents for these end up as
#    deeply-nested jq expressions that are LESS readable without being
#    more robust - the prefix strings are themselves part of the EVPN
#    NLRI format, not the CLI presentation layer.
#

# Warmup: ARP entries age out after port flaps in earlier sections, which
# in turn pulls Type-5 host /32 routes out of TENANT-1.inet.0 because the
# leaf only advertises them while the IRB ARP entry exists. Pre-ping every
# host so ARP is fresh before we make object-based assertions.
for h in dc1-host1 dc1-host2 dc1-host3 dc1-host4; do
  pid=$(docker inspect -f '{{.State.Pid}}' clab-${LAB_NAME}-${h} 2>/dev/null)
  [ -n "$pid" ] && nsenter -t $pid -n ping -c 1 -W 2 10.10.10.1 >/dev/null 2>&1
  [ -n "$pid" ] && nsenter -t $pid -n ping -c 1 -W 2 10.10.20.1 >/dev/null 2>&1
done
sleep 3  # let EVPN propagate the refreshed Type-5 advertisements

# Expected objects in this 2-leaf, 2-VNI lab. Drives object-based asserts.
LEAF1_LO=10.1.0.3
LEAF2_LO=10.1.0.4
EXPECT_VNIS="10010 10020"
# /32 host routes that should appear via EVPN in TENANT-1.inet.0 on each leaf
# (every host's IP, EXCEPT the leaf's own VLAN10 single-homed host, which is
# learned locally via ARP, not EVPN). ESI-LAG hosts in VLAN20 appear via EVPN
# on both leaves because both leaves snoop them locally too.
# In symmetric ERB with `advertise direct-nexthop`, leaves export their
# directly-connected /24 subnets via Type-5, NOT per-host /32s. Per-host
# /32s in TENANT-1.inet.0 are locally-snooped EVPN entries (the leaf's
# own IRB ARP feeds the EVPN database which then installs the /32 via
# irb.X). So we cannot assert "remote /32 present via EVPN" - that route
# does not exist in this design. Instead we assert that the local ARP-to-
# Type-5 import path works: pick an ESI-LAG host, which is locally
# snooped on both leaves through different ports. Its /32 should appear
# as `*[EVPN` in TENANT-1.inet.0 on BOTH leaves.
T5_VRF_EXPECT="10.10.20.13"
# Host MACs (set in setup-hosts.sh; query at runtime since they are dynamic)

# Helper: extract a host's MAC by its IP from leaf's EVPN database
mac_for_ip() {
  local leaf=$1 ip=$2
  junos_cmd $leaf "show evpn database" | awk -v ip="$ip" '$NF==ip {print $3; exit}'
}

# Per-leaf validation function. Mirrors the same checks on both leaves so
# a one-sided regression cannot pass the suite.
#
# Args: $1 leaf mgmt IP   $2 leaf name   $3 leaf's own loopback   $4 remote loopback
validate_leaf() {
  local ip=$1 name=$2 own_lo=$3 remote_lo=$4
  local t5_host=$T5_VRF_EXPECT

  # ----- ECMP next-hop count to remote loopback -----
  # Without forwarding-table export LOAD-BALANCE the PFE installs only one
  # next-hop even when BGP shows multipath. Expect 2 (one per spine).
  local ecmp
  ecmp=$(junos_cmd $ip "show route forwarding-table destination ${remote_lo}/32 table default" | grep -cE "ucst .* ge-")
  if [ "$ecmp" = "2" ]; then
    pass "$name ECMP: ${remote_lo}/32 installed via 2 next-hops (both spines)"
  else
    fail "$name ECMP: ${remote_lo}/32 has $ecmp next-hops in PFE (expected 2)"
  fi

  # ----- EVPN route-type breakdown, per VNI (object-based, not threshold) -----
  # For each L2VNI we expect at least one Type-2 MAC/IP and one Type-3 IMET
  # entry sourced from the remote leaf's RD. Catches a single-VNI outage that
  # an aggregate count would miss.
  local vni t2 t3
  for vni in $EXPECT_VNIS; do
    t2=$(junos_cmd $ip "show route table bgp.evpn.0 match-prefix 2:*::${vni}::*" | grep -c "^2:")
    t3=$(junos_cmd $ip "show route table bgp.evpn.0 match-prefix 3:*::${vni}::*" | grep -c "^3:")
    if [ "$t2" -ge 1 ]; then
      pass "$name EVPN Type-2 (VNI $vni): $t2 MAC/IP routes"
    else
      fail "$name EVPN Type-2 (VNI $vni): 0 MAC/IP routes"
    fi
    if [ "$t3" -ge 1 ]; then
      pass "$name EVPN Type-3 (VNI $vni): $t3 IMET routes"
    else
      fail "$name EVPN Type-3 (VNI $vni): 0 IMET routes"
    fi
  done

  # Type-5 (IP-prefix) - any count > 0 means tenant L3VNI advertisement works
  local t5
  t5=$(junos_cmd $ip "show route table bgp.evpn.0 match-prefix 5:*" | grep -c "^5:")
  if [ "$t5" -ge 1 ]; then
    pass "$name EVPN Type-5 (IP-prefix): $t5 routes"
  else
    fail "$name EVPN Type-5 (IP-prefix): 0 routes"
  fi

  # ----- Specific TENANT-1.inet.0 /32 imported by EVPN -----
  # Object-based: the expected ESI-LAG host /32 must be installed via
  # protocol EVPN (from local ARP snoop into the EVPN database, then back
  # into the VRF). Catches a broken RT or wrong VNI binding even if the
  # destination count is non-zero.
  if junos_cmd $ip "show route table TENANT-1.inet.0 ${t5_host}/32 protocol evpn" | grep -q "\*\[EVPN"; then
    pass "$name TENANT-1.inet.0: ${t5_host}/32 present via EVPN"
  else
    fail "$name TENANT-1.inet.0: ${t5_host}/32 NOT present via EVPN"
  fi

  # ----- Specific host MAC+IP entries in the EVPN database -----
  # The 4 lab hosts must all be learned in the EVPN database with a non-empty
  # IP column. The leaf's local hosts come from ARP snooping (regression test
  # for no-arp-suppression), the remote ones come from BGP Type-2.
  local expected_ips="10.10.10.11 10.10.10.12 10.10.20.13 10.10.20.14"
  local missing="" found=0
  for h in $expected_ips; do
    if junos_cmd $ip "show evpn database" | awk '{print $NF}' | grep -q "^${h}$"; then
      found=$((found+1))
    else
      missing="$missing $h"
    fi
  done
  if [ -z "$missing" ]; then
    pass "$name EVPN database: all 4 host IPs present ($expected_ips)"
  else
    fail "$name EVPN database: missing host IPs:$missing"
  fi

  # ----- Per-peer overlay BGP: Established AND receiving EVPN NLRI -----
  # Parsed from `| display json` + jq instead of grep on screen text. The
  # field names "peer-state" and "received-prefix-count" under bgp-rib are
  # part of the Junos schema and stable across releases.
  local peer state recvd
  for peer in 10.1.0.1 10.1.0.2; do
    [ "$peer" = "$own_lo" ] && continue
    local nbr_json
    nbr_json=$(junos_json $ip "show bgp neighbor $peer")
    state=$(echo "$nbr_json" | jq -r '.["bgp-information"][0]["bgp-peer"][0]["peer-state"][0].data // "unknown"')
    recvd=$(echo "$nbr_json" | jq -r '[.["bgp-information"][0]["bgp-peer"][0]["bgp-rib"][]?["received-prefix-count"][0].data | tonumber] | add // 0')
    if [ "$state" = "Established" ] && [ "$recvd" -gt 0 ] 2>/dev/null; then
      pass "$name overlay BGP -> $peer: Established, $recvd received EVPN prefixes"
    else
      fail "$name overlay BGP -> $peer: state=$state received=$recvd"
    fi
  done

  # ----- Jumbo MTU end-to-end across the underlay -----
  if junos_cmd $ip "ping ${remote_lo} source ${own_lo} size 8972 do-not-fragment count 3 rapid" | grep -q "0% packet loss"; then
    pass "$name MTU: jumbo (size 8972 DF) -> ${remote_lo}"
  else
    fail "$name MTU: jumbo ping -> ${remote_lo} failed (underlay MTU too small?)"
  fi

  # ----- Duplicate-MAC detection clean -----
  local dup
  dup=$(junos_cmd $ip "show evpn database state duplicate" | grep -cE "^[[:space:]]*[0-9]+[[:space:]]")
  if [ "$dup" = "0" ]; then
    pass "$name EVPN duplicate-MAC: 0 duplicate entries"
  else
    fail "$name EVPN duplicate-MAC: $dup entries (loop or mis-cabling?)"
  fi

  # ----- BFD session health -----
  # Parsed from JSON. Asserts every session is Up AND every session has
  # local-diagnostic == "None". One sed/awk-free assertion replaces the
  # two grep counters from the original implementation.
  local bfd_json bfd_total bfd_clean
  bfd_json=$(junos_json $ip "show bfd session extensive")
  bfd_total=$(echo "$bfd_json" | jq '[.["bfd-session-information"][0]["bfd-session"][]? | select(.["session-state"][0].data=="Up" and .["local-diagnostic"][0].data=="None")] | length')
  bfd_all=$(echo "$bfd_json" | jq '[.["bfd-session-information"][0]["bfd-session"][]?] | length')
  if [ "$bfd_total" -ge 2 ] && [ "$bfd_total" = "$bfd_all" ]; then
    pass "$name BFD: $bfd_total sessions Up + diag=None (out of $bfd_all)"
  else
    fail "$name BFD: $bfd_total clean / $bfd_all total (expected matching, >= 2)"
  fi

  # ----- Underlay interface error/drop counters -----
  # JSON-parsed. Replaces a fragile sed regex against a single-line
  # "Input errors:" block. The Junos schema names input-errors and
  # input-drops directly under input-error-list[0].
  local iface_bad=0 errs drops
  for iface in ge-0/0/0 ge-0/0/1; do
    local iface_json
    iface_json=$(junos_json $ip "show interfaces $iface extensive")
    errs=$(echo "$iface_json" | jq -r '.["interface-information"][0]["physical-interface"][0]["input-error-list"][0]["input-errors"][0].data // "0"')
    drops=$(echo "$iface_json" | jq -r '.["interface-information"][0]["physical-interface"][0]["input-error-list"][0]["input-drops"][0].data // "0"')
    if [ "$errs" != "0" ] || [ "$drops" != "0" ]; then
      iface_bad=$((iface_bad+1))
      echo "    $name $iface: errors=$errs drops=$drops"
    fi
  done
  if [ "$iface_bad" = "0" ]; then
    pass "$name underlay counters: fabric interfaces clean (0 errors, 0 drops)"
  else
    fail "$name underlay counters: $iface_bad fabric interfaces have errors/drops"
  fi
}

# Run the per-leaf validation against both leaves.
validate_leaf 172.16.18.162 leaf1 $LEAF1_LO $LEAF2_LO
echo ""
validate_leaf 172.16.18.163 leaf2 $LEAF2_LO $LEAF1_LO
echo ""

# ---------------------------------------------------------------
# Cross-leaf checks (run once, compare both leaves' state)
# ---------------------------------------------------------------

# DF election + ESI consistency: every LACP-derived ESI (01:00:...) must
# elect the same DF on both leaves. Mismatch = split-brain BUM forwarder.
df_l1=$(junos_cmd 172.16.18.162 "show evpn instance designated-forwarder")
df_l2=$(junos_cmd 172.16.18.163 "show evpn instance designated-forwarder")
esi_l1=$(echo "$df_l1" | grep -c "ESI: 01:")
esi_l2=$(echo "$df_l2" | grep -c "ESI: 01:")
if [ "$esi_l1" -ge 2 ] && [ "$esi_l1" = "$esi_l2" ]; then
  pass "ESI consistency: both leaves see $esi_l1 LACP-derived ESIs"
else
  fail "ESI consistency: leaf1=$esi_l1 leaf2=$esi_l2 (expected matching, >= 2)"
fi

df_mismatch=0; df_checked=0
while read -r esi; do
  [ -z "$esi" ] && continue
  df1=$(echo "$df_l1" | awk -v e="$esi" '$0~e{getline; print $NF}')
  df2=$(echo "$df_l2" | awk -v e="$esi" '$0~e{getline; print $NF}')
  df_checked=$((df_checked+1))
  if [ -z "$df1" ] || [ "$df1" != "$df2" ]; then
    df_mismatch=$((df_mismatch+1))
  fi
done <<< "$(echo "$df_l1" | awk '/ESI: 01:/ {print $2}')"
if [ "$df_checked" -gt 0 ] && [ "$df_mismatch" = "0" ]; then
  pass "DF election: $df_checked ESIs, both leaves agree on DF"
else
  fail "DF election: $df_mismatch/$df_checked ESIs have mismatched DF between leaves"
fi

echo ""
# ---------------------------------------------------------------
echo "============================================"
if [ $FAILURES -eq 0 ]; then
  echo "  ALL TESTS PASSED"
else
  echo "  $FAILURES TEST(S) FAILED"
fi
echo "============================================"

exit $FAILURES
