/* global mpesa_qp */
frappe.provide("mpesa_qp");
/* global pos_qp */
frappe.provide("pos_qp");
/* global cur_pos */

pos_qp.pos_doctype = null;
pos_qp.confirmed_selections = new Map(); // mop -> { mop, account, shortcode, payments[] }
pos_qp._last_active_mop = null;
pos_qp.main_dialog = null; // Store reference to main Mpesa C2B dialog

for (const doctype of ["Sales Invoice", "POS Invoice"]) {
	frappe.ui.form.on(doctype, {
		refresh(frm) {
			const company = frm.doc?.company;
			if (!company) return;

			// Listen for successful Mpesa payment to close dialogs
			frappe.realtime.on("mpesa_stk_payment_completed", function (data) {
				if (
					data.reference_doctype === frm.doctype &&
					data.reference_name === frm.doc.name
				) {
					// Close any open dialogs to allow user to complete order
					close_all_mpesa_dialogs();
					clear_mpesa_state();
				}
			});

			_resolve_doctype((resolved_doctype) => {
				const pos_profile = frm.doc?.pos_profile || null;

				frappe.call({
					method: "frappe_mpsa_payments.frappe_mpsa_payments.api.mpesa_quick_pay.get_mpesa_phone_mops_for_pos_profile",
					args: {
						company,
						pos_profile,
					},
					callback(r) {
						if (r.message && r.message.length > 0) {
							pos_qp.mpesa_mops = r.message || []; // { mop, account, shortcode }
							add_button(frm);
						}
					},
				});
			});

			refresh_selected_summary();
		},

		before_submit(frm) {
			if (!pos_qp.confirmed_selections.size) return;
			console.log("Final confirmed selections before submit:", pos_qp.confirmed_selections);
			console.log(pos_qp.confirmed_selections.size);

			// Reload the document to sync with any external changes (e.g., from payment callback)
			frappe.call({
				method: "frappe.client.get",
				args: {
					doctype: frm.doctype,
					name: frm.docname,
				},
				async: false,
				callback(r) {
					if (r.message) {
						// Update form with latest state
						frm.doc = r.message;
						frm.refresh();
						// Update dirty flag to match actual state
						frm.dirty();
					}
				},
			});

			// For each MOP group, add payment rows
			for (const [mop, group] of pos_qp.confirmed_selections) {
				const total = group.payments.reduce((s, p) => s + flt(p.amount), 0);

				// Remove existing Phone rows for this specific MOP to avoid duplicates
				(frm.doc.payments || [])
					.filter((row) => row.mode_of_payment === mop && row.type === "Phone")
					.forEach((row) => {
						frm.get_field("payments").grid.grid_rows_by_docname[row.name]?.remove();
					});

				if (group.merge && group.payments.length > 1) {
					const row = frm.add_child("payments");
					row.mode_of_payment = mop;
					row.type = "Phone";
					row.amount = total;
					row.account = group.account;
					row.custom_reference_text = group.payments.map((p) => p.name).join("\n");
				} else {
					for (const p of group.payments) {
						const row = frm.add_child("payments");
						row.mode_of_payment = mop;
						row.type = "Phone";
						row.account = group.account;
						row.amount = flt(p.amount);
						row.reference_no = p.name;
					}
				}
			}

			frm.refresh_field("payments");
		},

		on_submit(frm) {
			if (!pos_qp.confirmed_selections.size) return;

			const all_payments = [];
			for (const [, group] of pos_qp.confirmed_selections) {
				group.payments.forEach((p) => all_payments.push(p.name));
			}
			if (!all_payments.length) return;

			frappe.call({
				method: "frappe_mpsa_payments.frappe_mpsa_payments.api.mpesa_quick_pay.update_mpesa_after_invoice_submission",
				args: {
					doctype: frm.doctype,
					invoice_name: frm.doc.name,
					customer: frm.doc.customer,
					mpesa_payments: all_payments.join(","),
				},
				callback(r) {
					if (!r.message?.success) {
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
	if (!$payment_container.length) return;
	if ($(".mpesa-quick-pay-section").length) return;

	$payment_container.prepend(`
		<div class="mpesa-quick-pay-section">
			<button class="btn btn-success btn-sm mpesa-quick-pay-btn w-100">
				<i class="fa fa-mobile"></i> ${__("Mpesa C2B")}
			</button>
			<div id="mpesa-selected-summary"></div>
		</div>`);

	$(".mpesa-quick-pay-section").on("click", (e) => {
		e.stopPropagation();
	});

	$(".mpesa-quick-pay-btn").on("mousedown", () => {
		pos_qp._last_active_mop = get_selected_phone_mop();
	});

	$(".mpesa-quick-pay-btn").on("click", () => handle_button_click(frm));

	mpesa_qp.inject_btn_styles();
	refresh_selected_summary();
}

function handle_button_click(frm) {
	const selected_mop_info = pos_qp._last_active_mop || get_selected_phone_mop();
	pos_qp._last_active_mop = null;

	if (!selected_mop_info) {
		frappe.msgprint({
			title: __("No Mpesa Payment Mode Selected"),
			message: __(
				"Please select an Mpesa payment method in the payment section before using Mpesa C2B."
			),
			indicator: "orange",
		});
		return;
	}

	const mpesa_mops = pos_qp.mpesa_mops || [];
	const mop_config = mpesa_mops.find((m) => m.mop === selected_mop_info.mop_name);

	if (!mop_config) {
		frappe.msgprint({
			title: __("Payment Mode Not Mpesa"),
			message: __(
				`The selected payment mode "${selected_mop_info.mop_name}" is not configured for Mpesa. Please select an Mpesa-enabled payment mode.`
			),
			indicator: "red",
		});
		return;
	}

	// Gather any previously confirmed payments for this specific MOP
	const existing_group = pos_qp.confirmed_selections.get(mop_config.mop);
	const initial_selections = existing_group ? [...existing_group.payments] : [];

	const outstanding =
		selected_mop_info.amount || flt(selected_mop_info.control?.get_value() || 0);

	if (outstanding <= 0 && !initial_selections.length) {
		frappe.msgprint(__("No outstanding amount to pay for this payment mode."));
		return;
	}

	mpesa_qp.show_dialog(
		frm,
		"pos",
		(dialog_frm, dialog) => mpesa_process_payments(dialog_frm, dialog, mop_config),
		outstanding,
		{
			auto_save_invoice: false,
			auto_submit_invoice: false,
			merge_payments: true,
		},
		initial_selections,
		mop_config
	);
}

function get_selected_phone_mop() {
	const $modes = $(".mode-of-payment[data-payment-type='Phone']");
	if (!$modes.length) return null;

	const pos_payment = cur_pos?.payment;

	if (pos_payment?.selected_mode) {
		const selected_label = pos_payment.selected_mode.df?.label;
		let result = null;
		$modes.each(function () {
			const mode = $(this).data("mode");
			const control = pos_payment[`${mode}_control`];
			if (control?.df?.label === selected_label) {
				result = {
					mode,
					mop_name: selected_label,
					control,
					amount: flt(control.get_value() || 0),
					$el: $(this),
				};
				return false;
			}
		});
		if (result) return result;
	}

	const $active = $modes.filter(".border-primary");
	if (!$active.length) return null;

	const mode = $active.data("mode");
	const control = pos_payment?.[`${mode}_control`];
	const mop_name = control?.df?.label || mode;

	return {
		mode,
		mop_name,
		control,
		amount: flt(control?.get_value() || 0),
		$el: $active,
	};
}

function mpesa_process_payments(frm, dialog, mop_config) {
	if (!dialog.selected.length) {
		frappe.msgprint(__("Please select at least one Mpesa payment"));
		return;
	}

	const mop = mop_config.mop;
	const existing = pos_qp.confirmed_selections.get(mop) || {
		mop,
		account: mop_config.account,
		shortcode: mop_config.shortcode,
		payments: [],
		merge: dialog.merge_payments,
	};

	// Merge new selections (avoid duplicates)
	const existing_names = new Set(existing.payments.map((p) => p.name));
	for (const p of dialog.selected) {
		if (!existing_names.has(p.name)) {
			existing.payments.push(p);
		}
	}
	existing.merge = dialog.merge_payments;
	pos_qp.confirmed_selections.set(mop, existing);

	dialog.hide();

	const total = existing.payments.reduce((s, p) => s + flt(p.amount), 0);
	update_phone_mode_amount(mop, total);

	if (cur_pos?.payment) {
		cur_pos.payment.update_totals_section();
	}

	refresh_selected_summary();
}

function update_phone_mode_amount(mop_name, amount) {
	const $modes = $(".mode-of-payment[data-payment-type='Phone']");
	$modes.each(function () {
		const mode = $(this).data("mode");
		const control = cur_pos?.payment?.[`${mode}_control`];
		if (!control) return;

		if (control.df?.label === mop_name || mode === mop_name) {
			const currency = cur_pos?.frm?.doc?.currency || "";
			control.set_value(amount);
			$(this)
				.find(`.${mode}-amount`)
				.html(amount > 0 ? format_currency(amount, currency) : "");
			return false;
		}
	});
}

function refresh_selected_summary() {
	const $summary = $("#mpesa-selected-summary");
	if (!$summary.length) return;

	if (!pos_qp.confirmed_selections.size) {
		$summary.html("");
		return;
	}

	const currency = cur_pos?.frm?.doc?.currency || "";
	let grand_total = 0;
	let html = `<div class="mpesa-summary-panel">`;

	for (const [mop, group] of pos_qp.confirmed_selections) {
		if (!group.payments.length) continue;

		const group_total = group.payments.reduce((s, p) => s + flt(p.amount), 0);
		grand_total += group_total;

		html += `
			<div class="mpesa-summary-header">
				<span><i class="fa fa-check-circle text-success"></i> ${frappe.utils.escape_html(mop)}</span>
				<span class="mpesa-summary-total">${format_currency(group_total, currency)}</span>
			</div>
			<ul class="mpesa-summary-list">`;

		for (const p of group.payments) {
			html += `
				<li class="mpesa-summary-item" data-name="${p.name}" data-mop="${frappe.utils.escape_html(mop)}">
					<span class="mpesa-summary-name">${frappe.utils.escape_html(p.full_name || p.name)}</span>
					<span class="mpesa-summary-amt">${format_currency(p.amount, currency)}</span>
					<button class="mpesa-summary-remove" data-name="${p.name}" data-mop="${frappe.utils.escape_html(
				mop
			)}" title="${__("Remove")}">
						<i class="fa fa-times"></i>
					</button>
				</li>`;
		}

		html += `</ul>`;
	}

	html += `</div>`;
	$summary.html(html);

	$summary.find(".mpesa-summary-remove").on("click", function () {
		const name = $(this).data("name");
		const mop = $(this).data("mop");

		const group = pos_qp.confirmed_selections.get(mop);
		if (!group) return;

		group.payments = group.payments.filter((x) => x.name !== name);

		const remaining_total = group.payments.reduce((s, p) => s + flt(p.amount), 0);
		update_phone_mode_amount(mop, remaining_total);

		if (!group.payments.length) {
			pos_qp.confirmed_selections.delete(mop);
		}

		if (cur_pos?.payment) {
			cur_pos.payment.update_totals_section();
		}

		refresh_selected_summary();
	});
}

function clear_mpesa_state() {
	pos_qp.confirmed_selections = new Map();
	pos_qp.main_dialog = null;
	refresh_selected_summary();
}

function close_all_mpesa_dialogs() {
	// Close any visible Mpesa-related dialogs
	$(".frappe-control.modal").each(function () {
		const $el = $(this);
		const title = $el.find(".modal-header .modal-title").text();

		// Close if it's related to Mpesa payment dialogs
		if (title.includes("Mpesa") || title.includes("Payment") || title.includes("STK")) {
			const instance = $el.data("frappe_dialog") || $el.closest(".modal").data("instance");
			if (instance && typeof instance.hide === "function") {
				instance.hide();
			} else {
				// Fallback: just hide the modal
				$el.closest(".modal").modal("hide");
			}
		}
	});

	// Also try to close using Frappe's dialog registry if available
	if (frappe.ui && frappe.ui.form && frappe.ui.form._dialogs) {
		for (let dialog of frappe.ui.form._dialogs) {
			if (
				dialog?.title &&
				(dialog.title.includes("Mpesa") || dialog.title.includes("Payment"))
			) {
				dialog.hide?.();
			}
		}
	}
}
