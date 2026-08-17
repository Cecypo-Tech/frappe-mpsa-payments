// Copyright (c) 2026, Cecypo.Tech and contributors
// For license information, please see license.txt

frappe.ui.form.on("Mpesa Statement Import", {
	refresh(frm) {
		show_result_indicator(frm);
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
		failed ? "red" : created ? "green" : "orange",
	);

	frm.page.set_indicator(
		__("{0} Created", [created]),
		failed ? "red" : created ? "green" : "orange",
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
