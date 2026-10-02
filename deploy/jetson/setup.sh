#!/usr/bin/env bash
# TeamLynx Jetson setup (JetPack 5/6, Ubuntu 20.04/22.04). Idempotent; re-run after updating the repo.
#
#   sudo deploy/jetson/setup.sh --profile build/net/nodes/lynx-n03.json --site deploy/lynx/site.example.json \
#        --ssid LYNX-SQUAD --psk 'change-this-squad-passphrase'
#   sudo deploy/jetson/setup.sh ... --leader          # squad leader: secondary relay + recorder + time source
#   deploy/jetson/setup.sh ... --dry-run              # print every action, change nothing
#
# What it does: apt packages; code in /opt/lynx/src and a venv that sees JetPack's CUDA OpenCV
# (--system-site-packages); /etc/lynx/{node,site}.json; /var/lib/lynx and /var/log/lynx; udev names
# /dev/lynx-imu and /dev/lynx-gnss; the squad Wi-Fi profile (WPA3-SAE, PMF required, power save OFF);
# chrony following the squad leader; systemd units (headset; bridged relay on the leader); optional max clocks.
set -euo pipefail

PROFILE=""
SITE=""
SSID=""
PSK=""
LEADER=0
DRY=0
MAXPERF=0
NOWIFI=0
RUN_USER="${SUDO_USER:-${USER:-lynx}}"
PREFIX=/opt/lynx
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

usage() {
	sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
	echo "options: --profile FILE --site FILE --ssid S --psk P [--leader] [--user U] [--prefix DIR] [--max-perf] [--no-wifi] [--dry-run]"
	exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
	case "$1" in
		--profile) PROFILE="$2"; shift 2 ;;
		--site) SITE="$2"; shift 2 ;;
		--ssid) SSID="$2"; shift 2 ;;
		--psk) PSK="$2"; shift 2 ;;
		--leader) LEADER=1; shift ;;
		--user) RUN_USER="$2"; shift 2 ;;
		--prefix) PREFIX="$2"; shift 2 ;;
		--max-perf) MAXPERF=1; shift ;;
		--no-wifi) NOWIFI=1; shift ;;
		--dry-run) DRY=1; shift ;;
		-h|--help) usage 0 ;;
		*) echo "unknown option $1" >&2; usage 2 ;;
	esac
done

[[ -n "$PROFILE" ]] || { echo "--profile is required (lynx-field netconfig writes one per node)" >&2; exit 2; }
[[ -f "$PROFILE" ]] || { echo "profile $PROFILE not found" >&2; exit 2; }
if [[ $NOWIFI -eq 0 ]]; then
	[[ -n "$SSID" && -n "$PSK" ]] || { echo "--ssid and --psk are required (or --no-wifi)" >&2; exit 2; }
	[[ ${#PSK} -ge 12 ]] || { echo "--psk must be at least 12 characters" >&2; exit 2; }
fi
if [[ $DRY -eq 0 && $EUID -ne 0 ]]; then
	echo "run as root (sudo), or use --dry-run" >&2
	exit 1
fi

VENV="$PREFIX/venv"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6 || true)"
RUN_HOME="${RUN_HOME:-/home/$RUN_USER}"

run() {
	if [[ $DRY -eq 1 ]]; then
		printf '+ %s\n' "$*"
	else
		"$@"
	fi
}

write_file() { # write_file PATH MODE  (content on stdin)
	local path="$1" mode="$2"
	if [[ $DRY -eq 1 ]]; then
		printf '+ write %s (%s)\n' "$path" "$mode"
		sed 's/^/|   /'
	else
		install -d "$(dirname "$path")"
		cat > "$path"
		chmod "$mode" "$path"
	fi
}

render_unit() { # render_unit TEMPLATE -> stdout
	sed -e "s|@USER@|$RUN_USER|g" -e "s|@HOME@|$RUN_HOME|g" -e "s|@VENV@|$VENV|g" "$1"
}

echo "== TeamLynx Jetson setup: user $RUN_USER, prefix $PREFIX, leader=$LEADER"
if [[ -f /etc/nv_tegra_release ]]; then
	head -n1 /etc/nv_tegra_release
else
	echo "(not a Jetson: continuing; the vision stack will use the CPU)"
fi

echo "== packages"
run apt-get update
run apt-get install -y --no-install-recommends python3-venv python3-pip python3-dev chrony avahi-daemon \
	network-manager v4l-utils rsync

echo "== code -> $PREFIX/src, venv -> $VENV"
run install -d "$PREFIX"
run rsync -a --delete --exclude '.git' --exclude '__pycache__' --exclude 'build' "$SRC_DIR/" "$PREFIX/src/"
if [[ ! -x "$VENV/bin/python" ]]; then
	run python3 -m venv --system-site-packages "$VENV"
fi
run "$VENV/bin/pip" install --upgrade pip
# cv2 comes from JetPack (system site packages, CUDA build); never pip-install opencv here.
run "$VENV/bin/pip" install -e "$PREFIX/src[hw,field]" scipy

echo "== config and data directories"
run install -d -m 0755 /etc/lynx
write_file /etc/lynx/node.json 0644 < "$PROFILE"
if [[ -n "$SITE" ]]; then
	write_file /etc/lynx/site.json 0644 < "$SITE"
fi
run install -d -o "$RUN_USER" -g "$RUN_USER" -m 0755 /var/lib/lynx /var/log/lynx
run usermod -aG dialout,video "$RUN_USER"

echo "== udev"
write_file /etc/udev/rules.d/99-lynx.rules 0644 < "$SRC_DIR/deploy/udev/99-lynx.rules"
run udevadm control --reload
run udevadm trigger --subsystem-match=tty

if [[ $NOWIFI -eq 0 ]]; then
	echo "== squad Wi-Fi ($SSID): WPA3-SAE, PMF required, power save off"
	write_file /etc/NetworkManager/conf.d/lynx-wifi-powersave.conf 0644 <<-'EOF'
	[connection]
	# 2 = disable: 802.11 power save adds up to a beacon interval (~100 ms) of downlink latency
	wifi.powersave = 2
	EOF
	if nmcli -t -f NAME connection show 2>/dev/null | grep -qx lynx; then
		run nmcli connection delete lynx
	fi
	run nmcli connection add type wifi con-name lynx ifname '*' ssid "$SSID" \
		wifi-sec.key-mgmt sae wifi-sec.psk "$PSK" wifi-sec.pmf required \
		802-11-wireless.powersave 2 connection.autoconnect yes connection.autoconnect-priority 100 \
		ipv4.method auto ipv6.method disabled
	run systemctl reload NetworkManager
fi

echo "== time: chrony"
if [[ $LEADER -eq 1 ]]; then
	write_file /etc/chrony/lynx.conf 0644 < "$SRC_DIR/deploy/chrony/lynx-leader.conf"
else
	write_file /etc/chrony/lynx.conf 0644 < "$SRC_DIR/deploy/chrony/lynx-node.conf"
fi
if [[ $DRY -eq 1 ]] || ! grep -qx 'include /etc/chrony/lynx.conf' /etc/chrony/chrony.conf 2>/dev/null; then
	if [[ $DRY -eq 1 ]]; then
		echo "+ append 'include /etc/chrony/lynx.conf' to /etc/chrony/chrony.conf"
	else
		echo 'include /etc/chrony/lynx.conf' >> /etc/chrony/chrony.conf
	fi
fi
run systemctl restart chrony

echo "== systemd units"
render_unit "$SRC_DIR/deploy/systemd/lynx-headset.service" | write_file /etc/systemd/system/lynx-headset.service 0644
UNITS=(lynx-headset.service)
if [[ $LEADER -eq 1 ]]; then
	render_unit "$SRC_DIR/deploy/systemd/lynx-relay.service" | write_file /etc/systemd/system/lynx-relay.service 0644
	render_unit "$SRC_DIR/deploy/systemd/lynx-recorder.service" | write_file /etc/systemd/system/lynx-recorder.service 0644
	write_file /etc/lynx/relay.env 0644 <<-'EOF'
	# Secondary relay bridged to the router's primary. It sees the whole squad through the bridge, so its
	# log is the session record (the router has no storage); lynx-recorder.service is only for setups
	# without a leader relay.
	LYNX_RELAY_ARGS=--role secondary --priority 10 --upstream ws://relay.lynx:8765 --log /var/log/lynx --log-gz
	EOF
	write_file /etc/avahi/services/lynx-relay.service 0644 < "$SRC_DIR/deploy/avahi/lynx-relay.service"
	UNITS+=(lynx-relay.service)
fi
run systemctl daemon-reload
run systemctl enable "${UNITS[@]}"

if [[ $MAXPERF -eq 1 ]] && command -v nvpmodel >/dev/null 2>&1; then
	echo "== max performance mode"
	run nvpmodel -m 0
	run jetson_clocks
fi

echo "== done"
echo "check:   $VENV/bin/lynx-field headset --profile /etc/lynx/node.json --dry-run"
echo "start:   sudo systemctl start ${UNITS[*]}"
echo "logs:    journalctl -fu lynx-headset"
