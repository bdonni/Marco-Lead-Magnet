"""Invite-only email + password accounts for a client's dashboard.

Gamic invites people (POST /api/briefs/accounts with the ingest key) and gets back a one-time link to send them.
The link lets them set a password; after that they sign in with email + password on the locked page and stay
signed in for 90 days on that device. Removing someone ends their access at once. The old private ?key= link keeps
working alongside, so nothing changes for clients who never get accounts.

Stored in the settings table: "accounts" (JSON, email -> scrypt hash, salt, version, invite hash and expiry) and
"session_secret" (random, made on first use). Sessions are signed cookies naming the email and the account
version, so a password change or removal invalidates them.
"""
import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from typing import Optional

import store

SESSION_COOKIE = "mb_sess"
SESSION_DAYS = 90
INVITE_DAYS = 14
_attempts: dict = {}
_alock = threading.Lock()


def _load() -> dict:
    try:
        return json.loads(store.get_setting("accounts") or "{}")
    except ValueError:
        return {}


def _save(a: dict) -> None:
    store.set_setting("accounts", json.dumps(a))


def _secret() -> bytes:
    s = store.get_setting("session_secret")
    if not s:
        s = secrets.token_urlsafe(32)
        store.set_setting("session_secret", s)
    return s.encode()


def _hash_pw(pw: str, salt: str) -> str:
    return hashlib.scrypt(pw.encode(), salt=salt.encode(), n=2 ** 14, r=8, p=1, dklen=32).hex()


def _norm(email: str) -> str:
    return (email or "").strip().lower()


def any_accounts() -> bool:
    return bool(_load())


def invite(email: str) -> str:
    """Create or re-invite an account; returns the one-time token (the caller builds the link)."""
    email = _norm(email)
    if "@" not in email:
        raise ValueError("bad email")
    a = _load()
    tok = secrets.token_urlsafe(24)
    acc = a.get(email) or {"v": 1}
    acc.update({"invite": hashlib.sha256(tok.encode()).hexdigest(), "invite_exp": int(time.time()) + INVITE_DAYS * 86400})
    a[email] = acc
    _save(a)
    return tok


def remove(email: str) -> bool:
    a = _load()
    gone = a.pop(_norm(email), None) is not None
    _save(a)
    return gone


def listing() -> list:
    return [{"email": k, "active": bool(v.get("pw")), "invite_pending": bool(v.get("invite"))} for k, v in sorted(_load().items())]


def invite_email(token: str) -> Optional[str]:
    h = hashlib.sha256((token or "").encode()).hexdigest()
    for k, v in _load().items():
        if v.get("invite") and hmac.compare_digest(v["invite"], h) and v.get("invite_exp", 0) > time.time():
            return k
    return None


def set_password(token: str, pw: str) -> Optional[str]:
    email = invite_email(token)
    if not email or len(pw or "") < 10:
        return None
    a = _load()
    acc = a[email]
    salt = secrets.token_hex(16)
    acc.update({"pw": _hash_pw(pw, salt), "salt": salt, "v": int(acc.get("v", 1)) + 1})
    acc.pop("invite", None)
    acc.pop("invite_exp", None)
    _save(a)
    return email


def _throttled(key: str) -> bool:
    now = time.time()
    with _alock:
        recent = [t for t in _attempts.get(key, []) if now - t < 900]
        _attempts[key] = recent
        return len(recent) >= 8


def _note_fail(key: str) -> None:
    with _alock:
        _attempts.setdefault(key, []).append(time.time())


def check_login(email: str, pw: str, ip: str) -> Optional[str]:
    """Returns a session cookie value, or None. Eight failures in 15 minutes per email or IP locks attempts."""
    email = _norm(email)
    if _throttled("e:" + email) or _throttled("i:" + ip):
        return None
    acc = _load().get(email)
    if not acc or not acc.get("pw") or not hmac.compare_digest(_hash_pw(pw or "", acc["salt"]), acc["pw"]):
        _note_fail("e:" + email)
        _note_fail("i:" + ip)
        return None
    return make_session(email, acc)


def make_session(email: str, acc: dict) -> str:
    exp = int(time.time()) + SESSION_DAYS * 86400
    body = f"{email}|{acc.get('v', 1)}|{exp}"
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{body}|{sig}".encode()).decode()


def session_email(cookie: Optional[str]) -> Optional[str]:
    if not cookie:
        return None
    try:
        email, v, exp, sig = base64.urlsafe_b64decode(cookie.encode()).decode().rsplit("|", 3)
    except Exception:
        return None
    body = f"{email}|{v}|{exp}"
    if not hmac.compare_digest(hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest(), sig):
        return None
    if int(exp) < time.time():
        return None
    acc = _load().get(email)
    if not acc or not acc.get("pw") or str(acc.get("v", 1)) != v:
        return None
    return email
