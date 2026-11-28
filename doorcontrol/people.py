"""Who counts as an admin, who is on the in-app people list, and who may create calendar
unlock events.

Reads the environment and the database only. Never imports web, auth or google_signin, so
the scheduler and calendar work with nobody signed in.
"""

import os
import re

MAX_EMAIL = 254
# Plain check, enough to catch typos: one @, no spaces or list separators, a dotted domain.
_EMAIL = re.compile(
    r"[^@\s,;<>\"'()\[\]\\]+@[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+"
)


def admin_emails(env=None):
    """The admins: the exact emails in GOOGLE_ALLOWED_EMAILS (lower-cased)."""
    env = os.environ if env is None else env
    raw = env.get("GOOGLE_ALLOWED_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def listed(db):
    """Emails on the in-app people list (lower-cased). Empty without a database."""
    if db is None:
        return set()
    return set(db.people_emails())


def clean_email(value):
    """Lower-cased, trimmed email, or ValueError with a plain sentence."""
    email = str(value or "").strip().lower()
    if not email:
        raise ValueError("Type an email address.")
    if not email.isascii():
        raise ValueError(
            "Use only letters, digits and the usual symbols in the address."
        )
    if len(email) > MAX_EMAIL or not _EMAIL.fullmatch(email):
        raise ValueError("That doesn't look like an email address.")
    return email


def allowed_organizers(cal_cfg, env=None, db=None):
    """Calendar organizers whose 'Unlock:' events may open doors.

    `calendar.allowed_organizers` in config.json, when present, is the whole list.
    Otherwise the admins in GOOGLE_ALLOWED_EMAILS plus the in-app people list (read from
    the database each time). Empty means no event opens a door.
    """
    cal_cfg = cal_cfg or {}
    if "allowed_organizers" in cal_cfg:
        vals = cal_cfg.get("allowed_organizers") or []
        if isinstance(vals, str):
            vals = vals.split(",")
        return {str(e).strip().lower() for e in vals if str(e).strip()}
    return admin_emails(env) | listed(db)
