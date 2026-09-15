from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import frappe
from frappe.utils import cint, flt, get_datetime, now_datetime

DEAUTH_PENDING_PREFIX = "DEAUTH_PENDING|"


def _to_mb(octets: float) -> float:
	return round((flt(octets) / 1024 / 1024), 2)


def close_expired_or_used_sessions() -> dict[str, int]:
	"""
	Close open sessions once the voucher expires or data quota is exhausted.
	Also updates voucher status to Expired/Used.
	"""
	now = now_datetime()
	closed = 0
	voucher_updates = 0

	open_sessions = frappe.get_all(
		"Hotspot Session",
		filters={"session_status": "Open"},
		fields=["name", "voucher", "input_octets", "output_octets", "total_mb"],
		ignore_permissions=True,
	)

	for row in open_sessions:
		if not row.voucher:
			continue

		voucher = frappe.get_doc("Hotspot Voucher", row.voucher)
		session_total_mb = flt(row.total_mb) or _to_mb(flt(row.input_octets) + flt(row.output_octets))

		is_slice_expired = False
		if voucher.current_slice_expires_on and get_datetime(voucher.current_slice_expires_on) <= now:
			is_slice_expired = True

		is_expired = bool(voucher.expires_on and get_datetime(voucher.expires_on) <= now) or is_slice_expired
		limit_mb = flt(voucher.data_limit_mb)
		data_used_mb = max(flt(voucher.data_used_mb), session_total_mb)
		is_used = bool(limit_mb > 0 and data_used_mb >= limit_mb)

		# Always update voucher data_used_mb in real-time when it increases
		if flt(voucher.data_used_mb) < data_used_mb:
			voucher.data_used_mb = data_used_mb
			voucher.save(ignore_permissions=True)
			voucher_updates += 1

		if not (is_expired or is_used):
			continue

		session = frappe.get_doc("Hotspot Session", row.name)
		session.session_status = "Expired" if is_expired else "Closed"
		session.stop_time = now
		session.total_mb = session_total_mb
		base_cause = "Free Slice Expired" if is_slice_expired else ("Session Expired" if is_expired else "Data Limit Reached")
		if session.ip_address or session.mac_address:
			session.terminate_cause = f"{DEAUTH_PENDING_PREFIX}{base_cause}"
		else:
			session.terminate_cause = base_cause
		session.save(ignore_permissions=True)
		closed += 1

		# Determine if the voucher itself should be expired
		should_expire_voucher = is_expired
		if is_slice_expired and voucher.plan:
			max_slices = cint(frappe.db.get_value("Hotspot Plan", voucher.plan, "max_slices_per_day")) or 4
			if cint(voucher.ad_slices_used) < max_slices:
				should_expire_voucher = False

		new_status = "Expired" if should_expire_voucher else ("Used" if is_used else voucher.status)
		if voucher.status != new_status:
			voucher.status = new_status
			voucher.save(ignore_permissions=True)

	if closed or voucher_updates:
		frappe.db.commit()

	return {"closed_sessions": closed, "updated_vouchers": voucher_updates}


def auto_activate_stuck_vouchers() -> dict[str, int]:
	"""
	Automatically activate 'New' vouchers that have a Successful Payment Transaction,
	but failed to auto-activate via webhook (usually because the user closed the captive portal browser).
	"""
	txs = frappe.get_all(
		"Payment Transaction",
		filters={"status": "Successful", "voucher": ["is", "set"]},
		fields=["name", "voucher", "webhook_payload"],
	)

	activated = 0
	for tx in txs:
		voucher = frappe.get_doc("Hotspot Voucher", tx.voucher)
		if voucher.status != "New":
			continue

		payload = {}
		if tx.webhook_payload:
			try:
				payload = json.loads(tx.webhook_payload)
			except Exception:
				pass

		data = payload.get("data", {})
		metadata = data.get("metadata", {})

		mac_address = metadata.get("mac_address")
		if not mac_address:
			continue

		try:
			from hotspot_ms.api.portal import activate_voucher

			res = activate_voucher(
				voucher_code=voucher.voucher_code,
				mac_address=mac_address,
				ip_address=metadata.get("ip_address"),
				nas_device=metadata.get("nas_device"),
			)
			if res.get("ok"):
				activated += 1
		except Exception:
			frappe.log_error(frappe.get_traceback(), "auto_activate_stuck_vouchers failed")

	return {"auto_activated_vouchers": activated}


def _safe_int(value: Any) -> int:
	try:
		return int(value)
	except (TypeError, ValueError):
		return 0


def _fetch_router_clients(router) -> tuple[str, dict[str, Any] | None, str]:
	"""SSH one router and return its openNDS client map.

	Runs off the main thread, so it must not touch frappe.db or frappe.local.
	Returns (router_name, clients, error).
	"""
	try:
		cmd = [
			"ssh",
			"-o",
			"StrictHostKeyChecking=no",
			"-o",
			"UserKnownHostsFile=/dev/null",
			"-o",
			"ConnectTimeout=10",
			f"root@{router.vpn_ip_address}",
			"ndsctl json",
		]
		res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
		if res.returncode != 0:
			return router.name, None, (res.stderr.decode("utf-8", "replace") or "SSH execution failed")

		payload = json.loads(res.stdout.decode("utf-8", "replace"))
		return router.name, (payload.get("clients") or {}), ""
	except Exception as exc:
		return router.name, None, str(exc)


def sync_all_routers_data() -> dict[str, int]:
	"""Refresh Hotspot Active Client rows for every reachable OpenWrt NAS.

	This used to SSH each router one after another inside the scheduler, so the
	run time grew linearly with the number of routers and one slow box delayed
	every other. SSH now runs in parallel; database writes stay on the main
	thread because frappe.db is not thread-safe.
	"""
	routers = frappe.get_all(
		"Nas Device",
		filters={"enabled": 1, "nas_type": "OpenWrt"},
		fields=["name", "vpn_ip_address"],
	)
	targets = [router for router in routers if (router.vpn_ip_address or "").strip()]
	if not targets:
		return {"routers": 0, "clients": 0}

	with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
		results = {name: (clients, error) for name, clients, error in pool.map(_fetch_router_clients, targets)}

	online = 0
	client_rows = 0
	now = frappe.utils.now()

	for router in targets:
		clients, error = results.get(router.name, (None, "no result"))
		if error:
			frappe.log_error(title=f"Failed to sync router {router.name}", message=error)
			frappe.db.set_value(
				"Nas Device",
				router.name,
				{"status": "Offline", "last_heartbeat": now},
				update_modified=False,
			)
			continue

		frappe.db.delete("Hotspot Active Client", {"nas_device": router.name})
		for mac, client in (clients or {}).items():
			# ndsctl json reports the state lowercase; compare tolerantly.
			if (client.get("state") or "").strip().lower() != "authenticated":
				continue
			frappe.get_doc(
				{
					"doctype": "Hotspot Active Client",
					"mac_address": mac,
					"ip_address": client.get("ip"),
					"nas_device": router.name,
					"download_bytes": _safe_int(client.get("download_this_session")),
					"upload_bytes": _safe_int(client.get("upload_this_session")),
					"connected_since": now,
				}
			).insert(ignore_permissions=True)
			client_rows += 1

		frappe.db.set_value(
			"Nas Device",
			router.name,
			{"status": "Online", "last_heartbeat": now},
			update_modified=False,
		)
		online += 1

	frappe.db.commit()
	return {"routers": online, "clients": client_rows}
