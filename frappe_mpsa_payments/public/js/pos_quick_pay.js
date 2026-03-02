frappe.provide("mpesa_qp");
frappe.provide("pos_qp");

pos_qp.pos_doctype = null;
pos_qp.mpesa_references = new Set();
pos_qp.confirmed_selections = [];

for (const doctype of ["Sales Invoice", "POS Invoice"]) {
	frappe.ui.form.on(doctype, {
		refresh(frm) {
			const company = frm.doc?.company;
			if (!company) return;

			_resolve_doctype((doctype) => {
				frappe.call({
					method: "frappe_mpsa_payments.frappe_mpsa_payments.api.mpesa_quick_pay.pos_quick_pay_mpesa_process",
					args: { action: "check_mpesa_available", doctype, company },
					callback(r) {
						if (r.message?.available) {
							pos_qp.phone_mop_name = r.message.phone_mop;
							pos_qp.phone_mop_account = r.message.phone_mop_account;
							add_button(frm);
						}
					},
				});
			});
			refresh_selected_summary();
		},
		before_submit(frm) {
			// before submitting, add the queued Mpesa payments to the document
			if (!pos_qp.confirmed_selections?.length) return;

			const selections = pos_qp.confirmed_selections;
			const mop_name = get_phone_mop_name();
			const total = selections.reduce((s, p) => s + flt(p.amount), 0);

			// Remove existing Phone rows safely
			(frm.doc.payments || [])
				.filter((row) => row.type === "Phone")
				.forEach((row) => {
					frm.get_field("payments").grid.grid_rows_by_docname[row.name].remove();
				});

			if (pos_qp.merge_payments && selections.length > 1) {
				const row = frm.add_child("payments");
				row.mode_of_payment = mop_name;
				row.type = "Phone";
				row.amount = total;
				row.account = pos_qp.phone_mop_account;
				row.custom_reference_text = selections.map((p) => p.name).join("\n");
			} else {
				for (const p of selections) {
					const row = frm.add_child("payments");
					row.mode_of_payment = mop_name;
					row.type = "Phone";
					row.account = pos_qp.phone_mop_account;
					row.amount = flt(p.amount);
					row.reference_no = p.name;
				}
			}

			frm.refresh_field("payments");
		},
		on_submit(frm) {
			// after submitting update the linked mpesa payments
			if (!pos_qp.confirmed_selections.length) return;

			frappe.call({
				method: "frappe_mpsa_payments.frappe_mpsa_payments.api.mpesa_quick_pay.pos_quick_pay_mpesa_process",
				args: {
					action: "update_mpesa_after_submit",
					doctype: frm.doctype,
					invoice_name: frm.doc.name,
					customer: frm.doc.customer,
					mpesa_payments: pos_qp.confirmed_selections.map((p) => p.name).join(","),
				},
				callback(r) {
					if (r.message?.success) {
						console.log("Mpesa payments updated successfully after submit");
					} else {
						frappe.msgprint({
							title: __("Warning"),
							message: __(
								`Mpesa payments were added to the ${frm.doctype} ${frm.doc.name}, but we couldn't finalize them after submission. Please check the linked payments.`
							),
							indicator: "orange",
						});
					}
					clear_mpesa_state();
				},
			});
		},
	});
}

// get the POS doctype based on POS settings
function _resolve_doctype(callback) {
	if (pos_qp.pos_doctype) {
		callback(pos_qp.pos_doctype);
		return;
	}

	frappe.model.with_doctype("POS Settings", () => {
		const meta = frappe.get_meta("POS Settings");
		const has_invoice_type = meta.fields.some((f) => f.fieldname === "invoice_type");

		if (!has_invoice_type) {
			pos_qp.pos_doctype = "POS Invoice";
			callback(pos_qp.pos_doctype);
			return;
		}

		frappe.db
			.get_single_value("POS Settings", "invoice_type")
			.then((value) => {
				pos_qp.pos_doctype = value === "POS Invoice" ? "POS Invoice" : "Sales Invoice";
				callback(pos_qp.pos_doctype);
			})
			.catch(() => {
				console.error("Defaulting to POS Invoice for backward compatibility");
				pos_qp.pos_doctype = "POS Invoice";
				callback(pos_qp.pos_doctype);
			});
	});
}

function add_button(frm) {
	const $payment_container = $(".payment-container");

	if ($payment_container.length) {
		if ($(".mpesa-quick-pay-section").length) return;

		$payment_container.prepend(`
			<div class="mpesa-quick-pay-section">
				<button class="btn btn-success btn-sm mpesa-quick-pay-btn w-100">
					<i class="fa fa-mobile"></i> ${__("Quick Pay - Mpesa")}
				</button>
				<div id="mpesa-selected-summary"></div>
			</div>`);

		// $(".mpesa-quick-pay-btn").on("click", () => show_dialog(frm));
		$(".mpesa-quick-pay-btn").on("click", () => {
			const outstanding = get_outstanding();
			console.log("Outstanding amount for quick pay:", outstanding);
			mpesa_qp.show_dialog(
				frm,
				"pos",
				mpesa_process_payments,
				outstanding,
				{
					auto_save_invoice: false,
					auto_submit_invoice: false,
					merge_payments: true,
				},
				pos_qp.confirmed_selections
			);
		});
		mpesa_qp.inject_btn_styles();
		refresh_selected_summary();
	}
}

function refresh_selected_summary() {
	const $summary = $("#mpesa-selected-summary");
	if (!$summary.length) return;

	const selections = pos_qp.confirmed_selections;
	if (!selections.length) {
		$summary.html("");
		return;
	}

	const currency = cur_pos?.frm?.doc?.currency || "";
	const total = selections.reduce((s, p) => s + flt(p.amount), 0);

	let html = `
        <div class="mpesa-summary-panel">
            <div class="mpesa-summary-header">
                <span><i class="fa fa-check-circle text-success"></i> ${__(
					"Mpesa Payments Queued"
				)}</span>
                <span class="mpesa-summary-total">${format_currency(total, currency)}</span>
            </div>
            <ul class="mpesa-summary-list">`;

	for (const p of selections) {
		html += `
            <li class="mpesa-summary-item" data-name="${p.name}">
                <span class="mpesa-summary-name">${frappe.utils.escape_html(
					p.full_name || p.name
				)}</span>
                <span class="mpesa-summary-amt">${format_currency(p.amount, currency)}</span>
                <button class="mpesa-summary-remove" data-name="${p.name}" title="${__("Remove")}">
                    <i class="fa fa-times"></i>
                </button>
            </li>`;
	}

	html += `</ul></div>`;
	$summary.html(html);

	$summary.find(".mpesa-summary-remove").on("click", function () {
		const name = $(this).data("name");

		pos_qp.confirmed_selections = pos_qp.confirmed_selections.filter((x) => x.name !== name);
		pos_qp.mpesa_references.delete(name);

		const remaining_total = pos_qp.confirmed_selections.reduce((s, p) => s + flt(p.amount), 0);
		update_phone_mode_amount(remaining_total);

		// refresh the totals section to reflect the change
		if (cur_pos?.payment) {
			cur_pos.payment.update_totals_section();
		}

		refresh_selected_summary();
	});
}

// function mpesa_process_payments(dialog) {
function mpesa_process_payments(_, dialog, _) {
	if (!dialog.selected.length) {
		frappe.msgprint(__("Please select at least one Mpesa payment"));
		return;
	}

	pos_qp.confirmed_selections = [...dialog.selected];
	pos_qp.merge_payments = dialog.merge_payments;
	dialog.selected.forEach((p) => pos_qp.mpesa_references.add(p.name));

	dialog.hide();

	// Update the phone mode control to reflect queued total
	const total = pos_qp.confirmed_selections.reduce((s, p) => s + flt(p.amount), 0);
	update_phone_mode_amount(total);

	if (cur_pos?.payment) {
		cur_pos.payment.update_totals_section();
	}

	refresh_selected_summary();
}

function get_phone_mop_name() {
	const $phone_mode = $(".mode-of-payment[data-payment-type='Phone']");
	if (!$phone_mode.length) return "";

	const mode = $phone_mode.data("mode");
	const control = cur_pos?.payment?.[`${mode}_control`];

	return control?.df?.label || "";
}

function update_phone_mode_amount(amount) {
	const $phone_mode = $(".mode-of-payment[data-payment-type='Phone']");
	if (!$phone_mode.length) return;

	const mode = $phone_mode.data("mode");
	const control = cur_pos?.payment?.[`${mode}_control`];
	if (!control) return;

	const currency = cur_pos?.frm?.doc?.currency || "";
	control.set_value(amount);
	$phone_mode.find(`.${mode}-amount`).html(amount > 0 ? format_currency(amount, currency) : "");
}

function clear_mpesa_state() {
	pos_qp.confirmed_selections = [];
	pos_qp.mpesa_references = new Set();
	refresh_selected_summary();
}

function get_outstanding() {
	const $phone_mode = $(".mode-of-payment[data-payment-type='Phone']");

	if (!$phone_mode.length) {
		frappe.msgprint({
			title: __("No Mpesa Payment Mode"),
			message: __("Please select the Mpesa payment method and enter the amount first."),
			indicator: "orange",
		});
		return 0;
	}

	const mode = $phone_mode.data("mode");
	const control = cur_pos?.payment?.[`${mode}_control`];

	return flt(control?.get_value() || 0);
}
