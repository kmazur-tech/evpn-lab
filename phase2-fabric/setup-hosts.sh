#!/bin/bash
# Setup test hosts for DC1 EVPN-VXLAN fabric
# Run after containerlab deploy when all nodes are up.
#
# Host layout:
#   host1: single-homed to leaf1, VLAN 10 (10.10.10.11)
#   host2: single-homed to leaf2, VLAN 10 (10.10.10.12)
#   host3: dual-homed ESI-LAG to both leaves, VLAN 20 (10.10.20.13)
#   host4: dual-homed ESI-LAG to both leaves, VLAN 20 (10.10.20.14)
#
# vjunos-switch IRB does not generate ARP replies.
# Static ARP entries are set on all hosts for the anycast gateway MAC.

set -e

LAB_NAME="${1:-dc1}"
GW_MAC="00:00:5e:00:01:01"  # virtual-gateway-v4-mac from leaf IRB config

echo "=== Configuring hosts for lab: $LAB_NAME ==="

# --- host1: single-homed, VLAN 10 ---
echo "[host1] Single-homed leaf1, VLAN 10"
docker exec clab-${LAB_NAME}-dc1-host1 sh -c "
  ip addr add 10.10.10.11/24 dev eth1 2>/dev/null || true
  ip route replace default via 10.10.10.1
  arp -s 10.10.10.1 ${GW_MAC}
"

# --- host2: single-homed, VLAN 10 ---
echo "[host2] Single-homed leaf2, VLAN 10"
docker exec clab-${LAB_NAME}-dc1-host2 sh -c "
  ip addr add 10.10.10.12/24 dev eth1 2>/dev/null || true
  ip route replace default via 10.10.10.1
  arp -s 10.10.10.1 ${GW_MAC}
"

# --- host3: dual-homed ESI-LAG, VLAN 20 ---
echo "[host3] Dual-homed ESI-LAG, VLAN 20"
docker exec clab-${LAB_NAME}-dc1-host3 sh -c "
  # Remove existing bond if any
  ip link del bond0 2>/dev/null || true

  # Load bonding module (from host kernel)
  modprobe bonding 2>/dev/null || true

  # Create bond with 802.3ad mode
  ip link add bond0 type bond
  echo 802.3ad > /sys/class/net/bond0/bonding/mode
  echo 100 > /sys/class/net/bond0/bonding/miimon

  # Add slaves
  ip link set eth1 down
  ip link set eth2 down
  ip link set eth1 master bond0
  ip link set eth2 master bond0
  ip link set bond0 up
  ip link set eth1 up
  ip link set eth2 up

  # IP config
  ip addr add 10.10.20.13/24 dev bond0 2>/dev/null || true
  ip route replace default via 10.10.20.1

  # Static ARP for gateway
  arp -s 10.10.20.1 ${GW_MAC}
"

# --- host4: dual-homed ESI-LAG, VLAN 20 ---
echo "[host4] Dual-homed ESI-LAG, VLAN 20"
docker exec clab-${LAB_NAME}-dc1-host4 sh -c "
  ip link del bond0 2>/dev/null || true
  modprobe bonding 2>/dev/null || true

  ip link add bond0 type bond
  echo 802.3ad > /sys/class/net/bond0/bonding/mode
  echo 100 > /sys/class/net/bond0/bonding/miimon

  ip link set eth1 down
  ip link set eth2 down
  ip link set eth1 master bond0
  ip link set eth2 master bond0
  ip link set bond0 up
  ip link set eth1 up
  ip link set eth2 up

  ip addr add 10.10.20.14/24 dev bond0 2>/dev/null || true
  ip route replace default via 10.10.20.1

  arp -s 10.10.20.1 ${GW_MAC}
"

echo ""
echo "=== Verifying ==="
for h in dc1-host1 dc1-host2 dc1-host3 dc1-host4; do
  IP=$(docker exec clab-${LAB_NAME}-${h} ip -4 addr show | grep "inet 10\." | awk '{print $2}')
  GW=$(docker exec clab-${LAB_NAME}-${h} ip route show default | awk '{print $3}')
  ARP=$(docker exec clab-${LAB_NAME}-${h} arp -a | grep "$GW_MAC" | head -1)
  echo "  ${h}: ${IP} gw=${GW} arp=${ARP:+OK}"
done

echo ""
echo "=== Quick traffic tests ==="
echo -n "  L2 (host1->host2): "
docker exec clab-${LAB_NAME}-dc1-host1 ping -c 1 -W 2 10.10.10.12 2>&1 | grep -o "[0-9]* received" || echo "FAIL"

echo -n "  L3 (host1->host3): "
docker exec clab-${LAB_NAME}-dc1-host1 ping -c 1 -W 2 10.10.20.13 2>&1 | grep -o "[0-9]* received" || echo "FAIL"

echo -n "  ESI-LAG (host3->host4): "
docker exec clab-${LAB_NAME}-dc1-host3 ping -c 1 -W 2 10.10.20.14 2>&1 | grep -o "[0-9]* received" || echo "FAIL"

echo ""
echo "=== Done ==="
