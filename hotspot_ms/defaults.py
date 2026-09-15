from __future__ import annotations

import frappe

from hotspot_ms import provision


def ensure_default_hotspot_plans() -> dict[str, int]:
	"""Seed the starter plans. Thin wrapper kept for existing callers."""
	result = provision.ensure_plans()
	return {
		"created": len(result["created"]),
		"updated": len(result["disabled_free_plans"]),
		"total": len(provision.DEFAULT_PLANS),
	}


def after_migrate() -> None:
	"""Hook entry point - keeps a freshly installed site usable."""
	ensure_default_hotspot_plans()
	frappe.db.commit()
