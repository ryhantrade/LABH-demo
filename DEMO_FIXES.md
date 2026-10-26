# Demo-only fixes (2026-10-26, after first demo review)

These fixes live ONLY in this demo repo (`app.py` + `demo.db` here).
They are NOT in the real app. When the demo is approved, port them
into the real app — that is a separate step, on the user's word.

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
