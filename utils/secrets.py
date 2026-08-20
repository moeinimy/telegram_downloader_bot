"""
Keep credentials out of anything that gets written down.

The bot writes error text in three places that outlive the moment: the
journal, the admin chat, and a user's message history. An exception is not a
neutral string - httpx puts the whole failing url in its message, and this
bot's urls carry tokens:

    Client error '400 Bad Request' for url
    'https://graph.instagram.com/v21.0/me?fields=id&access_token=EAAG...'

/srcstatus formats exactly that into a Telegram message. It is admin-only,
which limits who sees it, and does nothing at all about the fact that a
sixty-day credential is now in a chat history that syncs to Telegram's
servers and stays there.

So the rule is: text derived from an exception goes through scrub() before it
is logged or sent. Cheap enough to apply everywhere, and the alternative is
remembering which endpoints have secrets in their urls - which is the kind of
thing that is true until somebody adds one.
"""

from __future__ import annotations

import re

# Query parameters whose VALUE is a credential. Matched case-insensitively,
# and the replacement keeps the parameter name so the message still reads.
_SECRET_PARAMS = (
    "access_token", "token", "api_key", "apikey", "key", "sessionid",
    "csrftoken", "client_secret", "password", "auth", "signature", "sig",
)

_PARAM_RE = re.compile(
    r"(?i)\b(" + "|".join(_SECRET_PARAMS) + r")=([^&\s'\"]+)"
)

# Bearer/Basic headers, which land in text when a request object is printed.
_HEADER_RE = re.compile(r"(?i)\b(bearer|basic)\s+([A-Za-z0-9._\-+/=]{8,})")

# Instagram's own token shape, in case it appears without a parameter name.
_IG_TOKEN_RE = re.compile(r"\bEAA[A-Za-z0-9]{20,}")

_MASK = "***"


def scrub(text) -> str:
    """The same text with credential values replaced by ***.

    Never raises: this runs on error paths, and a redaction helper that can
    itself fail would turn a reportable problem into a silent one.
    """
    try:
        out = str(text)
        out = _PARAM_RE.sub(lambda m: f"{m.group(1)}={_MASK}", out)
        out = _HEADER_RE.sub(lambda m: f"{m.group(1)} {_MASK}", out)
        out = _IG_TOKEN_RE.sub(_MASK, out)
        return out
    except Exception:
        return "<unprintable>"
