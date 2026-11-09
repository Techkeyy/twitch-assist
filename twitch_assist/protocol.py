"""Pure Twitch chat logic (no Hermes imports, no I/O) so it can be unit-tested on its own.

Covers: IRC line + IRCv3 tag parsing, auth-failure detection, trigger/mention filtering,
per-user cooldown, own-message tracking, markdown stripping and 500-char-safe message splitting.
"""

from __future__ import annotations

import re
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Optional

TWITCH_MAX_CHARS = 500          # hard Twitch limit per chat message
CHUNK_LIMIT = 480               # we stay under it
MAX_PARTS = 3                   # at most this many chat messages per reply
MIN_SEND_INTERVAL = 1.1         # seconds between our messages (Twitch rate limit)

_TAG_UNESCAPE = {":": ";", "s": " ", "\\": "\\", "r": "\r", "n": "\n"}

_AUTH_FAILURE_MARKERS = (
    "login authentication failed",
    "improperly formatted auth",
    "login unsuccessful",
)


# -- IRC line parsing --------------------------------------------------------

def _unescape_tag(value: str) -> str:
    out, i = [], 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            out.append(_TAG_UNESCAPE.get(value[i + 1], value[i + 1]))
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def parse_line(raw: str) -> dict:
    """Parse one IRC line into ``{"tags", "prefix", "command", "params"}`` (trailing is the last param)."""
    tags: dict = {}
    if raw.startswith("@"):
        tag_str, _, raw = raw[1:].partition(" ")
        for item in tag_str.split(";"):
            key, _, val = item.partition("=")
            if key:
                tags[key] = _unescape_tag(val)
    raw = raw.lstrip(" ")
    prefix = ""
    if raw.startswith(":"):
        prefix, _, raw = raw[1:].partition(" ")
    trailing: Optional[str] = None
    if " :" in raw:
        raw, trailing = raw.split(" :", 1)
    elif raw.startswith(":"):
        raw, trailing = "", raw[1:]
    parts = raw.split()
    params = parts[1:]
    if trailing is not None:
        params.append(trailing)
    return {"tags": tags, "prefix": prefix, "command": parts[0] if parts else "",
            "params": params}


def nick_from_prefix(prefix: str) -> str:
    return prefix.split("!", 1)[0]


def is_auth_failure(msg: dict) -> bool:
    """True for the NOTICE Twitch sends when PASS/NICK is rejected."""
    if msg.get("command") != "NOTICE":
        return False
    text = (msg.get("params") or [""])[-1].lower()
    return any(marker in text for marker in _AUTH_FAILURE_MARKERS)


@dataclass
class ChatMessage:
    channel: str
    login: str
    display_name: str
    user_id: str
    message_id: str
    text: str
    badges: list = field(default_factory=list)

    @property
    def is_broadcaster(self) -> bool:
        return any(b.startswith("broadcaster/") for b in self.badges)


def parse_privmsg(msg: dict) -> Optional[ChatMessage]:
    """Turn a parsed PRIVMSG line into a ChatMessage (None for anything else, incl. /me actions)."""
    if msg.get("command") != "PRIVMSG" or len(msg.get("params", [])) < 2:
        return None
    channel, text = msg["params"][0], msg["params"][-1]
    if not channel.startswith("#") or text.startswith("\x01"):
        return None
    tags = msg.get("tags", {})
    login = nick_from_prefix(msg.get("prefix", "")).lower()
    badges = [b for b in tags.get("badges", "").split(",") if b]
    return ChatMessage(
        channel=channel.lstrip("#").lower(), login=login,
        display_name=tags.get("display-name") or login,
        user_id=tags.get("user-id", "") or login, message_id=tags.get("id", ""),
        text=text, badges=badges)


# -- addressing --------------------------------------------------------------

def extract_question(text: str, trigger: str, bot_name: str) -> Optional[str]:
    """Return the text to send to the agent if ``text`` addresses the bot, else None.

    Addressed = starts with the trigger (``!ask``) as a whole word, or mentions ``@botname``.
    The trigger / mention is stripped. An empty remainder is not a question (None).
    """
    stripped = text.strip()
    trig = (trigger or "").strip()
    if trig:
        m = re.match(rf"{re.escape(trig)}(?:\s+|$)", stripped, re.IGNORECASE)
        if m:
            return stripped[m.end():].strip() or None
    if bot_name:
        mention = re.compile(rf"(?<![\w@])@{re.escape(bot_name)}\b[,:]?", re.IGNORECASE)
        if mention.search(stripped):
            return " ".join(mention.sub("", stripped).split()) or None
    return None


class Cooldown:
    """Per-user cooldown: ``allow`` records the use and returns True only when the user is off cooldown."""

    def __init__(self, seconds: float, max_users: int = 2048):
        self.seconds = max(0.0, float(seconds))
        self._last: "OrderedDict[str, float]" = OrderedDict()
        self._max = max_users

    def allow(self, user_id: str, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        last = self._last.get(user_id)
        if last is not None and now - last < self.seconds:
            return False
        self._last[user_id] = now
        self._last.move_to_end(user_id)
        while len(self._last) > self._max:
            self._last.popitem(last=False)
        return True


class SentTracker:
    """Remembers what the adapter itself posted, so its own lines are never treated as viewer input."""

    def __init__(self, ttl: float = 300.0, max_items: int = 50):
        self.ttl = ttl
        self._items: deque = deque(maxlen=max_items)

    @staticmethod
    def _norm(text: str) -> str:
        return " ".join(text.split()).lower()

    def remember(self, text: str, now: Optional[float] = None) -> None:
        self._items.append((self._norm(text), time.monotonic() if now is None else now))

    def is_own(self, text: str, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        norm = self._norm(text)
        return any(norm == t and now - ts <= self.ttl for t, ts in self._items)


# -- outbound formatting -----------------------------------------------------

_MARKDOWN_RULES = (
    (re.compile(r"```[\w+-]*\n?"), ""),                         # code fences
    (re.compile(r"!\[([^\]]*)\]\(([^)]+)\)"), r"\2"),            # images -> url
    (re.compile(r"\[([^\]]+)\]\(([^)]+)\)"), r"\1 (\2)"),        # links -> text (url)
    (re.compile(r"\*\*(.+?)\*\*", re.S), r"\1"),                 # bold
    (re.compile(r"__(.+?)__", re.S), r"\1"),
    (re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", re.S), r"\1"),  # italic
    (re.compile(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", re.S), r"\1"),
    (re.compile(r"`([^`]+)`"), r"\1"),                           # inline code
    (re.compile(r"^\s{0,3}#{1,6}\s+", re.M), ""),                # headings
    (re.compile(r"^\s{0,3}>\s?", re.M), ""),                     # blockquotes
    (re.compile(r"^\s*[-*+]\s+", re.M), "- "),                   # bullets
)


def to_plain_text(text: str) -> str:
    """Strip markdown and collapse all whitespace/newlines to single spaces."""
    for pattern, repl in _MARKDOWN_RULES:
        text = pattern.sub(repl, text)
    text = text.replace("\x00", "")
    return " ".join(text.split())


def split_message(content: str, reply_user: Optional[str] = None, *,
                  limit: int = CHUNK_LIMIT, max_parts: int = MAX_PARTS) -> list:
    """Plain-text ``content`` into at most ``max_parts`` chat messages of <= ``limit`` chars.

    The first message is prefixed with ``@reply_user`` when given. If the text does not fit,
    the last part is cut with an ellipsis. Returns [] for empty content.
    """
    text = to_plain_text(content)
    if not text:
        return []
    prefix = f"@{reply_user} " if reply_user else ""
    parts: list = []
    remaining = text
    while remaining and len(parts) < max_parts:
        room = limit - (len(prefix) if not parts else 0)
        if len(remaining) <= room:
            parts.append(remaining)
            remaining = ""
            break
        cut = remaining.rfind(" ", 0, room + 1)
        if cut < room // 3:
            cut = room
        parts.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:  # truncated
        last = parts[-1]
        if len(last) > limit - 1:
            last = last[: limit - 1].rstrip()
        parts[-1] = last + "…"
        # the ellipsis char counts as one char; Twitch limit is by characters
    if prefix:
        parts[0] = prefix + parts[0]
    return parts
