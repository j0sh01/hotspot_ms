import frappe


DEFAULT_PORTAL_BRAND = "KiliGrid Technology"
DEFAULT_ACCENT = "#1e3a8a"
DEFAULT_PORTAL_LOGO = "/assets/hotspot_ms/images/kiligrid-mark.png"


def _sanitize_hex_color(value: str | None) -> str:
	"""Only allow a plain hex colour, since it is injected into a style block."""
	text = (value or "").strip()
	if len(text) in (4, 7) and text.startswith("#"):
		if all(ch in "0123456789abcdefABCDEF" for ch in text[1:]):
			return text
	return DEFAULT_ACCENT


def _sanitize_phone_for_tel(value: str | None) -> str:
	if not value:
		return ""

	value = value.strip()
	if not value:
		return ""

	allowed = []
	for ch in value:
		if ch.isdigit() or ch == "+":
			allowed.append(ch)

	sanitized = "".join(allowed)
	return sanitized if any(ch.isdigit() for ch in sanitized) else ""


def _single_value(fieldname: str):
	"""Read one Hotspot Settings value, tolerating a not-yet-migrated site."""
	try:
		return frappe.db.get_single_value("Hotspot Settings", fieldname)
	except Exception:
		return None


def get_portal_branding() -> dict[str, str]:
	brand = DEFAULT_PORTAL_BRAND
	accent = DEFAULT_ACCENT
	logo_url = DEFAULT_PORTAL_LOGO
	customer_support_number = ""
	enable_maintenance_mode = 0
	maintenance_message = ""

	configured_brand = _single_value("portal_brand")
	if configured_brand:
		brand = configured_brand.strip() or DEFAULT_PORTAL_BRAND

	accent = _sanitize_hex_color(_single_value("portal_accent_color"))

	configured_logo = _single_value("portal_logo")
	if configured_logo:
		logo_url = configured_logo.strip() or DEFAULT_PORTAL_LOGO

	configured_support = _single_value("customer_support_number")
	if configured_support:
		customer_support_number = configured_support.strip()

	enable_maintenance_mode = _single_value("enable_maintenance_mode") or 0
	maintenance_message = _single_value("maintenance_message") or ""

	return {
		"portal_brand": brand,
		"portal_logo_url": logo_url,
		"portal_accent_color": accent,
		"portal_login_title": f"{brand} Login",
		"portal_status_title": f"Session Status | {brand}",
		"customer_support_number": customer_support_number,
		"customer_support_tel": _sanitize_phone_for_tel(customer_support_number),
		"enable_maintenance_mode": enable_maintenance_mode,
		"maintenance_message": maintenance_message,
	}
