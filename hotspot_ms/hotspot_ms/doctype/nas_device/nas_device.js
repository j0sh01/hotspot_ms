// Copyright (c) 2026, Sydney Kibanga and contributors
// For license information, please see license.txt

frappe.ui.form.on("Nas Device", {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.add_custom_button(__("Generate FAS Key"), async () => {
			const result = await frappe.call({
				method: "hotspot_ms.api.portal.generate_opennds_fas_key",
				args: { name: frm.doc.name },
			});

			const key = result.message && result.message.opennds_fas_key;
			if (!key) {
				frappe.msgprint(__("Could not generate FAS key."));
				return;
			}

			await frm.reload_doc();
			frappe.msgprint({
				title: __("FAS Key Generated"),
				indicator: "green",
				message: __(
					"The openNDS FAS key was generated and saved. Re-run the router setup script to apply it."
				),
			});
		});

		frm.add_custom_button(__("Router Setup Script"), async () => {
			if (frm.is_dirty()) {
				frappe.msgprint(__("Save the document first."));
				return;
			}

			const result = await frappe.call({
				method: "hotspot_ms.hotspot_ms.doctype.nas_device.nas_device.get_provisioning_script",
				args: { name: frm.doc.name },
			});

			const script = result.message && result.message.script;
			if (!script) {
				frappe.msgprint(__("Could not render the setup script."));
				return;
			}

			const portalUrl = (result.message.portal_url || "").trim();
			const dialog = new frappe.ui.Dialog({
				title: __("Router Setup Script"),
				fields: [
					{
						fieldtype: "HTML",
						options: `<p class="text-muted">${__(
							"Paste this into a root shell on the router. It is idempotent, and it refuses to run if the guest LAN interface is missing."
						)}</p>
						<p class="text-muted">${__("Portal URL")}: <b>${frappe.utils.escape_html(
							portalUrl || __("not set")
						)}</b></p>`,
					},
					{
						fieldname: "script",
						fieldtype: "Code",
						options: "Shell",
						read_only: 1,
						default: script,
					},
				],
				size: "extra-large",
				primary_action_label: __("Copy Script"),
				primary_action() {
					frappe.utils.copy_to_clipboard(script);
					dialog.hide();
					frappe.show_alert({ message: __("Copied to clipboard"), indicator: "green" });
				},
			});

			dialog.show();
		});
	},
});
