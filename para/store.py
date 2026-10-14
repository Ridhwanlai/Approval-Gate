"""The hosted version's database: firms, users, sign-in sessions, usage, subscriptions and audit.

SQLite from the standard library, one file on the server's persistent disk (config.DB_FILE).
Passwords are stored as salted PBKDF2-SHA256 hashes; session and invite tokens only as SHA-256
hashes, so a copy of the database cannot be used to sign in.
"""
import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time

from . import config, plans

SCHEMA = """
create table if not exists firms (id text primary key, name text not null, plan text not null default 'trial',
  status text not null default 'trialing', trial_ends real, period_start real, period_end real,
  stripe_customer text, stripe_subscription text, settings text not null default '{}', lists text, created real not null);
create table if not exists users (id text primary key, email text unique not null, name text, pw_hash text not null, created real not null);
create table if not exists members (firm_id text not null, user_id text not null, role text not null, primary key (firm_id, user_id));
create table if not exists sessions (token_hash text primary key, user_id text not null, firm_id text not null, csrf text not null, expires real not null);
create table if not exists usage (firm_id text not null, period text not null, used integer not null default 0, primary key (firm_id, period));
create table if not exists audit (id integer primary key autoincrement, firm_id text not null, ts real not null, entry text not null);
create table if not exists invites (token_hash text primary key, firm_id text not null, expires real not null, used integer not null default 0);
create table if not exists login_failures (key text primary key, count integer not null, window_start real not null);
create table if not exists stripe_events (id text primary key, ts real not null);
create index if not exists audit_firm on audit (firm_id, id);
"""
SESSION_DAYS = 14
PBKDF2_ROUNDS = 600_000

_lock = threading.RLock()
_conn = None


class StoreError(Exception):
    """A user-facing problem (duplicate email, bad invite, no seats). The message is safe to show."""


def db():
    global _conn
    with _lock:
        if _conn is None:
            config.DB_FILE.parent.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(str(config.DB_FILE), check_same_thread=False, isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.execute("pragma journal_mode=wal")
            _conn.execute("pragma foreign_keys=on")
            _conn.executescript(SCHEMA)
        return _conn


def reset_for_tests():
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None


def _q(sql, args=()):
    with _lock:
        return db().execute(sql, args)


def _one(sql, args=()):
    row = _q(sql, args).fetchone()
    return dict(row) if row else None


def _new_id(prefix):
    return f"{prefix}_{secrets.token_hex(8)}"


def _hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------- passwords

def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def check_password(password, stored):
    try:
        _, rounds, salt, digest = stored.split("$")
        test = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(test.hex(), digest)
    except (ValueError, AttributeError):
        return False


def valid_password(password):
    return isinstance(password, str) and 10 <= len(password) <= 200


def normal_email(email):
    email = (email or "").strip().lower()
    if not (3 <= len(email) <= 254) or "@" not in email or " " in email or email.startswith("@") or email.endswith("@"):
        raise StoreError("Enter a valid email address.")
    return email


# ---------------------------------------------------------------- firms and users

def create_firm_with_owner(firm_name, name, email, password, now=None):
    now = now or time.time()
    email = normal_email(email)
    firm_name = (firm_name or "").strip()[:120]
    if not firm_name:
        raise StoreError("Enter your firm's name.")
    if not valid_password(password):
        raise StoreError("Use a password of at least 10 characters.")
    with _lock:
        if _one("select id from users where email = ?", (email,)):
            raise StoreError("An account with that email already exists. Sign in instead.")
        firm_id, user_id = _new_id("firm"), _new_id("user")
        _q("insert into firms (id, name, plan, status, trial_ends, created) values (?, ?, 'trial', 'trialing', ?, ?)",
           (firm_id, firm_name, now + plans.TRIAL_DAYS * 86400, now))
        _q("insert into users (id, email, name, pw_hash, created) values (?, ?, ?, ?, ?)",
           (user_id, email, (name or "").strip()[:120], hash_password(password), now))
        _q("insert into members (firm_id, user_id, role) values (?, ?, 'owner')", (firm_id, user_id))
    return firm_id, user_id


def firm(firm_id):
    f = _one("select * from firms where id = ?", (firm_id,))
    if f:
        f["settings"] = json.loads(f["settings"] or "{}")
    return f


def firm_by_customer(customer_id):
    row = _one("select id from firms where stripe_customer = ?", (customer_id,))
    return firm(row["id"]) if row else None


def user(user_id):
    return _one("select id, email, name, created from users where id = ?", (user_id,))


def membership(user_id):
    return _one("select firm_id, role from members where user_id = ?", (user_id,))


def members(firm_id):
    return [dict(r) for r in _q("select u.id, u.email, u.name, m.role from members m join users u on u.id = m.user_id "
                                "where m.firm_id = ? order by m.role desc, u.email", (firm_id,))]


def update_firm(firm_id, **fields):
    allowed = {"plan", "status", "trial_ends", "period_start", "period_end", "stripe_customer", "stripe_subscription", "name"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if sets:
        _q(f"update firms set {', '.join(k + ' = ?' for k in sets)} where id = ?", (*sets.values(), firm_id))


def firm_settings(firm_id):
    f = firm(firm_id)
    return f["settings"] if f else {}


def save_firm_settings(firm_id, data):
    _q("update firms set settings = ? where id = ?", (json.dumps(data), firm_id))


def firm_lists(firm_id):
    row = _one("select lists from firms where id = ?", (firm_id,))
    return json.loads(row["lists"]) if row and row["lists"] else None


def save_firm_lists(firm_id, lists):
    _q("update firms set lists = ? where id = ?", (json.dumps(lists) if lists else None, firm_id))


# ---------------------------------------------------------------- sign-in

def _failure_key(email, ip):
    return f"{email}|{ip}"


def too_many_failures(email, ip, now=None, limit=10, window=900):
    now = now or time.time()
    row = _one("select count, window_start from login_failures where key = ?", (_failure_key(email, ip),))
    return bool(row) and now - row["window_start"] < window and row["count"] >= limit


def _record_failure(email, ip, now, window=900):
    key = _failure_key(email, ip)
    row = _one("select count, window_start from login_failures where key = ?", (key,))
    if not row or now - row["window_start"] >= window:
        _q("insert or replace into login_failures (key, count, window_start) values (?, 1, ?)", (key, now))
    else:
        _q("update login_failures set count = count + 1 where key = ?", (key,))


def authenticate(email, password, ip="", now=None):
    now = now or time.time()
    try:
        email = normal_email(email)
    except StoreError:
        return None
    if too_many_failures(email, ip, now):
        raise StoreError("Too many sign-in attempts. Wait 15 minutes and try again.")
    row = _one("select id, pw_hash from users where email = ?", (email,))
    if not row or not check_password(password or "", row["pw_hash"]):
        _record_failure(email, ip, now)
        return None
    _q("delete from login_failures where key = ?", (_failure_key(email, ip),))
    return row["id"]


def create_session(user_id, now=None):
    now = now or time.time()
    m = membership(user_id)
    if not m:
        raise StoreError("This account is not part of a firm.")
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    _q("insert into sessions (token_hash, user_id, firm_id, csrf, expires) values (?, ?, ?, ?, ?)",
       (_hash_token(token), user_id, m["firm_id"], csrf, now + SESSION_DAYS * 86400))
    return token


def session(token, now=None):
    """{user_id, firm_id, role, csrf} for a live session token, else None."""
    if not token:
        return None
    now = now or time.time()
    row = _one("select s.user_id, s.firm_id, s.csrf, s.expires, m.role from sessions s "
               "join members m on m.user_id = s.user_id and m.firm_id = s.firm_id where s.token_hash = ?", (_hash_token(token),))
    if not row or row["expires"] < now:
        return None
    return row


def end_session(token):
    if token:
        _q("delete from sessions where token_hash = ?", (_hash_token(token),))


# ---------------------------------------------------------------- team invites

def create_invite(firm_id, now=None, days=7):
    now = now or time.time()
    f = firm(firm_id)
    ent = plans.entitlement(f, now)
    pending = _one("select count(*) n from invites where firm_id = ? and used = 0 and expires > ?", (firm_id, now))["n"]
    if len(members(firm_id)) + pending >= ent["seats"]:
        raise StoreError(f"Your {ent['label']} plan includes {ent['seats']} user(s). Upgrade to add more.")
    token = secrets.token_urlsafe(24)
    _q("insert into invites (token_hash, firm_id, expires) values (?, ?, ?)", (_hash_token(token), firm_id, now + days * 86400))
    return token


def accept_invite(token, name, email, password, now=None):
    now = now or time.time()
    email = normal_email(email)
    if not valid_password(password):
        raise StoreError("Use a password of at least 10 characters.")
    with _lock:
        inv = _one("select firm_id, expires, used from invites where token_hash = ?", (_hash_token(token or ""),))
        if not inv or inv["used"] or inv["expires"] < now:
            raise StoreError("This invite link is invalid or has expired. Ask for a new one.")
        if _one("select id from users where email = ?", (email,)):
            raise StoreError("An account with that email already exists.")
        user_id = _new_id("user")
        _q("insert into users (id, email, name, pw_hash, created) values (?, ?, ?, ?, ?)",
           (user_id, email, (name or "").strip()[:120], hash_password(password), now))
        _q("insert into members (firm_id, user_id, role) values (?, ?, 'member')", (inv["firm_id"], user_id))
        _q("update invites set used = 1 where token_hash = ?", (_hash_token(token),))
    return inv["firm_id"], user_id


def remove_member(firm_id, user_id):
    row = _one("select role from members where firm_id = ? and user_id = ?", (firm_id, user_id))
    if not row:
        raise StoreError("That person is not in your firm.")
    if row["role"] == "owner":
        raise StoreError("The firm's owner can't be removed.")
    _q("delete from sessions where user_id = ?", (user_id,))
    _q("delete from members where firm_id = ? and user_id = ?", (firm_id, user_id))


# ---------------------------------------------------------------- usage

def usage(firm_id, now=None):
    f = firm(firm_id)
    ent = plans.entitlement(f, now)
    row = _one("select used from usage where firm_id = ? and period = ?", (firm_id, ent["period_key"]))
    used = row["used"] if row else 0
    return {**ent, "used": used, "remaining": max(0, ent["checks"] - used)}


def consume(firm_id, now=None):
    """Use one AI check from this period's allowance, or raise StoreError saying why not."""
    with _lock:
        u = usage(firm_id, now)
        if not u["active"]:
            raise StoreError(u["reason"])
        if u["used"] >= u["checks"]:
            raise StoreError(f"Your {u['label']} plan's {u['checks']} AI checks for this period are used. "
                             + ("Choose a plan to continue." if u["plan"] == "trial" else "Upgrade, or wait for your next billing period."))
        _q("insert into usage (firm_id, period, used) values (?, ?, 1) on conflict (firm_id, period) do update set used = used + 1",
           (firm_id, u["period_key"]))
        return u["checks"] - u["used"] - 1


def refund(firm_id, now=None):
    """Give back one check, e.g. when a job could not start."""
    u = usage(firm_id, now)
    _q("update usage set used = max(0, used - 1) where firm_id = ? and period = ?", (firm_id, u["period_key"]))


# ---------------------------------------------------------------- audit

def add_audit(firm_id, entry):
    _q("insert into audit (firm_id, ts, entry) values (?, ?, ?)", (firm_id, time.time(), json.dumps(entry)))


def read_audit(firm_id, limit=50):
    rows = _q("select entry from audit where firm_id = ? order by id desc limit ?", (firm_id, limit)).fetchall()
    return [json.loads(r["entry"]) for r in reversed(rows)]


# ---------------------------------------------------------------- stripe idempotency

def seen_event(event_id):
    """True if this Stripe event was already handled; records it otherwise."""
    with _lock:
        if _one("select id from stripe_events where id = ?", (event_id,)):
            return True
        _q("insert into stripe_events (id, ts) values (?, ?)", (event_id, time.time()))
        return False
