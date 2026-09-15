# Copyright (c) 2026, Sydney Kibanga and contributors
# For license information, please see license.txt

import hmac
import secrets
import string
from urllib.parse import quote

import frappe
from frappe.model.document import Document

from hotspot_ms import provision


class NasDevice(Document):
	def validate(self):
		if not self.shared_secret:
			alphabet = string.ascii_letters + string.digits
			self.shared_secret = "".join(secrets.choice(alphabet) for _ in range(24))


def _get_doc_secret(doc) -> str:
	try:
		secret = doc.get_password("shared_secret", raise_exception=False)
		if secret:
			return secret
	except Exception:
		pass
	return (doc.get("shared_secret") or "").strip()


def _router_reachable_url(doc) -> str:
	"""Prefer the operator-set portal URL: the router must be able to reach it."""
	return ((doc.get("portal_url") or "").strip() or frappe.utils.get_url()).rstrip("/")


@frappe.whitelist()
def get_provisioning_script(name: str) -> dict:
	"""Return the full router setup script so it can be reviewed before running."""
	return {"ok": True, **provision.render_router_setup_script(name)}


@frappe.whitelist()
def get_provisioning_command(name: str) -> dict:
	"""One-liner that downloads the setup script and pipes it into sh on the router."""
	doc = frappe.get_doc("Nas Device", name)
	secret = _get_doc_secret(doc)
	if not secret:
		frappe.throw("This Nas Device has no Shared Secret yet. Save it first.")

	cmd = (
		f'wget --no-check-certificate -qO- "{_router_reachable_url(doc)}/api/method/'
		"hotspot_ms.hotspot_ms.doctype.nas_device.nas_device.download_provisioning_script"
		f'?name={quote(doc.name)}&secret={quote(secret)}" | sh'
	)
	return {"ok": True, "command": cmd}


@frappe.whitelist(allow_guest=True)
def download_provisioning_script(name: str, secret: str | None = None):
	"""Serve the setup script to a router that proves it knows the shared secret.

	The script embeds the shared secret and the FAS key, so the secret is
	mandatory - an earlier version served the script to anyone who knew the
	device name.
	"""
	if not frappe.db.exists("Nas Device", name):
		frappe.local.response["http_status_code"] = 404
		return "NAS Device Not Found"

	doc = frappe.get_doc("Nas Device", name)
	expected = _get_doc_secret(doc)
	if not expected:
		frappe.local.response["http_status_code"] = 409
		return "NAS Device has no shared secret configured"
	if not secret or not hmac.compare_digest(secret, expected):
		frappe.local.response["http_status_code"] = 403
		return "Unauthorized"

	frappe.response["type"] = "text"
	frappe.response["content_type"] = "text/plain"
	return provision.render_router_setup_script(name).get("script", "")
