# app.py
"""
WRR Economy - persistent version with cashout.
- SQLite persistence for users, balances, investments, logs
- Cash out investments once per day -> converted to WRR
- Invest using your currencies (WRR/LC/KP)
- Gambling unchanged
- Chart snapshot via /api/snapshot used by dashboard JS
"""

import sqlite3
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date
import random, os, json

DB = "wrr_persistent.db"
app = Flask(__name__)
app.secret_key = os.environ.get("WRR_SECRET", "dev_secret_change_me")

# Rates (EUR per unit)
CURRENCY_RATES_EUR = {"WRR": 50.23, "LC": 20.54, "KP": 5.01}
# Asset prices in EUR (we persist only latest snapshot here; historical series optional)
ASSET_PRICES = {"WRR": CURRENCY_RATES_EUR["WRR"], "WRRC": 100.0, "LBC": 75.0, "KSP": 40.0}

# --- DB helpers ---
def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    c = conn()
    cur = c.cursor()
    cur.execute("""
      CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        username TEXT UNIQUE,
        password TEXT,
        last_cashout TEXT
      )
    """)
    cur.execute("""
      CREATE TABLE IF NOT EXISTS balances (
        user_id INTEGER,
        currency TEXT,
        amount REAL,
        PRIMARY KEY(user_id, currency)
      )
    """)
    cur.execute("""
      CREATE TABLE IF NOT EXISTS investments (
        id INTEGER PRIMARY KEY,
        user_id INTEGER,
        asset TEXT,
        shares REAL
      )
    """)
    cur.execute("""
      CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY,
        ts TEXT,
        level TEXT,
        message TEXT
      )
    """)
    cur.execute("""
      CREATE TABLE IF NOT EXISTS prices (
        asset TEXT PRIMARY KEY,
        price_eur REAL,
        updated_at TEXT
      )
    """)
    # ensure price table populated
    for a, p in ASSET_PRICES.items():
        cur.execute("INSERT OR REPLACE INTO prices(asset, price_eur, updated_at) VALUES (?, ?, ?)",
                    (a, float(p), datetime.utcnow().isoformat()))
    c.commit(); c.close()

def log(level, message):
    c = conn(); cur = c.cursor()
    cur.execute("INSERT INTO logs(ts, level, message) VALUES (?, ?, ?)", (datetime.utcnow().isoformat(), level, message))
    c.commit(); c.close()

# --- user helpers ---
def create_user(username, password):
    c = conn(); cur = c.cursor()
    try:
        cur.execute("INSERT INTO users(username, password) VALUES (?, ?)", (username, generate_password_hash(password)))
        uid = cur.lastrowid
        for cur_code in CURRENCY_RATES_EUR.keys():
            cur.execute("INSERT INTO balances(user_id, currency, amount) VALUES (?, ?, ?)", (uid, cur_code, 0.0))
        # give starting balances
        cur.execute("UPDATE balances SET amount = ? WHERE user_id = ? AND currency = ?", (10.0, uid, "WRR"))
        cur.execute("UPDATE balances SET amount = ? WHERE user_id = ? AND currency = ?", (25.0, uid, "LC"))
        cur.execute("UPDATE balances SET amount = ? WHERE user_id = ? AND currency = ?", (50.0, uid, "KP"))
        c.commit(); c.close()
        log("INFO", f"User created: {username}")
        return True
    except sqlite3.IntegrityError:
        c.close(); return False

def get_user_by_username(username):
    c = conn(); cur = c.cursor()
    cur.execute("SELECT * FROM users WHERE username=?", (username,))
    r = cur.fetchone(); c.close(); return r

def get_balances(user_id):
    c = conn(); cur = c.cursor()
    cur.execute("SELECT currency, amount FROM balances WHERE user_id=?", (user_id,))
    rows = cur.fetchall(); c.close()
    return {r["currency"]: r["amount"] for r in rows}

def change_balance(user_id, currency, delta):
    bal = get_balances(user_id)
    cur_amount = bal.get(currency, 0.0)
    new = cur_amount + delta
    if new < -1e-8:
        return False
    c = conn(); cur = c.cursor()
    cur.execute("INSERT OR REPLACE INTO balances(user_id, currency, amount) VALUES (?, ?, ?)", (user_id, currency, float(new)))
    c.execute("INSERT INTO logs(ts, level, message) VALUES (?, ?, ?)", (datetime.utcnow().isoformat(), "INFO", f"user {user_id} balance {currency} changed by {delta:.4f} => {new:.4f}"))
    c.commit(); c.close()
    return True

def add_investment(user_id, asset, shares):
    c = conn(); cur = c.cursor()
    # add to existing or insert
    cur.execute("SELECT id, shares FROM investments WHERE user_id=? AND asset=?", (user_id, asset))
    r = cur.fetchone()
    if r:
        new_shares = r["shares"] + shares
        cur.execute("UPDATE investments SET shares=? WHERE id=?", (new_shares, r["id"]))
    else:
        cur.execute("INSERT INTO investments(user_id, asset, shares) VALUES (?, ?, ?)", (user_id, asset, shares))
    cur.execute("INSERT INTO logs(ts, level, message) VALUES (?, ?, ?)", (datetime.utcnow().isoformat(), "INFO", f"user {user_id} invested {shares} {asset}"))
    c.commit(); c.close()

def get_investments(user_id):
    c = conn(); cur = c.cursor()
    cur.execute("SELECT asset, shares FROM investments WHERE user_id=?", (user_id,))
    rows = cur.fetchall(); c.close()
    return {r["asset"]: r["shares"] for r in rows}

def clear_investments(user_id):
    c = conn(); cur = c.cursor()
    cur.execute("DELETE FROM investments WHERE user_id=?", (user_id,))
    c.commit(); c.close()

def get_price(asset):
    c = conn(); cur = c.cursor()
    cur.execute("SELECT price_eur FROM prices WHERE asset=?", (asset,))
    r = cur.fetchone(); c.close()
    return r["price_eur"] if r else ASSET_PRICES.get(asset, 1.0)

def update_price(asset, price):
    c = conn(); cur = c.cursor()
    cur.execute("INSERT OR REPLACE INTO prices(asset, price_eur, updated_at) VALUES (?, ?, ?)", (asset, float(price), datetime.utcnow().isoformat()))
    c.commit(); c.close()

# --- simulation helper: small ticks (optional) ---
def small_tick():
    # small random walk and persist snapshot prices
    for a in list(ASSET_PRICES.keys()):
        pct = random.uniform(-0.02, 0.02)
        ASSET_PRICES[a] = max(0.01, round(ASSET_PRICES[a] * (1 + pct), 4))
        update_price(a, ASSET_PRICES[a])
    log("INFO", "Market tick (small)")

# --- cashout (once per calendar day) ---
def cashout(user_id):
    c = conn(); cur = c.cursor()
    cur.execute("SELECT last_cashout FROM users WHERE id=?", (user_id,))
    row = cur.fetchone()
    last = row["last_cashout"] if row else None
    today = date.today().isoformat()
    if last == today:
        c.close(); return False, "Already cashed out today"
    # calculate total EUR value of investments
    cur.execute("SELECT asset, shares FROM investments WHERE user_id=?", (user_id,))
    rows = cur.fetchall()
    total_eur = 0.0
    for r in rows:
        asset = r["asset"]; shares = r["shares"]
        price = get_price(asset)
        total_eur += shares * price
    # clear investments
    cur.execute("DELETE FROM investments WHERE user_id=?", (user_id,))
    # convert EUR to WRR units and credit
    wrr_units = total_eur / CURRENCY_RATES_EUR["WRR"] if CURRENCY_RATES_EUR["WRR"]>0 else 0.0
    # get current WRR balance
    cur.execute("SELECT amount FROM balances WHERE user_id=? AND currency=?", (user_id, "WRR"))
    r = cur.fetchone()
    cur_wrr = r["amount"] if r else 0.0
    new_wrr = cur_wrr + wrr_units
    cur.execute("INSERT OR REPLACE INTO balances(user_id, currency, amount) VALUES (?, ?, ?)", (user_id, "WRR", new_wrr))
    # set last_cashout
    cur.execute("UPDATE users SET last_cashout=? WHERE id=?", (today, user_id))
    cur.execute("INSERT INTO logs(ts, level, message) VALUES (?, ?, ?)", (datetime.utcnow().isoformat(), "INFO", f"user {user_id} cashed out {total_eur:.2f} EUR -> {wrr_units:.4f} WRR"))
    c.commit(); c.close()
    return True, f"Cashed out {total_eur:.2f} EUR -> {wrr_units:.4f} WRR"

# --- bootstrap (create DB and sample user) ---
def bootstrap():
    init_db()
    # create sample users if none
    c = conn(); cur = c.cursor()
    cur.execute("SELECT 1 FROM users LIMIT 1")
    if not cur.fetchone():
        create_user("alice", "alicepass")
        create_user("bob", "bobpass")
        log("INFO", "Created demo users alice/bob")
    c.close()

# ---------------- ROUTES ----------------
@app.route("/")
def root():
    if "username" not in session:
        return redirect(url_for("login"))
    return redirect(url_for("dashboard"))

@app.route("/register", methods=["GET","POST"])
def register():
    if request.method=="POST":
        u = request.form.get("username","").strip()
        p = request.form.get("password","")
        if not u or not p:
            flash("Enter username & password"); return redirect(url_for("register"))
        ok = create_user(u,p)
        if not ok:
            flash("Username exists"); return redirect(url_for("register"))
        session["username"] = u
        flash("Account created and logged in")
        return redirect(url_for("dashboard"))
    return render_template("register.html")

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method=="POST":
        u = request.form.get("username","").strip()
        p = request.form.get("password","")
        row = get_user_by_username(u)
        if not row:
            flash("Unknown user"); return redirect(url_for("login"))
        if check_password_hash(row["password"], p):
            session["username"] = u
            flash("Logged in")
            return redirect(url_for("dashboard"))
        flash("Bad password"); return redirect(url_for("login"))
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear(); flash("Logged out"); return redirect(url_for("login"))

@app.route("/dashboard")
def dashboard():
    if "username" not in session:
        return redirect(url_for("login"))
    # small tick and persist snapshot
    small_tick()
    row = get_user_by_username(session["username"])
    if not row:
        session.clear(); flash("Session error"); return redirect(url_for("login"))
    uid = row["id"]
    balances = get_balances(uid)
    investments = get_investments(uid)
    # supply snapshot of latest prices to JS
    c = conn(); cur = c.cursor()
    cur.execute("SELECT asset, price_eur FROM prices")
    js_prices = {r["asset"]: r["price_eur"] for r in cur.fetchall()}
    c.close()
    # recent logs
    c = conn(); df = c.execute("SELECT ts, level, message FROM logs ORDER BY ts DESC LIMIT 100").fetchall(); c.close()
    logs_list = [{"ts": r["ts"], "level": r["level"], "message": r["message"]} for r in df]
    return render_template("dashboard.html", assets=js_prices, balances=balances, investments=investments, rates=CURRENCY_RATES_EUR, logs=logs_list)

@app.route("/invest", methods=["GET","POST"])
def invest():
    if "username" not in session:
        flash("Log in"); return redirect(url_for("login"))
    user = get_user_by_username(session["username"]); uid = user["id"]
    if request.method=="POST":
        asset = request.form.get("asset")
        shares = float(request.form.get("shares","0") or 0)
        currency = request.form.get("currency")
        price_eur = get_price(asset)
        units_needed = shares * price_eur / CURRENCY_RATES_EUR[currency]
        # check funds
        bal = get_balances(uid)
        if bal.get(currency,0.0) + 1e-9 < units_needed:
            flash("Insufficient funds"); return redirect(url_for("invest"))
        # debit and add investment
        change_balance(uid, currency, -units_needed)
        add_investment(uid, asset, shares)
        flash(f"Bought {shares} {asset} for {units_needed:.4f} {currency}")
        return redirect(url_for("dashboard"))
    # GET
    balances = get_balances(uid)
    c = conn(); cur = c.cursor()
    cur.execute("SELECT asset, price_eur FROM prices"); rows = cur.fetchall(); c.close()
    prices = {r["asset"]: r["price_eur"] for r in rows}
    return render_template("invest.html", balances=balances, assets=prices, rates=CURRENCY_RATES_EUR)

@app.route("/cashout", methods=["POST"])
def cashout_route():
    if "username" not in session:
        flash("Log in"); return redirect(url_for("login"))
    user = get_user_by_username(session["username"]); uid = user["id"]
    ok, msg = cashout(uid)
    flash(msg)
    return redirect(url_for("dashboard"))

@app.route("/gamble", methods=["GET","POST"])
def gamble():
    if "username" not in session:
        flash("Log in"); return redirect(url_for("login"))
    user = get_user_by_username(session["username"]); uid = user["id"]
    if request.method=="POST":
        currency = request.form.get("currency")
        units = float(request.form.get("units","0") or 0)
        bal = get_balances(uid)
        if bal.get(currency,0.0) + 1e-9 < units:
            flash("Insufficient funds"); return redirect(url_for("gamble"))
        ok = change_balance(uid, currency, -units)
        if not ok:
            flash("Debit failed"); return redirect(url_for("gamble"))
        win = random.random() < 0.45
        if win:
            payout = units * 2.0
            change_balance(uid, currency, payout)
            flash(f"You won {payout:.2f} {currency}!")
        else:
            flash(f"You lost {units:.2f} {currency}.")
        return redirect(url_for("dashboard"))
    balances = get_balances(uid)
    return render_template("gamble.html", balances=balances, rates=CURRENCY_RATES_EUR)

@app.route("/api/snapshot")
def api_snapshot():
    c = conn(); cur = c.cursor()
    cur.execute("SELECT asset, price_eur FROM prices"); rows = cur.fetchall(); c.close()
    return jsonify({r["asset"]: r["price_eur"] for r in rows})

import os

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))  # Render sets PORT dynamically
    app.run(host="0.0.0.0", port=port)
