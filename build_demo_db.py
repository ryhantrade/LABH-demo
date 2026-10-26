#!/usr/bin/env python3
"""Build the demo seed database (demo.db) for the LABH demo website.

Runs the demo copy's own init_db() (so schema+migrations match the
app exactly), then inserts a small phone-shop dataset: demo business,
demo user, products, customers, supplier, October 2026 sales,
purchases, expenses and one payment. Demo data only — no real
business, no Firebase, no Drive.

Usage (from the labh-demo dir):  python3 build_demo_db.py
Output: demo.db next to app.py (labh.db is removed afterwards).
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app  # noqa: E402  (the demo copy, already patched)

DEMO_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(DEMO_DIR, "labh.db")


def main():
    for suffix in ("", "-wal", "-shm"):
        p = DB + suffix
        if os.path.exists(p):
            os.remove(p)
    app.init_db()
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys=ON")
    cur = conn.cursor()

    cur.execute("INSERT INTO businesses(name,currency,created_at) VALUES(?,?,?)",
                ("Demo Shop", "BDT", "2026-10-01T09:00:00"))
    biz = cur.lastrowid
    cur.execute("INSERT INTO users(business_id,email,name,role,created_at) VALUES(?,?,?,?,?)",
                (biz, "demo@labh.demo", "Demo User", "owner", "2026-10-01T09:00:00"))

    cur.execute("INSERT INTO categories(business_id,name,created_at) VALUES(?,?,?)", (biz, "Smartphones", "2026-10-01"))
    cat_phone = cur.lastrowid
    cur.execute("INSERT INTO categories(business_id,name,created_at) VALUES(?,?,?)", (biz, "Accessories", "2026-10-01"))
    cat_acc = cur.lastrowid

    products = [
        # name, category, brand, cost, sell, stock, low_stock
        ("Note 60 (8+256)", cat_phone, "Infinix", 18000, 22499, 24, 5),
        ("HOT 70 Pro 5G", cat_phone, "Infinix", 24000, 28999, 18, 5),
        ("Smart 20 (4+64)", cat_phone, "Infinix", 12000, 14999, 30, 5),
        ("Note Edge (8+256)", cat_phone, "Infinix", 28000, 33999, 12, 5),
        ("Smart 10 (3+64)", cat_phone, "Infinix", 9500, 11999, 40, 5),
        ("Galaxy A15", cat_phone, "Samsung", 20000, 24500, 15, 5),
        ("Redmi 13C", cat_phone, "Xiaomi", 14000, 16999, 22, 5),
        ("Charger 33W", cat_acc, "", 800, 1200, 60, 5),
        ("Back Cover Note 60", cat_acc, "", 250, 450, 3, 5),   # low stock demo
        ("Glass Protector", cat_acc, "", 150, 300, 0, 5),     # out of stock demo
    ]
    pid = {}
    for name, cat, brand, cost, sell, stock, low in products:
        cur.execute("INSERT INTO products(business_id,category_id,name,brand,purchase_price,selling_price,stock,low_stock,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?)", (biz, cat, name, brand, cost, sell, stock, low, "2026-10-01"))
        pid[name] = cur.lastrowid

    customers = {}
    for name in ("ABC Telecom", "Rahim Mobile", "Star Mobile"):
        cur.execute("INSERT INTO customers(business_id,name,created_at) VALUES(?,?,?)",
                    (biz, name, "2026-10-01"))
        customers[name] = cur.lastrowid
    cur.execute("INSERT INTO suppliers(business_id,name,created_at) VALUES(?,?,?)",
                (biz, "ABC Distribution", "2026-10-01"))
    supplier = cur.lastrowid

    # sales: (date, invoice, customer or None, [(product, qty)], paid)
    sales = [
        ("2026-10-14", "INV-DEMO-001", "ABC Telecom", [("Note 60 (8+256)", 2)], 44998),
        ("2026-10-16", "INV-DEMO-002", "Rahim Mobile", [("HOT 70 Pro 5G", 1), ("Charger 33W", 2)], 20000),
        ("2026-10-18", "INV-DEMO-003", None, [("Smart 20 (4+64)", 3)], 44997),
        ("2026-10-20", "INV-DEMO-004", "Star Mobile", [("Note Edge (8+256)", 1)], 33999),
        ("2026-10-22", "INV-DEMO-005", None, [("Galaxy A15", 2), ("Glass Protector", 1)], 49300),
        ("2026-10-24", "INV-DEMO-006", "ABC Telecom", [("Redmi 13C", 1), ("Back Cover Note 60", 4)], 6299),
        ("2026-10-25", "INV-DEMO-007", None, [("Smart 10 (3+64)", 1)], 11999),
        ("2026-10-26", "INV-DEMO-008", "Rahim Mobile", [("Note 60 (8+256)", 1)], 22499),
    ]
    price = {name: sell for name, _c, _b, _cost, sell, _s, _l in products}
    cost = {name: c for name, _c, _b, c, _s, _st, _l in products}
    for date, inv, cust, items, paid in sales:
        total = sum(price[n] * qty for n, qty in items)
        cur.execute("INSERT INTO sales(business_id,customer_id,invoice_no,total,paid,due,sale_date,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (biz, customers[cust] if cust else None, inv, total, paid, total - paid, date, date + "T10:00:00"))
        sid = cur.lastrowid
        for name, qty in items:
            cur.execute("INSERT INTO sale_items(sale_id,product_id,product_name,qty,price,amount,cost_price)"
                        " VALUES(?,?,?,?,?,?,?)",
                        (sid, pid[name], name, qty, price[name], price[name] * qty, cost[name]))

    for date, items, paid in [
        ("2026-10-10", [("Note 60 (8+256)", 10)], 180000),
        ("2026-10-12", [("Smart 20 (4+64)", 10)], 60000),
    ]:
        total = sum(cost[n] * qty for n, qty in items)
        cur.execute("INSERT INTO purchases(business_id,supplier_id,total,paid,due,purchase_date,created_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (biz, supplier, total, paid, total - paid, date, date + "T09:00:00"))
        pur = cur.lastrowid
        for name, qty in items:
            cur.execute("INSERT INTO purchase_items(purchase_id,product_id,product_name,qty,price,amount)"
                        " VALUES(?,?,?,?,?,?)",
                        (pur, pid[name], name, qty, cost[name], cost[name] * qty))

    for title, cat, amount, date in [
        ("Shop Rent", "Rent", 15000, "2026-10-01"),
        ("Staff Salary", "Salary", 25000, "2026-10-05"),
        ("Electricity Bill", "Utility", 3200, "2026-10-15"),
        ("Delivery Transport", "Transport", 4500, "2026-10-20"),
    ]:
        cur.execute("INSERT INTO expenses(business_id,title,category,amount,expense_date,created_at)"
                    " VALUES(?,?,?,?,?,?)", (biz, title, cat, amount, date, date + "T09:00:00"))

    cur.execute("INSERT INTO payments(business_id,entity_type,entity_id,amount,method,note,pay_date,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (biz, "customer", customers["Rahim Mobile"], 20000, "Cash", "Demo payment", "2026-10-17", "2026-10-17T10:00:00"))

    conn.commit()
    conn.close()

    # Demo settings: auto-sync off, update check pointed at a repo that
    # does not exist so no update pop-up ever appears on the demo.
    app.set_setting("auto_sync", "0")
    app.set_setting("github_release_repo", "labh-demo/no-release")

    os.rename(DB, os.path.join(DEMO_DIR, "demo.db"))
    for suffix in ("-wal", "-shm"):
        p = DB + suffix
        if os.path.exists(p):
            os.remove(p)
    print("demo.db built")


if __name__ == "__main__":
    main()
