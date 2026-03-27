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

# Post-failure withdrawal: leaf2 should drop the remote VTEP entry for the
# crashed leaf once BGP hold timer expires (~90s on default Junos timers).
# We have already waited 10s for LACP - wait the rest before sampling.
echo "  Waiting for BGP hold timer expiry to verify VTEP withdrawal..."
sleep 90
VTEP_DURING=$(junos_cmd 172.16.18.163 "show ethernet-switching vxlan-tunnel-end-point remote" | grep -c "^ 10.1.0.3 ")
if [ "$VTEP_BEFORE" = "1" ] && [ "$VTEP_DURING" = "0" ]; then
  pass "Post-failure withdrawal: leaf2 dropped remote VTEP 10.1.0.3 (was=$VTEP_BEFORE now=$VTEP_DURING)"
else
  fail "Post-failure withdrawal: leaf2 VTEP for 10.1.0.3 was=$VTEP_BEFORE now=$VTEP_DURING (expected 1 -> 0)"
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

echo "  Waiting for BGP + LACP recovery (includes hold-time up 60s)..."
wait_bgp_converged 172.16.18.162 "leaf1"

# Core isolation has hold-time up (60s) - ae0 stays down after BGP recovers
# to prevent flapping. Wait for ae0 to come back.
waited=0
while [ $waited -lt $MAX_WAIT ]; do
  AE0_LINK=$(junos_cmd 172.16.18.162 "show interfaces ae0 terse" | grep "ae0 " | awk '{print $3}')
  if [ "$AE0_LINK" = "up" ]; then break; fi
  sleep 10
  waited=$((waited+10))
done

if [ "$AE0_LINK" = "up" ]; then
  pass "Core isolation restore: ae0 back up (${waited}s after BGP)"
else
  fail "Core isolation restore: ae0 still $AE0_LINK after ${MAX_WAIT}s"
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

# A. Underlay ECMP next-hop count
# Asserts the forwarding-table export LOAD-BALANCE policy is active.
# Without it, BGP multipath shows multiple paths but PFE installs only one.
ECMP_NH=$(junos_cmd 172.16.18.162 "show route forwarding-table destination 10.1.0.4/32 table default" | grep -cE "ucst .* ge-")
if [ "$ECMP_NH" = "2" ]; then
  pass "ECMP: leaf1 -> leaf2 loopback installed via 2 next-hops (both spines)"
else
  fail "ECMP: leaf1 -> leaf2 loopback has $ECMP_NH next-hops in PFE (expected 2)"
fi

# B. EVPN route-type breakdown on leaf1.
# Type-2 = MAC/IP, Type-3 = IMET (one per remote VTEP per L2VNI), Type-5 = IP-prefix.
# With 1 remote leaf, 2 L2VNIs, hosts on both VLANs: T2 >= 4, T3 >= 2, T5 >= 1.
T2=$(junos_cmd 172.16.18.162 "show route table bgp.evpn.0 match-prefix 2:*" | grep -c "^2:")
T3=$(junos_cmd 172.16.18.162 "show route table bgp.evpn.0 match-prefix 3:*" | grep -c "^3:")
T5=$(junos_cmd 172.16.18.162 "show route table bgp.evpn.0 match-prefix 5:*" | grep -c "^5:")
if [ "$T2" -ge 4 ]; then pass "EVPN Type-2 (MAC/IP) routes: $T2"; else fail "EVPN Type-2 routes: $T2 (expected >= 4)"; fi
if [ "$T3" -ge 2 ]; then pass "EVPN Type-3 (IMET) routes: $T3"; else fail "EVPN Type-3 routes: $T3 (expected >= 2)"; fi
if [ "$T5" -ge 1 ]; then pass "EVPN Type-5 (IP-prefix) routes: $T5"; else fail "EVPN Type-5 routes: $T5 (expected >= 1)"; fi

# C. Type-5 actually programs TENANT-1.inet.0
# Confirms L3VNI control plane is wired up, not just routes in bgp.evpn.0.
T5_VRF=$(junos_cmd 172.16.18.162 "show route table TENANT-1.inet.0 protocol evpn" | grep -c "\*\[EVPN")
if [ "$T5_VRF" -ge 1 ]; then
  pass "TENANT-1.inet.0: $T5_VRF routes installed via EVPN (L3VNI working)"
else
  fail "TENANT-1.inet.0: no EVPN routes (Type-5 not programming VRF)"
fi

# D. Per-peer overlay BGP: Established AND receiving EVPN NLRI.
# Replaces "0 down peers" which can hide an Idle peer with family mismatch.
# Check Received prefixes (not Active) - with two RRs, only one wins best-path
# but both should be receiving the full set.
for peer in 10.1.0.1 10.1.0.2; do
  NBR=$(junos_cmd 172.16.18.162 "show bgp neighbor $peer")
  STATE=$(echo "$NBR" | grep -m1 "State: " | awk '{print $4}')
  RECVD=$(echo "$NBR" | grep -m1 "Received prefixes" | awk '{print $NF}')
  if [ "$STATE" = "Established" ] && [ -n "$RECVD" ] && [ "$RECVD" -gt 0 ] 2>/dev/null; then
    pass "Overlay BGP leaf1 -> $peer: Established, $RECVD received EVPN prefixes"
  else
    fail "Overlay BGP leaf1 -> $peer: state=$STATE received=$RECVD"
  fi
done

# E. Jumbo MTU end-to-end across the underlay.
# 8972 = 9000 - 20 IP - 8 ICMP. Underlay MTU 9192 leaves room for 50B VXLAN overhead.
# Catches a too-small underlay MTU that ARP-sized traffic would never expose.
MTU_OK=$(junos_cmd 172.16.18.162 "ping 10.1.0.4 source 10.1.0.3 size 8972 do-not-fragment count 3 rapid" | grep -o "0% packet loss")
if [ "$MTU_OK" = "0% packet loss" ]; then
  pass "MTU: jumbo (size 8972 DF) leaf1 -> leaf2 loopback"
else
  fail "MTU: jumbo ping leaf1 -> leaf2 loopback failed (underlay MTU too small?)"
fi

# G. DF election + ESI consistency across both leaves.
# For each LACP-derived ESI (01:00:00...), both leaves must agree on the DF.
# Mismatch = split-brain BUM forwarder = duplicated broadcast traffic.
DF_L1=$(junos_cmd 172.16.18.162 "show evpn instance designated-forwarder")
DF_L2=$(junos_cmd 172.16.18.163 "show evpn instance designated-forwarder")
ESI_L1=$(echo "$DF_L1" | grep -c "ESI: 01:")
ESI_L2=$(echo "$DF_L2" | grep -c "ESI: 01:")
if [ "$ESI_L1" -ge 2 ] && [ "$ESI_L1" = "$ESI_L2" ]; then
  pass "ESI consistency: both leaves see $ESI_L1 LACP-derived ESIs"
else
  fail "ESI consistency: leaf1=$ESI_L1 leaf2=$ESI_L2 (expected matching, >= 2)"
fi

DF_MISMATCH=0
DF_CHECKED=0
while read -r esi; do
  [ -z "$esi" ] && continue
  DF1=$(echo "$DF_L1" | awk -v e="$esi" '$0~e{getline; print $NF}')
  DF2=$(echo "$DF_L2" | awk -v e="$esi" '$0~e{getline; print $NF}')
  DF_CHECKED=$((DF_CHECKED+1))
  if [ -z "$DF1" ] || [ "$DF1" != "$DF2" ]; then
    DF_MISMATCH=$((DF_MISMATCH+1))
  fi
done <<< "$(echo "$DF_L1" | awk '/ESI: 01:/ {print $2}')"
if [ "$DF_CHECKED" -gt 0 ] && [ "$DF_MISMATCH" = "0" ]; then
  pass "DF election: $DF_CHECKED ESIs, both leaves agree on DF"
else
  fail "DF election: $DF_MISMATCH/$DF_CHECKED ESIs have mismatched DF between leaves"
fi

# H. Duplicate-MAC detection state must be empty.
# duplicate-mac-detection is configured in EVPN-VXLAN; any entry here means
# a real loop or mis-cabling, not a false positive.
DUP=$(junos_cmd 172.16.18.162 "show evpn database state duplicate" | grep -cE "^[[:space:]]*[0-9]+[[:space:]]")
if [ "$DUP" = "0" ]; then
  pass "EVPN duplicate-MAC: 0 duplicate entries"
else
  fail "EVPN duplicate-MAC: $DUP duplicate entries (loop or mis-cabling?)"
fi

# J. BFD session health on leaf1.
# Assert every BFD session is Up AND every session reports Local diagnostic None.
# Catches the case where a session is technically Up but flapping with diag bits.
BFD_OUT=$(junos_cmd 172.16.18.162 "show bfd session extensive")
BFD_UP_COUNT=$(echo "$BFD_OUT" | grep -cE "^[0-9.]+ +Up ")
BFD_DIAG_OK=$(echo "$BFD_OUT" | grep -c "Local diagnostic None")
if [ "$BFD_UP_COUNT" -ge 2 ] && [ "$BFD_DIAG_OK" = "$BFD_UP_COUNT" ]; then
  pass "BFD: $BFD_UP_COUNT sessions Up, all with diag=None"
else
  fail "BFD: $BFD_UP_COUNT up, $BFD_DIAG_OK clean diag (expected matching, >= 2)"
fi

# K. Underlay interface error/drop counters on leaf1.
# Carrier transitions are allowed to be > 0 (Section 6 spine failover causes flaps),
# but Errors and Drops on the fabric interfaces should always be 0.
# A nonzero count here means real packet loss on the underlay.
IFACE_BAD=0
for iface in ge-0/0/0 ge-0/0/1; do
  STATS=$(junos_cmd 172.16.18.162 "show interfaces $iface extensive" | grep -A1 "Input errors:" | tail -1)
  ERRS=$(echo "$STATS" | sed -n 's/.*Errors: \([0-9]*\).*/\1/p')
  DROPS=$(echo "$STATS" | sed -n 's/.*Drops: \([0-9]*\).*/\1/p')
  if [ "${ERRS:-1}" != "0" ] || [ "${DROPS:-1}" != "0" ]; then
    IFACE_BAD=$((IFACE_BAD+1))
    echo "    leaf1 $iface: errors=$ERRS drops=$DROPS"
  fi
done
if [ "$IFACE_BAD" = "0" ]; then
  pass "Underlay counters: leaf1 fabric interfaces clean (0 errors, 0 drops)"
else
  fail "Underlay counters: $IFACE_BAD fabric interfaces have errors/drops"
fi

# F. EVPN database must contain MAC+IP entries (regression test for no-arp-suppression).
# With ARP suppression at default-on, leaves snoop host ARPs into the EVPN db.
# If `no-arp-suppression` is re-introduced, locally-learned entries lose their IP column.
# Count database entries that have a non-empty IP field.
DB_WITH_IP=$(junos_cmd 172.16.18.162 "show evpn database" | awk 'NR>2 && NF>=6 && $NF ~ /^[0-9]+\./ {c++} END {print c+0}')
if [ "$DB_WITH_IP" -ge 6 ]; then
  pass "EVPN database: $DB_WITH_IP entries with MAC+IP (ARP suppression working)"
else
  fail "EVPN database: only $DB_WITH_IP MAC+IP entries (no-arp-suppression regression?)"
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
