#!/usr/bin/env python3
"""Password-reset worker for the Chains accounts database (chains-app-f38f8).

Polls two queues and services them with the Firebase Admin SDK:
  passwordResetRequests/{id}  {username, email, status:"pending"}  — self-service from the login screen
  admin/passwordResets/{id}   {uid|username, tempPassword, status:"pending"} — commissioner queue (legacy)

For a self-service request: find the account by username, compare the given email to the
account's recovery email (users/{uid}/profile/recoveryEmail, case-insensitive). If it matches,
set a temporary password, flag mustChangePassword, and email the temp password to that address.
If it doesn't match, mark the request "rejected" and send nothing (no account enumeration).

Environment (Railway → Variables):
  FIREBASE_SERVICE_ACCOUNT_JSON  the service-account key JSON (one line) for project chains-app-f38f8
  FIREBASE_DB_URL                https://chains-app-f38f8-default-rtdb.firebaseio.com
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, MAIL_FROM   any SMTP (Gmail app password, Resend, SendGrid…)
  RESET_POLL_SECONDS             default 30

Run: python reset_worker.py   (add to the Procfile as a second process, or import run_once() from poller.py's loop)
"""
import json, os, secrets, smtplib, string, sys, time
from email.message import EmailMessage

POLL = int(os.environ.get("RESET_POLL_SECONDS", "30"))
DB_URL = os.environ.get("FIREBASE_DB_URL", "https://chains-app-f38f8-default-rtdb.firebaseio.com")
DOMAIN = "@chains.app"

def log(*a): print(time.strftime("%H:%M:%S"), *a, flush=True)

def init():
    import firebase_admin
    from firebase_admin import credentials
    sa = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
    if not sa: raise SystemExit("FIREBASE_SERVICE_ACCOUNT_JSON missing")
    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(json.loads(sa)), {"databaseURL": DB_URL})

def temp_password():
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(10))

def send_mail(to, subject, body):
    host = os.environ.get("SMTP_HOST"); port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER"); pw = os.environ.get("SMTP_PASS"); frm = os.environ.get("MAIL_FROM", user)
    if not (host and user and pw): raise RuntimeError("SMTP not configured")
    msg = EmailMessage(); msg["From"] = frm; msg["To"] = to; msg["Subject"] = subject; msg.set_content(body)
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls(); s.login(user, pw); s.send_message(msg)

def find_user(auth, db, username):
    username = (username or "").strip().lower()
    if not username: return None
    try:
        return auth.get_user_by_email(username + DOMAIN)
    except Exception:
        pass
    # fall back to the public usernames index (stores the auth uid)
    v = db.reference("usernames/" + username).get() or {}
    uid = v.get("uid") if isinstance(v, dict) else None
    if uid:
        try: return auth.get_user(uid)
        except Exception: return None
    return None

def recovery_email(db, uid):
    p = db.reference("users/%s/profile" % uid).get() or {}
    em = (p.get("recoveryEmail") or "") if isinstance(p, dict) else ""
    if not em:
        root = db.reference("users/%s" % uid).get() or {}
        em = root.get("recoveryEmail") or root.get("email") or "" if isinstance(root, dict) else ""
    return (em or "").strip().lower()

def set_temp_password(auth, db, uid, pw, flag=True):
    auth.update_user(uid, password=pw)
    if flag:
        db.reference("users/%s/profile" % uid).update({"mustChangePassword": True, "passwordResetAt": int(time.time() * 1000)})

def handle_self_service(auth, db):
    ref = db.reference("passwordResetRequests")
    pend = ref.order_by_child("status").equal_to("pending").get() or {}
    for rid, r in pend.items():
        try:
            user = find_user(auth, db, r.get("username"))
            given = (r.get("email") or "").strip().lower()
            if not user or not given or recovery_email(db, user.uid) != given:
                ref.child(rid).update({"status": "rejected", "handledAt": int(time.time() * 1000)}); log("reset rejected", rid); continue
            pw = temp_password()
            set_temp_password(auth, db, user.uid, pw)
            send_mail(given, "Your Chains temporary password",
                      "Hi,\n\nSomeone (hopefully you) asked to reset the password for the Chains account \"%s\".\n\nTemporary password: %s\n\nSign in with it, then change it in Settings. If you didn't ask for this, just sign in and change your password.\n\n— Chains" % (r.get("username"), pw))
            ref.child(rid).update({"status": "done", "handledAt": int(time.time() * 1000)}); log("reset sent", rid, r.get("username"))
        except Exception as ex:
            ref.child(rid).update({"status": "error", "error": str(ex)[:200], "handledAt": int(time.time() * 1000)}); log("reset error", rid, ex)

def handle_admin_queue(auth, db):
    ref = db.reference("admin/passwordResets")
    pend = ref.order_by_child("status").equal_to("pending").get() or {}
    for rid, r in pend.items():
        try:
            user = None
            if r.get("uid"):
                try: user = auth.get_user(r["uid"])
                except Exception: user = None
            if not user: user = find_user(auth, db, r.get("username") or r.get("memberId"))
            if not user: ref.child(rid).update({"status": "error", "error": "no such user"}); continue
            set_temp_password(auth, db, user.uid, r.get("tempPassword") or temp_password(), flag=bool(r.get("setMustChange", True)))
            ref.child(rid).update({"status": "done", "handledAt": int(time.time() * 1000)}); log("admin reset done", rid)
        except Exception as ex:
            ref.child(rid).update({"status": "error", "error": str(ex)[:200]}); log("admin reset error", rid, ex)

def run_once():
    init()
    from firebase_admin import auth, db
    handle_self_service(auth, db); handle_admin_queue(auth, db)

if __name__ == "__main__":
    init()
    from firebase_admin import auth, db
    log("reset worker up")
    while True:
        try: handle_self_service(auth, db); handle_admin_queue(auth, db)
        except Exception as ex: log("loop error", ex)
        time.sleep(POLL)
