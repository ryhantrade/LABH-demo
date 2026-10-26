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

## Phase 2 (screen structure — NOT done, needs user direction)

These are bigger than styling; each adds/changes screen structure:
multi-column grids at desktop width; header stat cards + filter
bars + search on list pages (Sales, Invoices, Purchases, Categories);
POS product grid tiles + two-column layout; Customers Overview tabs;
status pills + action icons + pagination footers in tables;
tabbed Settings with option cards; Labh AI chat bubbles/suggestion
chips/Profit Alert card; Notifications filter pills/icon rows.
