"""Email delivery for the gated action: an approved memo goes to the partner by SMTP.

Settings (non-secret) live in settings.json under "email": host, port, security ("ssl", "starttls" or
"none"), username, from_addr, to_addr. The password is read from SMTP_PASSWORD or its own owner-only
file (config.SMTP_PASSWORD_FILE). "none" (no encryption) is allowed only for a server on this machine.
"""
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid

from . import config, settings

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class MailError(Exception):
    pass


def email_settings():
    if config.HOSTED:
        # One outgoing mail account for the service (PARA_SMTP_* and SMTP_PASSWORD on the server);
        # each firm only chooses its partner's address.
        cfg = {"host": os.getenv("PARA_SMTP_HOST", ""), "port": os.getenv("PARA_SMTP_PORT", "587"),
               "security": os.getenv("PARA_SMTP_SECURITY", "starttls"), "username": os.getenv("PARA_SMTP_USERNAME", ""),
               "from_addr": os.getenv("PARA_SMTP_FROM", ""), "to_addr": settings.load().get("partner_email", "")}
    else:
        cfg = settings.load().get("email") or {}
    needed = ("host", "port", "security", "from_addr", "to_addr")
    return cfg if all(cfg.get(k) for k in needed) else None


def password():
    value = os.getenv("SMTP_PASSWORD", "")
    if value:
        return value
    f = config.SMTP_PASSWORD_FILE
    try:
        if f.is_symlink() or not f.is_file() or f.stat().st_size > 4096:
            return ""
        lines = f.read_text().splitlines()
    except OSError:
        return ""
    return lines[0][len("SMTP_PASSWORD="):] if len(lines) == 1 and lines[0].startswith("SMTP_PASSWORD=") else ""


def describe():
    """What the page may show about delivery: never the password, never the server login."""
    cfg = email_settings()
    return {"channel": "email", "to": cfg["to_addr"]} if cfg else {"channel": "log", "to": None}


def _connect(cfg, timeout=20):
    host, port, security = cfg["host"], int(cfg["port"]), cfg["security"]
    if security == "ssl":
        smtp = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context())
    elif security == "starttls":
        smtp = smtplib.SMTP(host, port, timeout=timeout)
        smtp.starttls(context=ssl.create_default_context())
    elif security == "none" and host in LOCAL_HOSTS:
        smtp = smtplib.SMTP(host, port, timeout=timeout)
    else:
        raise MailError("Unencrypted SMTP is only allowed to a server on this machine")
    if cfg.get("username"):
        smtp.login(cfg["username"], password())
    return smtp


def check_login(cfg):
    """Connect and log in without sending anything. Raises MailError with a safe message."""
    try:
        with _connect(cfg) as smtp:
            smtp.noop()
    except smtplib.SMTPAuthenticationError:
        raise MailError("The mail server rejected the username or password") from None
    except (smtplib.SMTPException, OSError, ssl.SSLError) as e:
        raise MailError(f"Could not connect to {cfg['host']}:{cfg['port']} ({type(e).__name__})") from None


def send_memo(memo, check_id, reason, subject_hint=""):
    """Email one approved memo to the partner. Returns the recipient; raises MailError on failure."""
    cfg = email_settings()
    if not cfg:
        raise MailError("Email is not set up")
    msg = EmailMessage()
    subject = (subject_hint or memo.strip().splitlines()[0])[:90]
    msg["Subject"] = f"Research memo: {subject}"
    msg["From"] = cfg["from_addr"]
    msg["To"] = cfg["to_addr"]
    msg["Message-ID"] = make_msgid(domain=cfg["from_addr"].split("@")[-1] or None)
    msg["X-Para-Check"] = check_id
    msg.set_content(f"{memo}\n\n--\nChecked by Para before sending ({check_id}).\n{reason}\n")
    try:
        with _connect(cfg) as smtp:
            smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise MailError("The mail server rejected the username or password") from None
    except (smtplib.SMTPException, OSError, ssl.SSLError) as e:
        raise MailError(f"The mail server did not accept the memo ({type(e).__name__})") from None
    return cfg["to_addr"]
