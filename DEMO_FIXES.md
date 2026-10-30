# Demo-only fixes (2026-10-26, after first demo review)

These fixes live ONLY in this demo repo (`app.py` + `demo.db` here).
They are NOT in the real app. When the demo is approved, port them
into the real app — that is a separate step, on the user's word.

## Phase 2 (2026-10-30, master spec Phase 2 items — demo only)

1. **Per-action audit log.** New `audit_log` table (business_id,
   user_email, action, entity, entity_id, detail, created_at).
   Every create/update/delete/payment/return/cancel/adjust/import
   writes actor + timestamp + action + entity. Surfaced in the
   User Activity report as an "Action log" table (searchable,
   paginated) below the existing sessions table — the sessions
   table is kept as-is.
2. **Shared pagination.** New `paginate()` helper: 10/25/50/100/All
   page-size selector, Prev/Next + page numbers, preserves filters.
   Applied to: sales, products, inventory (+ movements), purchases,
   customers, suppliers, payments, expenses lists, and the audit log.
3. **Cash/bank accounts + opening balance.** New `accounts` table
   (name, type Cash/Bank/Mobile, opening_balance, active). New
   `/accounts` page lists accounts with live balances
   (opening + linked customer/sale payments in − supplier/purchase
   payments out − linked expenses). Payment and expense forms have
   an account dropdown; `account_id` stored on both. A default
   "Cash" account is auto-created.
4. **Bangla coverage.** `BN` dictionary expanded from 22 to ~180
   entries (nav, buttons, form labels, table headers, statuses,
   pager, Phase 2 pages). `table()` headers and `field()` labels
   now auto-translate via `tr()`; pager strings translated. The
   Settings language toggle covers all main pages.
5. **Import preview/validation.** CSV upload/paste now stages to
   `import_batches` and redirects to `/import/preview`, which shows
   every row with OK/Warning/Error status and reasons (bad numbers,
   duplicate SKU/barcode/IMEI in file or DB, unknown product).
   Confirm imports only OK + warning rows; error rows skipped.
   Summary shown; batch deleted after commit.
6. **Real Excel export.** Stdlib-only OOXML writer (`xlsx_bytes`):
   genuine `.xlsx` with green header row, borders, column widths,
   autofilter. Added beside CSV on: sales list, invoices list,
   sales report, purchase report. CSV + Print-to-PDF unchanged.
7. **Duplicate-submission fix.** One-time form tokens
   (`form_tokens` table, 6h expiry) on sale, purchase, payment and
   expense forms. Replaying a token shows "duplicate blocked"
   instead of creating a second record.
8. **Product detail verified.** Open/edit/save flow tested
   end-to-end (price change persisted, price_history row written,
   audit entry written).
9. **Admin Panel.** New `/admin` page (owner/admin only): role /
   permission matrix (Owner/Admin always full; staff column
   tick-boxes saved to `perm_matrix` setting, honored by
   `can_do()`), plus user list with role reassignment (cannot
   change own role). Added to nav.

Deliberately NOT built (per standing rules):
- Purchase Edit (accounting-sensitive, awaits user).
- Purchase-return double-return fix (asked twice, no answer).
- The 5 held spec conflicts (8-report cut, subscription/paywall,
  dashboard Cost card, Firestore, session CSRF) — untouched.

## Phase 1 (2026-10-27)

1. **Mobile side nav (drawer) did not open.** On narrow screens the
   base stylesheet hides the sidebar (`display:none`) while the
   drawer rules only moved it (`left:-280px` → `left:0`) without
   ever restoring display — tapping the hamburger only darkened
   the screen via the overlay. Fix: `.sidebar.open{display:block}`
   inside the mobile media query. Desktop sidebar was already fine
   (all 22 links verified navigating).
2. **"Profit by customer" showed no profit.** It listed Sales/Paid/
   Due per customer. Fixed to Revenue/COGS/Profit per customer
   (same computation as Profit by category/product/brand),
   Walk-in for sales without a customer.
3. **"Returns" renamed to "Purchase returns"** (page title too) —
   the report only ever contained purchase returns.
4. **Doubled period-sales titles fixed.** "Sales Daily sales" →
   "Daily sales" (same for Weekly/Monthly).
5. **Cash-flow wording on the Reports index fixed** — "Cash in
   (sales paid)" to match the dedicated Cash flow page (payments
   recorded in /payments are already reflected in sales paid, not
   added again).
6. **Expense category hint fixed** — "Utilities/Other" →
   "Utility/Others" to match the dashboard donut buckets.
7. **Reports index grouped by category** — the 26 report chips
   were a flat ungrouped list; now grouped under Sales, Inventory,
   Customers & suppliers, Purchases, Profit, Finance, Other.
8. **Demo seed brands added** — demo phones now carry brands
   (Infinix/Samsung/Xiaomi), so Profit by brand and the Brands
   page show a real breakdown instead of only "(no brand)".

Deliberately NOT changed (same data, different grouping — not bugs):
Customer dues vs Receivables, Supplier dues vs Payables,
Inventory vs Stock valuation, IMEI register scope, Stock movement
only counting manual movements, User activity session scope.
