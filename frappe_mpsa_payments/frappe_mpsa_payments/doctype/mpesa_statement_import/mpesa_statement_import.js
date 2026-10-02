// Copyright (c) 2026, Cecypo.Tech and contributors
// For license information, please see license.txt

frappe.ui.form.on("Mpesa Statement Import", {
	refresh(frm) {
		show_result_indicator(frm);

		// Submitting creates the payments, so it says so and asks first,
		// instead of Frappe's generic "Permanently Submit?".
		if (
			frm.doc.docstatus === 0 &&
			!frm.is_new() &&
			!frm.is_dirty() &&
			frm.perm[0]?.submit &&
			!frappe.model.has_workflow(frm.doctype)
		) {
			frm.page.set_primary_action(__("Import Payments"), () => confirm_import(frm));
		}

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

function confirm_import(frm) {
	// Frozen while it loads, so a double click cannot open two dialogs.
	frm.call({ method: "get_import_preview", doc: frm.doc, freeze: true }).then(
		({ message: preview }) => show_import_dialog(frm, preview)
	);
}

function show_import_dialog(frm, preview) {
	const esc = (value) => frappe.utils.escape_html(String(value ?? ""));
	const rows = [
		[__("Shortcode"), esc(preview.business_shortcode)],
		[__("Company"), esc(preview.company)],
		[__("Mode of Payment"), esc(preview.mode_of_payment)],
		[__("Period"), esc(preview.statement_period)],
		[
			__("New payments"),
			`<b>${esc(preview.new_count)}</b> &middot; ${format_currency(
				preview.new_total,
				"KES"
			)}`,
		],
		[__("Already imported (skipped)"), esc(preview.already_present)],
		[__("Captured by an STK push (skipped)"), esc(preview.stk_captured)],
		[__("Withdrawals to bank"), esc(preview.bank_withdrawal_count)],
		[__("Not imported"), esc(preview.ignored_count)],
	]
		.map(([label, value]) => `<tr><th>${label}</th><td>${value}</td></tr>`)
		.join("");

	const posting = preview.auto_reconcile
		? `<p class="text-warning">${__(
				"Auto-reconcile is on for {0}: each new payment posts a Payment Entry as soon as it is created and its customer is matched.",
				[esc(preview.business_shortcode)]
		  )}</p>`
		: `<p class="text-muted">${__(
				"Auto-reconcile is off for {0}: the payments are created as drafts in the Mpesa C2B Payment Register.",
				[esc(preview.business_shortcode)]
		  )}</p>`;

	const dialog = new frappe.ui.Dialog({
		title: __("Import Payments?"),
		fields: [
			{
				fieldtype: "HTML",
				options: `<table class="table table-bordered table-sm">${rows}</table>${posting}`,
			},
		],
		primary_action_label: preview.new_count
			? __("Create {0} Payments", [preview.new_count])
			: __("Submit Import"),
		primary_action() {
			dialog.hide();
			// frm.save runs the submit; savesubmit would ask a second time.
			frm.save("Submit", (r) => {
				if (!r.exc) {
					frm.script_manager.trigger("on_submit");
				}
			});
		},
		secondary_action_label: __("Cancel"),
		secondary_action() {
			dialog.hide();
		},
	});
	dialog.show();
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
