from __future__ import annotations

import secrets
import string
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import frappe
from frappe.utils import cint, flt, get_url

SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"
BRIDGE_NAME = "br-lan"

# One free tier, deliberately. The portal collapses "the free plan" into a
# single offer, so enabling several free plans used to make the granted plan
# depend on row order.
DEFAULT_PLANS: tuple[dict[str, Any], ...] = (
	{
		"plan_name": "Free - 10 Minutes Speed Test",
		"price": 0,
		"is_free": 1,
		"requires_ad_view": 0,
		"validity_value": 10,
		"validity_unit": "Minutes",
		"max_slices_per_day": 4,
		"description": "Free 10-minute speed test trial. One claim per device per day.",
	},
	{
		"plan_name": "Free - 1 Hour (Ad Supported)",
		"price": 0,
		"is_free": 1,
		"requires_ad_view": 1,
		"validity_value": 15,
		"validity_unit": "Minutes",
		"max_slices_per_day": 4,
		"description": "Watch a short ad to unlock 15 more minutes, up to 4 times a day.",
	},
	{
		"plan_name": "TSh 500 - 6 Hours",
		"price": 500,
		"is_free": 0,
		"validity_value": 6,
		"validity_unit": "Hours",
		"description": "6 hours of access.",
	},
	{
		"plan_name": "TSh 1,000 - 24 Hours",
		"price": 1000,
		"is_free": 0,
		"validity_value": 24,
		"validity_unit": "Hours",
		"description": "A full day of access.",
	},
	{
		"plan_name": "TSh 5,000 - 7 Days",
		"price": 5000,
		"is_free": 0,
		"validity_value": 7,
		"validity_unit": "Days",
		"description": "A full week of access.",
	},
)

# The free plan the portal should offer when a fresh site is seeded.
PRIMARY_FREE_PLAN = "Free - 10 Minutes Speed Test"

_VALIDITY_MINUTES = {"minute": 1, "hour": 60, "day": 1440}


def _validity_to_minutes(value: Any, unit: str | None) -> int:
	key = (unit or "Days").strip().lower().rstrip("s")
	return max(1, (cint(value) or 1) * _VALIDITY_MINUTES.get(key, 1440))


def _netmask(bits: int) -> str:
	if bits <= 0:
		return "0.0.0.0"
	mask = (0xFFFFFFFF << (32 - bits)) & 0xFFFFFFFF
	return ".".join(str((mask >> shift) & 0xFF) for shift in (24, 16, 8, 0))


def _random_key(length: int = 16) -> str:
	alphabet = string.ascii_letters + string.digits
	return "".join(secrets.choice(alphabet) for _ in range(length))


def _read_script(filename: str) -> str:
	path = SCRIPTS_DIR / filename
	if not path.exists():
		frappe.throw(f"Missing router script: {path}")
	return path.read_text(encoding="utf-8")


def _split_ipv4_cidr(value: str) -> tuple[str, int]:
	text = (value or "").strip()
	if "/" in text:
		ip, bits = text.split("/", 1)
		return ip.strip(), cint(bits) or 24
	return text, 24


def _longest_plan_minutes() -> int:
	rows = frappe.get_all(
		"Hotspot Plan",
		filters={"enabled": 1},
		fields=["validity_value", "validity_unit"],
		ignore_permissions=True,
	)
	if not rows:
		return 1440
	return max(_validity_to_minutes(row.validity_value, row.validity_unit) for row in rows)


def _free_plan_names() -> list[str]:
	return [
		row.name
		for row in frappe.get_all(
			"Hotspot Plan",
			filters={"is_free": 1},
			fields=["name"],
			ignore_permissions=True,
		)
	]


# ---------------------------------------------------------------------------
# plans
# ---------------------------------------------------------------------------
def ensure_plans(dry_run: bool = False) -> dict[str, Any]:
	"""Create the starter plans and keep exactly one free tier enabled.

	Existing plans are never overwritten - only missing ones are created - so
	operator edits survive a re-run.
	"""
	created: list[str] = []
	disabled_free: list[str] = []

	for row in DEFAULT_PLANS:
		existing = frappe.db.get_value("Hotspot Plan", {"plan_name": row["plan_name"]}, "name")
		if existing:
			continue
		if dry_run:
			created.append(row["plan_name"])
			continue

		plan = frappe.new_doc("Hotspot Plan")
		plan.update(
			{
				"plan_name": row["plan_name"],
				"enabled": 1,
				"price": row["price"],
				"currency": "TZS",
				"is_free": row.get("is_free", 0),
				"requires_ad_view": row.get("requires_ad_view", 0),
				"validity_value": row["validity_value"],
				"validity_unit": row["validity_unit"],
				"max_slices_per_day": row.get("max_slices_per_day", 0),
				"description": row.get("description", ""),
			}
		)
		plan.insert(ignore_permissions=True)
		created.append(plan.name)

	# Only one free offer, otherwise "the free plan" is ambiguous.
	for name in _free_plan_names():
		if name == PRIMARY_FREE_PLAN:
			continue
		if not cint(frappe.db.get_value("Hotspot Plan", name, "enabled")):
			continue
		if dry_run:
			disabled_free.append(name)
			continue
		frappe.db.set_value("Hotspot Plan", name, "enabled", 0, update_modified=False)
		disabled_free.append(name)

	if not dry_run:
		frappe.db.commit()

	return {"created": created, "disabled_free_plans": disabled_free}


# ---------------------------------------------------------------------------
# vouchers (desk / reseller use)
# ---------------------------------------------------------------------------
def issue_vouchers(plan: str, quantity: int = 1, customer: str | None = None) -> dict[str, Any]:
	"""Issue unused voucher codes for a plan.

	The portal is payment-first, so this is the offline path: staff and resellers
	print or hand out codes. Returns the codes so they can be exported.
	"""
	from hotspot_ms.api.portal import generate_unique_voucher_code

	plan_doc = frappe.get_doc("Hotspot Plan", plan)
	quantity = max(1, cint(quantity) or 1)
	codes: list[str] = []

	for _ in range(quantity):
		voucher = frappe.new_doc("Hotspot Voucher")
		voucher.voucher_code = generate_unique_voucher_code(7)
		voucher.status = "New"
		voucher.plan = plan_doc.name
		if customer:
			voucher.customer = customer
		if flt(plan_doc.data_limit_mb) > 0:
			voucher.data_limit_mb = flt(plan_doc.data_limit_mb)
		voucher.insert(ignore_permissions=True)
		codes.append(voucher.voucher_code)

	frappe.db.commit()
	return {"plan": plan_doc.name, "quantity": len(codes), "codes": codes}


# ---------------------------------------------------------------------------
# nas device
# ---------------------------------------------------------------------------
def ensure_nas(
	name: str,
	lan_ip: str = "192.168.10.1",
	lan_interface: str = "eth1",
	ssh_host: str | None = None,
	portal_url: str | None = None,
	location: str | None = None,
) -> dict[str, Any]:
	"""Create or top up the Nas Device record and its secrets.

	The DocType auto-names on device_name, so the record name, device_name and
	short_name are all kept equal to `name`. That matters because openNDS sends
	its `gatewayname` to the portal and the portal resolves it back to this
	record, and because the setup script writes it as `gatewayname`.
	"""
	exists = frappe.db.exists("Nas Device", name)
	doc = frappe.get_doc("Nas Device", name) if exists else frappe.new_doc("Nas Device")

	doc.device_name = name
	doc.short_name = name
	if location:
		doc.location = location

	doc.enabled = 1
	doc.nas_type = "OpenWrt"
	doc.ip_address = lan_ip
	doc.lan_interface = lan_interface
	doc.opennds_gateway_port = cint(doc.opennds_gateway_port) or 2050
	if ssh_host:
		doc.vpn_ip_address = ssh_host
	if portal_url:
		doc.portal_url = portal_url.rstrip("/")

	if not (doc.get_password("shared_secret", raise_exception=False) or "").strip():
		doc.shared_secret = _random_key(24)
	if not (doc.opennds_fas_key or "").strip():
		doc.opennds_fas_key = _random_key(24)

	doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"name": doc.name,
		"created": not exists,
		"ip_address": doc.ip_address,
		"lan_interface": doc.lan_interface,
		"vpn_ip_address": doc.vpn_ip_address,
		"portal_url": doc.portal_url or get_url(),
		"opennds_gateway_port": doc.opennds_gateway_port,
		"opennds_fas_key": doc.opennds_fas_key,
		"shared_secret": doc.get_password("shared_secret"),
	}


def _resolve_portal_url(doc) -> str:
	value = (doc.get("portal_url") or "").strip() or get_url()
	if "://" not in value:
		value = f"http://{value}"
	return value.rstrip("/")


# ---------------------------------------------------------------------------
# router script
# ---------------------------------------------------------------------------
def render_router_setup_script(name: str) -> dict[str, Any]:
	"""Render the OpenWrt setup script for a Nas Device.

	The shell templates live in hotspot_ms/scripts so the generated script and
	the one in the repo can never drift apart.
	"""
	doc = frappe.get_doc("Nas Device", name)
	nas_secret = (doc.get_password("shared_secret", raise_exception=False) or "").strip()
	fas_key = (doc.opennds_fas_key or "").strip()

	missing = [
		label
		for label, value in (
			("Shared Secret", nas_secret),
			("openNDS FAS Key", fas_key),
		)
		if not value
	]
	if missing:
		frappe.throw(f"Missing on Nas Device {name}: {', '.join(missing)}")

	portal_url = _resolve_portal_url(doc)
	parsed = urlsplit(portal_url)
	portal_host = parsed.hostname or ""
	portal_port = parsed.port or (443 if parsed.scheme == "https" else 80)
	if not portal_host:
		frappe.throw(f"Could not determine the portal host from portal_url: {portal_url}")

	lan_ip, cidr_bits = _split_ipv4_cidr(doc.ip_address or "192.168.10.1")
	gateway_port = cint(doc.opennds_gateway_port) or 2050
	maintenance = cint(frappe.db.get_single_value("Hotspot Settings", "enable_maintenance_mode"))

	setup = _read_script("openwrt_setup.sh")
	agent = _read_script("hotspot_agent.sh")

	# The guest LAN can sit on a dedicated NIC (eth1) or, on single-NIC boxes, on
	# the same wire as WAN. The script autodetects unless the operator pins it.
	lan_phy = (doc.lan_interface or "eth1").strip()

	# The provisioning host's SSH key, so ops keeps access after hardening.
	# Failure is non-fatal: shared hosting has no key to ship.
	ssh_pubkey = ""
	try:
		from pathlib import Path as _p
		ssh_pubkey = (_p("/root/.ssh/id_ed25519.pub").read_text().strip()
			or _p("/root/.ssh/id_rsa.pub").read_text().strip())
	except Exception:
		try:
			import os as _os
			ssh_pubkey = _os.path.expanduser("~/.ssh/id_ed25519.pub")
			ssh_pubkey = _p(ssh_pubkey).read_text().strip() if _p(ssh_pubkey).exists() else ""
		except Exception:
			ssh_pubkey = ""

	replacements = {
		"__WAN_IFACE__": "eth0",
		"__LAN_IFACE__": BRIDGE_NAME,
		"__LAN_PHY__": lan_phy,
		"__LAN_IP__": lan_ip,
		"__LAN_CIDR__": f"{lan_ip}/{cidr_bits}",
		"__LAN_NETMASK__": _netmask(cidr_bits),
		"__LAN_CIDR_BITS__": str(cidr_bits),
		"__DHCP_START__": "100",
		"__DHCP_LIMIT__": "150",
		"__PORTAL_URL__": portal_url,
		"__PORTAL_HOST__": portal_host,
		"__PORTAL_PORT__": str(portal_port),
		# Level 4 sends the same base64 payload as level 1 but over https.
		"__FAS_SECURE__": "4" if parsed.scheme == "https" else "1",
		"__NAS_IDENTIFIER__": doc.name,
		"__NAS_SECRET__": nas_secret,
		"__FAS_KEY__": fas_key,
		"__GATEWAY_PORT__": str(gateway_port),
		"__MAX_CLIENTS__": "140",
		"__SESSION_TIMEOUT__": str(max(_longest_plan_minutes(), 60)),
		"__AGENT_INTERVAL__": "20",
		"__AGENT_BODY__": agent,
		"__SSH_PUBKEY__": ssh_pubkey,
	}

	script = setup
	for token, value in replacements.items():
		script = script.replace(token, value)

	return {
		"script": script,
		"portal_url": portal_url,
		"nas": doc.name,
		"fas_key": fas_key,
		"shared_secret": nas_secret,
		"maintenance_mode": bool(maintenance),
	}


def write_router_setup_script(name: str) -> str:
	"""Write the rendered script to the site's private files and return its path."""
	rendered = render_router_setup_script(name)
	path = Path(frappe.get_site_path("private", "files")) / f"hotspot-setup-{frappe.scrub(name)}.sh"
	path.write_text(rendered["script"], encoding="utf-8")
	path.chmod(0o600)
	return str(path)


# ---------------------------------------------------------------------------
# diagnostics
# ---------------------------------------------------------------------------
def summary() -> dict[str, Any]:
	"""Everything an operator needs to see in one call."""
	plans = frappe.get_all(
		"Hotspot Plan",
		filters={"is_free": 1},
		fields=["name", "enabled", "requires_ad_view"],
		ignore_permissions=True,
	)
	nas_rows = frappe.get_all(
		"Nas Device",
		fields=[
			"name",
			"enabled",
			"ip_address",
			"lan_interface",
			"vpn_ip_address",
			"portal_url",
			"opennds_fas_key",
			"status",
			"last_heartbeat",
		],
		ignore_permissions=True,
	)

	snippe = frappe.get_single("Snippe Settings")
	branding = frappe.get_single("Hotspot Settings")

	return {
		"free_plans_enabled": [row.name for row in plans if cint(row.enabled)],
		"paid_plans": [
			row.name
			for row in frappe.get_all(
				"Hotspot Plan",
				filters={"enabled": 1, "is_free": 0},
				fields=["name"],
				ignore_permissions=True,
			)
		],
		"nas_devices": [
			{
				"name": row.name,
				"enabled": cint(row.enabled),
				"lan_ip": row.ip_address,
				"lan_interface": row.lan_interface,
				"ssh_host": row.vpn_ip_address,
				"portal_url": row.portal_url,
				"fas_key": "set" if (row.opennds_fas_key or "").strip() else "MISSING",
				"status": row.status,
				"last_heartbeat": row.last_heartbeat,
			}
			for row in nas_rows
		],
		"portal_brand": branding.portal_brand,
		"maintenance_mode": cint(branding.enable_maintenance_mode),
		"snippe": {
			"enabled": cint(snippe.enabled),
			"api_key": "set" if (snippe.get_password("api_key", raise_exception=False) or "") else "MISSING",
			"webhook_url": snippe.webhook_url or "MISSING",
			"auto_issue_voucher": cint(snippe.auto_issue_voucher),
		},
		"counts": {
			"vouchers": frappe.db.count("Hotspot Voucher"),
			"sessions": frappe.db.count("Hotspot Session"),
			"payments": frappe.db.count("Payment Transaction"),
		},
	}


def report() -> None:
	"""Print the summary in a readable form (bench execute friendly)."""
	import json

	print(json.dumps(summary(), indent=2, default=str))
