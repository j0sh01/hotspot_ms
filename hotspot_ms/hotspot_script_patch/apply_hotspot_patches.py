#!/usr/bin/env python3
"""KiliGrid patches for hotspot_ms. Run from the app root (apps/hotspot_ms).

  1. Wi-Fi-only mode  HOTSPOT_LAN_PHY=none -> guest LAN is the existing br-lan (no NIC needed)
  2. DHCP             pool sized from the LAN mask (start 10, up to 1000), 2h leases
  3. IPv4-only        DHCPv6 / RA off in every mode (openNDS only captures IPv4)
  4. openNDS portal   router resolves the portal name to an IPv4; fasremoteip and the walled
                      garden use the IP, fasremotefqdn keeps the name (needed for HTTPS)

Safe to re-run: applied patches are tagged [kiligrid:<name>] and skipped. Originals are kept
as <file>.orig. Every anchor must match exactly one line, otherwise nothing is changed.
"""
import pathlib
import py_compile
import shutil
import subprocess
import sys

TAG = "kiligrid"
APP = pathlib.Path.cwd()
TEMPLATE = APP / "hotspot_ms" / "scripts" / "openwrt_setup.sh"
PROVISION = APP / "hotspot_ms" / "provision.py"

TEMPLATE_EDITS = [
	("wifi-flag", "ONE_ARMED=0", "after", [
		"WIFI_ONLY=0",
	]),
	("wifi-mode", 'elif ip link show "$LAN_PHY" >/dev/null 2>&1; then', "before", [
		'elif [ "$LAN_PHY" = "none" ] && ip link show br-lan >/dev/null 2>&1; then',
		"\tWIFI_ONLY=1",
		'\tLAN_IFACE="br-lan"',
		'\tsay "mode       : WIFI-ONLY (guest LAN is the existing br-lan, Wi-Fi AP only)"',
	]),
	("wifi-keep-bridge", 'uci add_list network.brlan.ports="$LAN_PHY"', "after", [
		'if [ "$WIFI_ONLY" = "1" ]; then',
		"\tuci -q delete network.brlan",
		"fi",
	]),
	("dhcp-lease", "uci set dhcp.lan.leasetime='12h'", "replace", [
		"uci set dhcp.lan.leasetime='2h'",
	]),
	("ipv4-only", "uci set dhcp.lan.dhcpv4='server'", "after", [
		"uci set dhcp.lan.dhcpv6='disabled'",
		"uci set dhcp.lan.ra='disabled'",
		"uci -q delete dhcp.lan.ra_slaac",
		"uci -q delete dhcp.lan.ra_flags",
	]),
	("portal-ip", 'if [ "$DRY_RUN" = "1" ]; then', "before", [
		"# openNDS rules and fasremoteip take addresses: resolve the portal name here.",
		'case "$PORTAL_HOST" in',
		"\t*[!0-9.]*) PORTAL_IP=\"$(nslookup \"$PORTAL_HOST\" 127.0.0.1 2>/dev/null | awk '/^Address/ && $2 !~ /:/ {print $2; exit}')\" ;;",
		'\t*) PORTAL_IP="$PORTAL_HOST" ;;',
		"esac",
		'[ -n "$PORTAL_IP" ] || die "cannot resolve $PORTAL_HOST to an IPv4 address"',
		'say "portal ip  : $PORTAL_IP"',
	]),
	("fas-ip", 'uci set "$nds".fasremoteip="$PORTAL_HOST"', "replace", [
		'uci set "$nds".fasremoteip="$PORTAL_IP"',
	]),
	("fas-fqdn", 'uci -q delete "$nds".fasremotefqdn', "replace", [
		'uci set "$nds".fasremotefqdn="$PORTAL_HOST"',
	]),
	("walled-garden",
	 'uci add_list "$nds".preauthenticated_users="allow tcp port $PORTAL_PORT to $PORTAL_HOST"',
	 "replace", [
		'uci add_list "$nds".preauthenticated_users="allow tcp port $PORTAL_PORT to $PORTAL_IP"',
	]),
]

PROVISION_EDITS = [
	("dhcp-start", '"__DHCP_START__": "100",', "replace", [
		'"__DHCP_START__": "10",',
	]),
	("dhcp-limit", '"__DHCP_LIMIT__": "150",', "replace", [
		'"__DHCP_LIMIT__": str(min(1000, max(10, 2 ** (32 - cidr_bits) - 20))),',
	]),
]


def find(lines, anchor):
	hits = [i for i, line in enumerate(lines) if line.strip() == anchor]
	if len(hits) != 1:
		sys.exit(f"anchor must match exactly one line, got {len(hits)}: {anchor}")
	return hits[0]


def apply_edit(lines, name, anchor, mode, new):
	marker = f"[{TAG}:{name}]"
	if any(marker in line for line in lines):
		print(f"  skip     {name} (already applied)")
		return lines
	i = find(lines, anchor)
	pad = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
	block = [pad + n for n in new]
	block[0] += f"  # {marker}"
	if mode == "replace":
		lines[i : i + 1] = block
	elif mode == "before":
		lines[i:i] = block
	elif mode == "after":
		lines[i + 1 : i + 1] = block
	else:
		sys.exit(f"bad mode {mode}")
	print(f"  applied  {name}")
	return lines


def sh_ok(path):
	return subprocess.run(["sh", "-n", str(path)]).returncode == 0


def py_ok(path):
	try:
		py_compile.compile(str(path), doraise=True)
		return True
	except py_compile.PyCompileError as exc:
		print(exc)
		return False


def main():
	for path in (TEMPLATE, PROVISION):
		if not path.exists():
			sys.exit(f"not found: {path}\nRun this from the app root (apps/hotspot_ms).")

	jobs = []
	for path, edits, check in ((TEMPLATE, TEMPLATE_EDITS, sh_ok), (PROVISION, PROVISION_EDITS, py_ok)):
		print(path.relative_to(APP))
		lines = path.read_text(encoding="utf-8").split("\n")
		for edit in edits:
			lines = apply_edit(lines, *edit)
		jobs.append((path, "\n".join(lines), check))

	# Everything matched: now write, keeping the first-ever copy as .orig.
	for path, text, _ in jobs:
		backup = path.with_name(path.name + ".orig")
		if not backup.exists():
			shutil.copy2(path, backup)
		path.write_text(text, encoding="utf-8")

	failed = [p for p, _, check in jobs if not check(p)]
	if failed:
		for path, _, _ in jobs:
			shutil.copy2(path.with_name(path.name + ".orig"), path)
		sys.exit("syntax check failed for " + ", ".join(p.name for p in failed) + "; originals restored")

	print("\nDone. Review with: git diff   (restart workers so provision.py reloads: bench restart)")


if __name__ == "__main__":
	main()