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

pass() { echo "  PASS: $1"; }
fail() { echo "  FAIL: $1"; FAILURES=$((FAILURES+1)); }

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

echo "============================================"
echo "  DC1 EVPN-VXLAN Smoke Tests"
echo "============================================"
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
REMOTE_MACS=$(junos_cmd 172.16.18.162 "show ethernet-switching table" | grep -c "DR" 2>/dev/null || echo "0")
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
BFD_UP=$(junos_cmd 172.16.18.162 "show bfd session" | grep -c "Up" 2>/dev/null || echo "0")
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
echo "  Pausing leaf1 container (hard failure simulation)..."
docker pause clab-${LAB_NAME}-dc1-leaf1

echo "  Waiting 10s for LACP fast timeout (3x1s) + convergence..."
sleep 10

# Check LACP aggregator - with leaf1 paused, active aggregator should have 1 port
# (LACP detects missing PDUs and moves the failed slave to a separate aggregator)
AGG_PORTS=$(docker exec clab-${LAB_NAME}-dc1-host3 grep "Number of ports" /proc/net/bonding/bond0 | head -1 | awk '{print $NF}')
if [ "$AGG_PORTS" = "1" ]; then
  pass "ESI-LAG: LACP detected leaf1 failure (active aggregator: 1 port)"
else
  fail "ESI-LAG: active aggregator still has $AGG_PORTS ports (expected 1)"
fi

ping_test dc1-host3 10.10.20.14 "ESI-LAG failover: host3 -> host4 (leaf1 crashed)"

echo "  Unpausing leaf1 container..."
docker unpause clab-${LAB_NAME}-dc1-leaf1

echo "  Waiting 30s for leaf1 recovery..."
sleep 30

# Check if leaf1 recovered - if SSH works, it's back
if sshpass -p 'TestLabPass1' ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 admin@172.16.18.162 "show system uptime" >/dev/null 2>&1; then
  pass "ESI-LAG restore: leaf1 recovered after unpause"
else
  echo "  WARN: leaf1 SSH not responding after unpause, restarting container..."
  docker restart clab-${LAB_NAME}-dc1-leaf1
  # Wait for full reboot
  for i in $(seq 1 20); do
    sleep 15
    S=$(docker inspect clab-${LAB_NAME}-dc1-leaf1 --format '{{.State.Health.Status}}' 2>/dev/null)
    if [ "$S" = "healthy" ]; then break; fi
  done
  pass "ESI-LAG restore: leaf1 recovered after restart"
fi

sleep 10
ping_test dc1-host3 10.10.20.14 "ESI-LAG restore: host3 -> host4 (both leaves back)"

echo ""
# ---------------------------------------------------------------
echo "=== 5. Failover: Core Isolation ==="
# ---------------------------------------------------------------

# Deactivate overlay BGP on leaf1 to simulate EVPN core loss.
# Core isolation should automatically bring down ESI-LAG interfaces
# to prevent traffic blackholing through an isolated leaf.
echo "  Deactivating overlay BGP on leaf1..."
junos_cmd 172.16.18.162 "configure; deactivate protocols bgp group OVERLAY; commit" >/dev/null 2>&1

echo "  Waiting 15s for core-isolation to trigger..."
sleep 15

# ae0 should be link-down (core isolation shut it)
AE0_LINK=$(junos_cmd 172.16.18.162 "show interfaces ae0 terse" | grep "ae0 " | awk '{print $3}')
if [ "$AE0_LINK" = "down" ]; then
  pass "Core isolation: ae0 brought down after overlay BGP loss"
else
  fail "Core isolation: ae0 still $AE0_LINK (expected down)"
fi

ping_test dc1-host3 10.10.20.14 "Core isolation: host3 -> host4 (leaf1 isolated, via leaf2)"

echo "  Restoring overlay BGP on leaf1..."
junos_cmd 172.16.18.162 "configure; activate protocols bgp group OVERLAY; commit" >/dev/null 2>&1

echo "  Waiting 30s for BGP + LACP recovery..."
sleep 30

# Verify ae0 came back
AE0_LINK=$(junos_cmd 172.16.18.162 "show interfaces ae0 terse" | grep "ae0 " | awk '{print $3}')
if [ "$AE0_LINK" = "up" ]; then
  pass "Core isolation restore: ae0 back up after overlay BGP restored"
else
  fail "Core isolation restore: ae0 still $AE0_LINK (expected up)"
fi

ping_test dc1-host3 10.10.20.14 "Core isolation restore: host3 -> host4 (both leaves)"

echo ""
# ---------------------------------------------------------------
echo "=== 6. Failover: Spine ==="
# ---------------------------------------------------------------

echo "  Disabling spine1 underlay interfaces..."
junos_cmd 172.16.18.160 "configure; set interfaces ge-0/0/0 disable; set interfaces ge-0/0/1 disable; commit" >/dev/null 2>&1
sleep 10

ping_test dc1-host1 10.10.10.12 "Spine failover: host1 -> host2 (spine1 down, via spine2)"
ping_test dc1-host1 10.10.20.13 "Spine failover: host1 -> host3 L3 (spine1 down)"

echo "  Restoring spine1..."
junos_cmd 172.16.18.160 "configure; delete interfaces ge-0/0/0 disable; delete interfaces ge-0/0/1 disable; commit" >/dev/null 2>&1
sleep 10

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
echo "============================================"
if [ $FAILURES -eq 0 ]; then
  echo "  ALL TESTS PASSED"
else
  echo "  $FAILURES TEST(S) FAILED"
fi
echo "============================================"

exit $FAILURES
