// Copyright (c) 2026, Cecypo.Tech and contributors
// For license information, please see license.txt

frappe.ui.form.on("Mpesa Statement Import", {
	refresh(frm) {
		show_result_indicator(frm);

		if (frm.doc.docstatus === 1) {
			frm.add_custom_button(__("Create Bank Transfer"), () => open_bank_transfer_dialog(frm));
		}
	},

	statement_file(frm) {
		// A shortcode read from the previous file is not one the user typed;
		// the new file supplies its own, or the user types it in.
		frm.set_value("business_shortcode", "");
	},

	on_submit(frm) {
		// The counts are written server-side with db_set during on_submit,
		// so pull the saved values back before showing them.
		frm.reload_doc().then(() => {
			show_result_indicator(frm);
			show_result_summary(frm);
		});
	},
});

function show_result_indicator(frm) {
	if (frm.doc.docstatus !== 1) {
		return;
	}

	const created = frm.doc.created_count || 0;
	const failed = frm.doc.failed_count || 0;

	frm.dashboard.clear_headline();
	frm.dashboard.set_headline_alert(
		__("{0} created &middot; {1} skipped &middot; {2} blocked &middot; {3} failed", [
			created,
			frm.doc.skipped_count || 0,
			frm.doc.blocked_count || 0,
			failed,
		]),
		failed ? "red" : created ? "green" : "orange"
	);

	frm.page.set_indicator(
		__("{0} Created", [created]),
		failed ? "red" : created ? "green" : "orange"
	);
}

function show_result_summary(frm) {
	const created = frm.doc.created_count || 0;
	const skipped = frm.doc.skipped_count || 0;
	const blocked = frm.doc.blocked_count || 0;
	const failed = frm.doc.failed_count || 0;

	frappe.msgprint({
		title: __("Import Complete"),
		indicator: failed ? "red" : "green",
		message: `
			<p style="font-size: 1.4em; margin-bottom: 12px;">
				<b>${created}</b> ${__("created")} &middot;
				<b>${skipped}</b> ${__("skipped")}
			</p>
			<p class="text-muted">
				${__("Blocked (already captured by an STK push)")}: <b>${blocked}</b><br>
				${__("Failed")}: <b>${failed}</b>
			</p>
			<p class="text-muted">${__("Full row-by-row detail is in the Import Summary below.")}</p>
		`,
	});
}

function open_bank_transfer_dialog(frm) {
	frm.call("get_bank_transfer_context").then(({ message: context }) => {
		const withdrawals = context.withdrawals || [];
		if (!withdrawals.length) {
			frappe.msgprint(__("No withdrawals to bank in this statement."));
			return;
		}

		const rows = withdrawals
			.map((w) => {
				const existing = w.payment_entry
					? frappe.utils.get_form_link("Payment Entry", w.payment_entry, true)
					: __("new draft");
				return `<tr>
					<td>${frappe.utils.escape_html(w.receipt_no)}</td>
					<td>${frappe.utils.escape_html(w.completion_time || "")}</td>
					<td class="text-right">${format_currency(w.amount, "KES")}</td>
					<td>${existing}</td>
				</tr>`;
			})
			.join("");

		const dialog = new frappe.ui.Dialog({
			title: __("Create Bank Transfer"),
			fields: [
				{
					fieldtype: "HTML",
					options: `<p class="text-muted">${__(
						"A draft Internal Transfer from {0} is made for each withdrawal below. Check it, then submit it from the Payment Entry.",
						[frappe.utils.escape_html(context.paid_from)]
					)}</p>
					<table class="table table-bordered table-sm">
						<thead><tr><th>${__("Receipt")}</th><th>${__("Time")}</th><th>${__(
						"Amount"
					)}</th><th>${__("Payment Entry")}</th></tr></thead>
						<tbody>${rows}</tbody>
					</table>`,
				},
				{
					fieldname: "bank_account",
					fieldtype: "Link",
					label: __("Bank Account"),
					options: "Account",
					reqd: 1,
					get_query: () => ({
						filters: {
							company: context.company,
							account_type: "Bank",
							is_group: 0,
							name: ["!=", context.paid_from],
						},
					}),
				},
			],
			primary_action_label: __("Create Draft"),
			primary_action({ bank_account }) {
				dialog.get_primary_btn().prop("disabled", true);
				frm.call("create_bank_transfer", { bank_account })
					.always(() => dialog.get_primary_btn().prop("disabled", false))
					.then(({ message }) => {
					dialog.hide();
					const entries = message || [];
					if (entries.length === 1) {
						frappe.set_route("Form", "Payment Entry", entries[0].name);
						return;
					}
					frappe.msgprint(
						entries
							.map(
								(e) =>
									`${frappe.utils.get_form_link("Payment Entry", e.name, true)} ${
										e.created ? __("created") : __("already existed")
									}`
							)
							.join("<br>")
					);
				});
			},
		});
		dialog.show();
	});
}
