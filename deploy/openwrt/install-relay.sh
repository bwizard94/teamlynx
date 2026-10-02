#!/bin/sh
# Install the TeamLynx relay on an OpenWrt / GL.iNet travel router, from a laptop in the repo root.
# The router needs internet once (WAN cable or repeater mode) for opkg and pip; in the field it is offline.
#
#   lynx-field netconfig deploy/squad.example.json --out build/net      # 1. generate the router script
#   deploy/openwrt/install-relay.sh root@192.168.8.1 build/net/router/lynx-router-setup.sh
#
# Steps: python3 + websockets on the router, relay code to /opt/lynx (lynx.net + lynx.field only, no numpy),
# procd service + uci config + umdns service, start the relay, then apply the squad network script last
# (it reloads Wi-Fi and the LAN, which may drop this SSH session).
set -eu

ROUTER=${1:?usage: install-relay.sh root@ROUTER [lynx-router-setup.sh]}
SETUP=${2:-}
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
SSH="ssh -o ConnectTimeout=10"

echo "== packages on $ROUTER"
$SSH "$ROUTER" 'set -e; opkg update; opkg install python3 python3-pip; pip3 install --no-cache-dir "websockets>=13,<18"'

echo "== relay code -> /opt/lynx"
tar -C "$REPO" --exclude='__pycache__' -cf - lynx/__init__.py lynx/net lynx/field \
	| $SSH "$ROUTER" 'set -e; rm -rf /opt/lynx/lynx; mkdir -p /opt/lynx; tar -C /opt/lynx -xf -'

echo "== service files"
tar -C "$HERE/files" -cf - etc | $SSH "$ROUTER" 'set -e
	rm -rf /tmp/lynx-files; mkdir -p /tmp/lynx-files; tar -C /tmp/lynx-files -xf -
	cp /tmp/lynx-files/etc/init.d/lynx-relay /etc/init.d/lynx-relay; chmod 755 /etc/init.d/lynx-relay
	mkdir -p /etc/umdns; cp /tmp/lynx-files/etc/umdns/lynx-relay.json /etc/umdns/lynx-relay.json
	[ -f /etc/config/lynx ] || cp /tmp/lynx-files/etc/config/lynx /etc/config/lynx
	rm -rf /tmp/lynx-files'

echo "== self-test and start"
$SSH "$ROUTER" 'set -e
	PYTHONPATH=/opt/lynx python3 -c "import lynx.field.relay, sys; assert \"numpy\" not in sys.modules; print(\"relay import ok\")"
	/etc/init.d/lynx-relay enable; /etc/init.d/lynx-relay restart; sleep 2
	(logread -e lynx 2>/dev/null || logread) | tail -n 5'

if [ -n "$SETUP" ]; then
	echo "== squad network ($SETUP); the SSH session may drop while Wi-Fi reloads"
	$SSH "$ROUTER" 'cat > /tmp/lynx-router-setup.sh' < "$SETUP"
	$SSH "$ROUTER" 'sh /tmp/lynx-router-setup.sh' || echo "(session closed during network reload - expected)"
fi
echo "done. Join the squad SSID and run: lynx-field discover"
