# UI audit: demo vs labhsbm.ai.studio (2026-10-26)

Reference: https://labhsbm.ai.studio (signed in as Rayhan Ahmed).
Demo: https://labh-demo.onrender.com (demo data). Visual audit only;
colors are observations, not measured CSS.

## Shared identity (already matching)

Pale mint-grey page background, white rounded cards, LABH green
primary buttons, left sidebar grouped MAIN / INVENTORY / PURCHASE /
FINANCE / REPORTS, light-mint active sidebar item with green text
and left green bar, sans-serif type, bold black headings.

## Design pass applied on the demo (this file's app.py)

- Money symbol: `BDT 22,499.00` → `৳22,499.00` everywhere
  (`money()` maps BDT to ৳; other currencies keep their code).
- Dates: raw ISO timestamps (`2026-10-26T10:00:00`) in Recent
  Transactions and Notifications → `26 Oct 2026, 10:00 AM`
  (`fmt_dt()` helper).
- Inputs: light-grey filled, rounded 10px (reference style) instead
  of white thin-bordered, scoped to page content.
- Table headers: grey-blue strip, uppercase slate text.
- Row links: plain bold dark text instead of green underlined
  (reference style; underline returns on hover).
- Cards (16px radius) and primary buttons (12px radius) already
  matched the reference — unchanged.
- Top-bar brand: verified NOT a bug — the tag is on its own line
  via CSS (`display:block`); the audit's concatenated reading was a
  screenshot-text artifact. Unchanged.

## Deliberately NOT changed

- The amber DEMO ribbon: demo scaffolding, never goes to the app.
- Home's 5 stat cards, "Smart Business Manager" tag, dark mobile
  nav, no Period Summary/Health ring on home: user-approved v2.15
  decisions (reference has 4 cards, "Grow Together", both sections
  on home). Reverting them needs the user's explicit word.
- Sidebar extras (Brands, Payments, SYSTEM group): real app
  features the user asked for; the reference lacks them because it
  is an earlier/smaller app, not because they are wrong.
- Overlapping reports (dues vs receivables/payables, inventory vs
  stock valuation): same data grouped differently, not bugs.

## Board pass (2026-10-26, user: "ai ui design same design Apply koro")

The design target changed from the labhsbm.ai.studio desktop-web
layouts to the user's mobile UI board (14 screens). Applied in the
demo copy only:
- Mobile top bar: back arrow on inner pages, hamburger on
  Dashboard, page title, search icon, bell; main h1 hidden on
  mobile. Desktop sidebar + h1 unchanged. Bottom nav already
  matched (dark green + raised Scan) — unchanged.
- Dashboard: greeting + date + Today pill banner (quote removed);
  exactly 4 stat cards (Sales/Profit/Due/Orders — Cost dropped);
  trend chart + Recent Transactions with type pills.
- Lists (Products, Inventory, Sales, Purchases, Customers,
  Suppliers, Expenses): search pill, filter pills, icon tiles,
  status pills, green prices, full-width green action button at
  the bottom. Products: SKU-Barcode + Category columns. Inventory:
  Opening/In/Out/Current/Value + Stock Summary card (Opening =
  Current − In + Out, footnoted on the page).
- New Sale checkout: customer card, product search, cart steppers,
  per-row discount, live summary (Items/Subtotal/Discount/Total/
  Paid/Due), Payment Method, Note, big Complete Sale button.
  Discount stored in the existing sale_items.discount column; note
  in a new sales.note column (init_db migration — applies to any
  existing demo.db on restart). Purchases POST updated for the
  shared line parser (discount ignored there, totals unchanged).
- Reports menu: flat icon list with chevrons on top; the grouped
  chip index kept below for the other 15 reports. "Purchase" links
  to /purchases (no dedicated purchase report exists — payables
  would have been the wrong page); "Income" links to
  /reports/balance-sheet (the app's own income page points there).
- Sales report detail: date pill, 4 stat cards, 14-day CSS bar
  chart, real Print + CSV export buttons (nothing labelled
  Excel/PDF).
- Profile hub + Settings menu hub (anchored sections, single Save).
- NOT built: Subscription screen (no paywall in the app; no fake
  plan state). POS structure untouched.
- Reports index cleanup (2026-10-27, user: "ai jinish gola lagbe
  na" + phone screenshot of the page below the menu): the Reports
  page now shows ONLY the icon menu (11 items, board screen 10).
  Removed from the index: the "All reports" grouped chips, the
  From/To filter, and the Sales summary / Profit-Loss / Cash flow
  cards and the inline Inventory / Customer dues / Supplier dues
  tables. The 15 other report pages still exist at their URLs and
  the menu's 11 detail pages are unchanged — only the index
  listing was removed.
- Premium Recent Transactions (2026-10-27, user: "recent
  transection ta aro premium Lagbe" + phone screenshot): the
  dashboard section is no longer a cramped table (no # column, no
  horizontal scroll). Each row is now: colored icon tile (Sale
  green / Purchase blue / Payment amber) + bold title (Sale
  INV-DEMO-008) + muted sub line (customer · products) on the
  left, and bold amount + colored status (Paid green / Partial
  orange / Due red) + short time ("26 Oct, 10:00 AM", new
  fmt_dt_short helper) on the right, with hairline dividers and
  "View All" in the section header. Same 10 newest transactions,
  same data and same /sales target — presentation only.

## Phase 2 (screen structure — NOT done, needs user direction)

These are bigger than styling; each adds/changes screen structure:
multi-column grids at desktop width; header stat cards + filter
bars + search on list pages (Sales, Invoices, Purchases, Categories);
POS product grid tiles + two-column layout; Customers Overview tabs;
status pills + action icons + pagination footers in tables;
tabbed Settings with option cards; Labh AI chat bubbles/suggestion
chips/Profit Alert card; Notifications filter pills/icon rows.
