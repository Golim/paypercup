import os
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional
from fastapi import FastAPI, Request, Form, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import TimestampSigner, BadSignature

DATABASE_PATH = os.getenv("DATABASE_URL", "coffee.db")
SECRET_KEY = os.getenv("SECRET_KEY", "super-secret-key-change-me")
INITIAL_ADMIN_USERS = [
    username.strip()
    for username in os.getenv("INITIAL_ADMIN_USERS", "admin").split(",")
    if username.strip()
]
INITIAL_ADMIN_PASSWORD = os.getenv("INITIAL_ADMIN_PASSWORD")
if not INITIAL_ADMIN_PASSWORD:
    raise RuntimeError("Set INITIAL_ADMIN_PASSWORD before starting PayPerCup")

signer = TimestampSigner(SECRET_KEY)
app = FastAPI(title="PayPerCup")
templates = Jinja2Templates(directory="app/templates")

# --- DATABASE SETUP ---
def get_db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True) if "/" in DATABASE_PATH else None
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin', 'user'))
            );
            CREATE TABLE IF NOT EXISTS coffees (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                drinker_id INTEGER NOT NULL,
                offered_by_id INTEGER,
                cost_cents INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(drinker_id) REFERENCES users(id),
                FOREIGN KEY(offered_by_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                recipient_id INTEGER,
                amount_cents INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(recipient_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paid_by_id INTEGER NOT NULL,
                amount_cents INTEGER NOT NULL,
                title TEXT NOT NULL DEFAULT 'Expense',
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(paid_by_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS expense_participants (
                expense_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                share_cents INTEGER NOT NULL,
                PRIMARY KEY (expense_id, user_id),
                FOREIGN KEY(expense_id) REFERENCES expenses(id),
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS expense_payers (
                expense_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                share_cents INTEGER NOT NULL,
                PRIMARY KEY (expense_id, user_id),
                FOREIGN KEY(expense_id) REFERENCES expenses(id),
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
        """)
        cursor = conn.cursor()
        expense_columns = {row[1] for row in cursor.execute("PRAGMA table_info(expenses)")}
        if "title" not in expense_columns:
            cursor.execute("ALTER TABLE expenses ADD COLUMN title TEXT NOT NULL DEFAULT 'Expense'")
        payment_columns = {row[1] for row in cursor.execute("PRAGMA table_info(payments)")}
        if "recipient_id" not in payment_columns:
            cursor.execute("ALTER TABLE payments ADD COLUMN recipient_id INTEGER REFERENCES users(id)")
        # Ensure configured initial admins exist, including on databases that
        # already contain users. Preserve existing passwords on restarts.
        for username in INITIAL_ADMIN_USERS:
            cursor.execute(
                "INSERT INTO users (username, password, role) VALUES (?, ?, 'admin') "
                "ON CONFLICT(username) DO UPDATE SET role = 'admin'",
                (username, INITIAL_ADMIN_PASSWORD),
            )
        # Remove the legacy seeded account from older database volumes.
        if "admin" not in INITIAL_ADMIN_USERS:
            cursor.execute("DELETE FROM users WHERE username = 'admin'")
        # Carry forward old expense splits, retaining only admin credits.
        cursor.execute(
            "INSERT OR IGNORE INTO expense_payers (expense_id, user_id, share_cents) "
            "SELECT ep.expense_id, ep.user_id, ep.share_cents "
            "FROM expense_participants ep JOIN users u ON u.id = ep.user_id "
            "WHERE u.role = 'admin' AND NOT EXISTS "
            "(SELECT 1 FROM expense_payers existing WHERE existing.expense_id = ep.expense_id)"
        )
        conn.commit()

init_db()

# --- AUTH HELPER ---
def get_current_user(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Optional[sqlite3.Row]:
    session = request.cookies.get("session")
    if not session:
        return None
    try:
        unsigned = signer.unsign(session, max_age=86400 * 90).decode() # 90-day session
        user = db.execute("SELECT * FROM users WHERE id = ?", (unsigned,)).fetchone()
        return user
    except BadSignature:
        return None

# --- ROUTES ---
@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    history_user: Optional[int] = None,
    history_page: int = Query(1, ge=1),
    db: sqlite3.Connection = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    users_raw = db.execute(
        "SELECT * FROM users ORDER BY CASE WHEN role = 'admin' THEN 0 ELSE 1 END, username ASC"
    ).fetchall()
    user_stats = []
    history_page_size = 10

    for u in users_raw:
        # Calculate cups drunk & cost
        coffees = db.execute("SELECT cost_cents FROM coffees WHERE drinker_id = ?", (u['id'],)).fetchall()
        cups_count = len(coffees)
        total_coffee_cost = sum(c['cost_cents'] for c in coffees)

        # Offered coffee charges (admin paid for someone else)
        offered_cost = db.execute("SELECT COALESCE(SUM(cost_cents), 0) FROM coffees WHERE offered_by_id = ?", (u['id'],)).fetchone()[0]

        # Total payments made by this user
        paid = db.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM payments WHERE user_id = ?", (u['id'],)).fetchone()[0]
        received = db.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM payments WHERE recipient_id = ?",
            (u['id'],),
        ).fetchone()[0]

        expense_credit = db.execute(
            "SELECT COALESCE(SUM(share_cents), 0) FROM expense_payers WHERE user_id = ?",
            (u['id'],),
        ).fetchone()[0]
        legacy_expense_credit = db.execute(
            "SELECT COALESCE(SUM(e.amount_cents), 0) FROM expenses e "
            "WHERE e.paid_by_id = ? AND NOT EXISTS "
            "(SELECT 1 FROM expense_payers ep WHERE ep.expense_id = e.id) "
            "AND EXISTS (SELECT 1 FROM users payer WHERE payer.id = e.paid_by_id AND payer.role = 'admin')",
            (u['id'],),
        ).fetchone()[0]
        balance_cents = paid - received - total_coffee_cost - offered_cost + expense_credit + legacy_expense_credit

        history_total = db.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM coffees WHERE drinker_id = ?) + "
            "(SELECT COUNT(*) FROM coffees WHERE offered_by_id = ?)",
            (u['id'], u['id']),
        ).fetchone()[0]
        user_history_page = history_page if history_user == u['id'] else 1
        history_pages = max(1, (history_total + history_page_size - 1) // history_page_size)
        user_history_page = min(user_history_page, history_pages)
        history = db.execute(
            "SELECT c.id, c.created_at, "
            "CASE WHEN c.offered_by_id IS NULL THEN 'drank' ELSE 'received' END AS kind, "
            "offerer.username AS other_username "
            "FROM coffees c LEFT JOIN users offerer ON offerer.id = c.offered_by_id "
            "WHERE c.drinker_id = ? "
            "UNION ALL "
            "SELECT c.id, c.created_at, 'offered' AS kind, drinker.username AS other_username "
            "FROM coffees c JOIN users drinker ON drinker.id = c.drinker_id "
            "WHERE c.offered_by_id = ? "
            "ORDER BY 2 DESC, 1 DESC LIMIT ? OFFSET ?",
            (u['id'], u['id'], history_page_size, (user_history_page - 1) * history_page_size),
        ).fetchall()

        user_stats.append({
            "id": u['id'],
            "username": u['username'],
            "role": u['role'],
            "cups": cups_count,
            "history": history,
            "history_total": history_total,
            "history_page": user_history_page,
            "history_pages": history_pages,
            "balance_euros": f"{balance_cents / 100:+.2f}",
            "balance_raw": balance_cents
        })

    expense_history = []
    expense_rows = db.execute(
        "SELECT e.id, e.title, e.description, e.amount_cents, e.created_at, u.username AS legacy_payer "
        "FROM expenses e LEFT JOIN users u ON u.id = e.paid_by_id "
        "ORDER BY e.created_at DESC, e.id DESC"
    ).fetchall()
    for expense in expense_rows:
        payers = db.execute(
            "SELECT u.username, ep.share_cents FROM expense_payers ep "
            "JOIN users u ON u.id = ep.user_id WHERE ep.expense_id = ? ORDER BY u.username",
            (expense['id'],),
        ).fetchall()
        if not payers and expense['legacy_payer']:
            payers = [{"username": expense['legacy_payer'], "share_cents": expense['amount_cents']}]
        expense_history.append({**dict(expense), "payers": payers})

    payment_history = db.execute(
        "SELECT p.id, p.amount_cents, p.created_at, payer.username AS payer, recipient.username AS recipient "
        "FROM payments p JOIN users payer ON payer.id = p.user_id "
        "JOIN users recipient ON recipient.id = p.recipient_id "
        "WHERE p.recipient_id IS NOT NULL ORDER BY p.created_at DESC, p.id DESC"
    ).fetchall()
    admins = db.execute("SELECT id, username FROM users WHERE role = 'admin' ORDER BY username").fetchall()

    return templates.TemplateResponse(request=request, name="dashboard.html", context={
        "current_user": user,
        "users": user_stats,
        "expense_history": expense_history,
        "payment_history": payment_history,
        "admins": admins,
    })

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={"login_mode": True},
    )

@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), db: sqlite3.Connection = Depends(get_db)):
    user = db.execute("SELECT * FROM users WHERE username = ? AND password = ?", (username, password)).fetchone()
    if not user:
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={"login_mode": True, "error": "Invalid credentials"},
        )
    
    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    signed_session = signer.sign(str(user['id']).encode()).decode()
    response.set_cookie(key="session", value=signed_session, max_age=86400 * 90, httponly=True)
    return response

@app.get("/logout")
def logout():
    response = RedirectResponse(url="/login")
    response.delete_cookie("session")
    return response

@app.get("/admin/export.json")
def export_data(request: Request, db: sqlite3.Connection = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user or current_user['role'] != 'admin':
        raise HTTPException(status_code=403)

    def fetch_rows(query: str):
        return [dict(row) for row in db.execute(query).fetchall()]

    export = {
        "format": "PayPerCup JSON export",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "currency": "EUR",
        "users": fetch_rows("SELECT id, username, role FROM users ORDER BY id"),
        "coffees": fetch_rows("SELECT id, drinker_id, offered_by_id, cost_cents, created_at FROM coffees ORDER BY id"),
        "expenses": fetch_rows("SELECT id, paid_by_id, amount_cents, title, description, created_at FROM expenses ORDER BY id"),
        "expense_payers": fetch_rows("SELECT expense_id, user_id, share_cents FROM expense_payers ORDER BY expense_id, user_id"),
        "legacy_expense_participants": fetch_rows("SELECT expense_id, user_id, share_cents FROM expense_participants ORDER BY expense_id, user_id"),
        "payments": fetch_rows("SELECT id, user_id, recipient_id, amount_cents, created_at FROM payments ORDER BY id"),
    }
    filename = f"paypercup-export-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    return JSONResponse(
        content=export,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )

@app.post("/coffee/add")
def add_coffee(request: Request, user_id: int = Form(...), is_offered: bool = Form(False), db: sqlite3.Connection = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user:
        raise HTTPException(status_code=401)
    
    drinker = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    
    if is_offered:
        # Offered by current admin, costs 25 cents, billed to admin
        cost = 25
        offered_by = current_user['id']
    else:
        cost = 15 if drinker['role'] == 'admin' else 25
        offered_by = None

    db.execute("INSERT INTO coffees (drinker_id, offered_by_id, cost_cents) VALUES (?, ?, ?)",
               (user_id, offered_by, cost))
    db.commit()
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/coffee/remove")
def remove_coffee(request: Request, user_id: int = Form(...), db: sqlite3.Connection = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user or current_user['role'] != 'admin':
        raise HTTPException(status_code=403)
    
    # Delete the most recent coffee entry for this user
    latest = db.execute("SELECT id FROM coffees WHERE drinker_id = ? ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
    if latest:
        db.execute("DELETE FROM coffees WHERE id = ?", (latest['id'],))
        db.commit()
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/expense/add")
def add_expense(
    request: Request,
    title: str = Form(...),
    description: str = Form(...),
    payer_ids: list[int] = Form(...),
    amount: str = Form(...),
    db: sqlite3.Connection = Depends(get_db),
):
    current_user = get_current_user(request, db)
    if not current_user or current_user['role'] != 'admin':
        raise HTTPException(status_code=403)

    title = title.strip()
    description = description.strip()
    if not title or not description:
        raise HTTPException(status_code=422, detail="Title and description are required")
    try:
        amount_cents = int((Decimal(amount) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=422, detail="Enter a valid expense amount")
    if amount_cents <= 0:
        raise HTTPException(status_code=422, detail="Expense amount must be greater than zero")

    payer_ids = sorted(set(payer_ids))
    if not payer_ids:
        raise HTTPException(status_code=422, detail="Choose at least one admin who paid the expense")
    admin_rows = db.execute(
        f"SELECT id FROM users WHERE role = 'admin' AND id IN ({','.join('?' for _ in payer_ids)})",
        payer_ids,
    ).fetchall()
    if {row['id'] for row in admin_rows} != set(payer_ids):
        raise HTTPException(status_code=422, detail="Only registered admins can be selected")

    base_share, remainder = divmod(amount_cents, len(payer_ids))
    cursor = db.execute(
        "INSERT INTO expenses (paid_by_id, amount_cents, title, description) VALUES (?, ?, ?, ?)",
        (payer_ids[0], amount_cents, title, description),
    )
    expense_id = cursor.lastrowid
    for index, payer_id in enumerate(payer_ids):
        share_cents = base_share + (1 if index < remainder else 0)
        db.execute(
            "INSERT INTO expense_payers (expense_id, user_id, share_cents) VALUES (?, ?, ?)",
            (expense_id, payer_id, share_cents),
        )
    db.commit()
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/expense/delete")
def delete_expense(
    request: Request,
    expense_id: int = Form(...),
    db: sqlite3.Connection = Depends(get_db),
):
    current_user = get_current_user(request, db)
    if not current_user or current_user['role'] != 'admin':
        raise HTTPException(status_code=403)

    db.execute("DELETE FROM expense_participants WHERE expense_id = ?", (expense_id,))
    db.execute("DELETE FROM expense_payers WHERE expense_id = ?", (expense_id,))
    db.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
    db.commit()
    return RedirectResponse(url="/#expense-history", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/user/payment/add")
def add_user_payment(
    request: Request,
    recipient_id: int = Form(...),
    amount: str = Form(...),
    db: sqlite3.Connection = Depends(get_db),
):
    current_user = get_current_user(request, db)
    if not current_user or current_user['role'] != 'user':
        raise HTTPException(status_code=403)

    recipient = db.execute(
        "SELECT id FROM users WHERE id = ? AND role = 'admin'", (recipient_id,)
    ).fetchone()
    if not recipient:
        raise HTTPException(status_code=422, detail="Choose a registered admin")
    try:
        amount_cents = int((Decimal(amount) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=422, detail="Enter a valid payment amount")
    if amount_cents <= 0:
        raise HTTPException(status_code=422, detail="Payment amount must be greater than zero")

    db.execute(
        "INSERT INTO payments (user_id, recipient_id, amount_cents) VALUES (?, ?, ?)",
        (current_user['id'], recipient_id, amount_cents),
    )
    db.commit()
    return RedirectResponse(url="/#payment-history", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/user/add")
def add_user(request: Request, username: str = Form(...), password: str = Form(...), role: str = Form(...), db: sqlite3.Connection = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user or current_user['role'] != 'admin':
        raise HTTPException(status_code=403)

    db.execute("INSERT INTO users (username, password, role) VALUES (?, ?, ?)", (username, password, role))
    db.commit()
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
