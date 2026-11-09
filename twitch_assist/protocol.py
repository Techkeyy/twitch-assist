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

try:
    from . import notices
except ImportError:  # loaded straight from its file (tests, no package context)
    import importlib.util as _ilu
    import pathlib as _pl
    import sys as _sys

    _spec = _ilu.spec_from_file_location("twitch_notices", _pl.Path(__file__).with_name("notices.py"))
    notices = _ilu.module_from_spec(_spec)
    _sys.modules["twitch_notices"] = notices
    _spec.loader.exec_module(notices)

TWITCH_MAX_CHARS = 500          # hard Twitch limit per chat message
CHUNK_LIMIT = 480               # we stay under it
MAX_PARTS = 3                   # at most this many chat messages per reply
MIN_SEND_INTERVAL = 1.6         # seconds between our messages (Twitch non-mod limit: 20 messages per 30 s)

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
    mod_tag: bool = False

    @property
    def is_broadcaster(self) -> bool:
        return any(b.startswith("broadcaster/") for b in self.badges)

    @property
    def is_moderator(self) -> bool:
        return self.mod_tag or any(b.startswith("moderator/") for b in self.badges)

    @property
    def can_manage(self) -> bool:
        """Broadcaster or moderator, from the server-supplied IRCv3 tags only (never from message text)."""
        return self.is_broadcaster or self.is_moderator


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
        text=text, badges=badges, mod_tag=tags.get("mod", "") == "1")


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

    def allow(self, user_id: str, now: Optional[float] = None, *, exempt: bool = False) -> bool:
        """``exempt`` (the broadcaster) is never limited and does not start a cooldown."""
        if exempt:
            return True
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


_LEADING_COMMAND_CHARS = re.compile(r"^[/.!\s]+")


def _no_command_start(text: str) -> str:
    """Strip leading ``/``, ``.``, ``!`` and whitespace so no chat message can start as a Twitch/bot command."""
    return _LEADING_COMMAND_CHARS.sub("", text)


def split_message(content: str, reply_user: Optional[str] = None, *,
                  limit: int = CHUNK_LIMIT, max_parts: int = MAX_PARTS) -> list:
    """Plain-text ``content`` into at most ``max_parts`` chat messages of <= ``limit`` chars.

    Every part is prefixed with ``@reply_user`` when given, and no part ever starts with ``/``, ``.`` or ``!``
    (model output must not be able to issue a chat or bot command). If the text does not fit, the last part is
    cut with an ellipsis. Returns [] for empty content.
    """
    text = _no_command_start(to_plain_text(content))
    if not text:
        return []
    prefix = f"@{reply_user} " if reply_user else ""
    room = limit - len(prefix)
    parts: list = []
    remaining = text
    while remaining and len(parts) < max_parts:
        if len(remaining) <= room:
            parts.append(remaining)
            remaining = ""
            break
        cut = remaining.rfind(" ", 0, room + 1)
        if cut < room // 3:
            cut = room
        parts.append(remaining[:cut].rstrip())
        remaining = _no_command_start(remaining[cut:])
    if remaining:  # truncated
        last = parts[-1]
        if len(last) > room - 1:
            last = last[: room - 1].rstrip()
        parts[-1] = last + "…"
    return [prefix + p for p in parts if p]


# -- Backseat Mode (chat votes) ----------------------------------------------

VOTE_COMMAND = "!vote"
BACKSEAT_COMMAND = "!backseat"
MIN_OPTIONS = 2
MAX_OPTIONS = 4
MAX_PROMPT_CHARS = 120
MAX_OPTION_CHARS = 60
BACKSEAT_USAGE = "Usage: !backseat <prompt> | <option 1> | <option 2> [| up to 4 options]"
VOTE_RUNNING_TEXT = "A vote is already running."
VOTE_CANCELLED_TEXT = "Vote cancelled."
NO_VOTES_TEXT = "No votes, nothing to do."


def _clean(text: str) -> str:
    return " ".join("".join(ch for ch in text if ch.isprintable() or ch.isspace()).split())


@dataclass
class BackseatCommand:
    kind: str                       # "start" | "cancel" | "invalid"
    prompt: str = ""
    options: list = field(default_factory=list)
    error: str = ""


def parse_backseat(text: str) -> Optional[BackseatCommand]:
    """Parse ``!backseat <prompt> | <opt> | <opt> ...``. None when ``text`` is not a !backseat command.

    This only parses syntax; whether the sender may start a vote is decided separately from IRC tags.
    """
    m = re.match(rf"{re.escape(BACKSEAT_COMMAND)}(?:\s+|$)", text.strip(), re.IGNORECASE)
    if not m:
        return None
    body = text.strip()[m.end():].strip()
    if body.lower() == "cancel":
        return BackseatCommand("cancel")
    pieces = [_clean(x) for x in body.split("|")]
    prompt, options = pieces[0], [x for x in pieces[1:] if x]
    if not prompt or len(options) < MIN_OPTIONS:
        return BackseatCommand("invalid", error=BACKSEAT_USAGE)
    if len(options) > MAX_OPTIONS:
        return BackseatCommand("invalid", error=f"At most {MAX_OPTIONS} options. {BACKSEAT_USAGE}")
    if len(prompt) > MAX_PROMPT_CHARS or any(len(o) > MAX_OPTION_CHARS for o in options):
        return BackseatCommand(
            "invalid", error=f"Keep the prompt under {MAX_PROMPT_CHARS} and each option under {MAX_OPTION_CHARS} characters.")
    return BackseatCommand("start", prompt=prompt, options=options)


def is_vote_command(text: str) -> bool:
    """True for any message starting with ``!vote`` as a whole word (such messages never reach the agent)."""
    return re.match(rf"{re.escape(VOTE_COMMAND)}(?:\s|$)", text.strip(), re.IGNORECASE) is not None


def parse_vote(text: str) -> Optional[int]:
    """The N of ``!vote N`` (1-based), or None when the message has no valid single number."""
    m = re.match(rf"{re.escape(VOTE_COMMAND)}\s+(\d{{1,3}})\s*$", text.strip(), re.IGNORECASE)
    return int(m.group(1)) if m else None


@dataclass
class VoteResult:
    winner: Optional[int]           # 1-based option number; None when there were no votes
    option: str
    votes: int                      # votes the winner got
    total: int
    tie: bool


class BackseatVote:
    """One running vote: a vote per user id (a later vote replaces an earlier one)."""

    def __init__(self, prompt: str, options: list, starter_id: str, starter_name: str, seconds: float):
        self.prompt = prompt
        self.options = list(options)
        self.starter_id = starter_id
        self.starter_name = starter_name
        self.seconds = seconds
        self._votes: dict = {}

    def cast(self, user_id: str, number: Optional[int]) -> bool:
        """Record a vote; False (and nothing recorded) for a missing or out-of-range number."""
        if number is None or not 1 <= number <= len(self.options):
            return False
        self._votes[user_id] = number
        return True

    def result(self) -> VoteResult:
        total = len(self._votes)
        if total == 0:
            return VoteResult(None, "", 0, 0, False)
        counts = {i: 0 for i in range(1, len(self.options) + 1)}
        for n in self._votes.values():
            counts[n] += 1
        top = max(counts.values())
        leaders = [i for i, c in counts.items() if c == top]
        winner = min(leaders)
        return VoteResult(winner, self.options[winner - 1], top, total, len(leaders) > 1)


def format_announcement(vote: BackseatVote) -> str:
    opts = " ".join(f"{i}) {o}" for i, o in enumerate(vote.options, 1))
    nums = ", ".join(f"{VOTE_COMMAND} {i}" for i in range(1, len(vote.options) + 1))
    return f"Backseat vote: {vote.prompt} {opts} Type {nums} ({int(vote.seconds)}s)"


def format_result(res: VoteResult) -> str:
    if res.winner is None:
        return NO_VOTES_TEXT
    tie = "tie, " if res.tie else ""
    return f"Chat picked: {res.option} ({tie}{res.votes} of {res.total} votes)"


def build_backseat_task(prompt: str, option: str) -> str:
    return (f'Chat voted in Backseat Mode. Prompt: "{prompt}". Winning choice: "{option}". '
            "Do this now and reply to chat in under 450 characters.")


# -- gateway notices that must not reach public chat ---------------------------

FRIENDLY_ERROR_TEXT = "Sorry, I can't answer right now."
_ERROR_LEADS = ("⚠", "⏱", "⛔")   # warning, stopwatch, no entry: a failed turn, shown as the friendly line
# gateway.shutdown.* / gateway.startup.*: lifecycle and maintenance, never about this viewer's question
_LIFECYCLE_SILENT = ("⚠ Hermes is shutting down", "⚠ Hermes is restarting", "⚠ Scheduled job", "⚠ Session database")


def _norm_notice(text: str) -> str:
    return " ".join((text or "").replace("️", "").split())


def classify_gateway_notice(text: str) -> Optional[str]:
    """"error" | "silent" | None (a normal answer) for text that arrives while a turn is pending.

    Prefix-anchored match against the English gateway templates in ``notices.py`` (derived from Hermes's
    ``locales/en.yaml``). Substrings and bare leading emoji never classify: a real answer that starts with
    an emoji, or that mentions a notice phrase, passes. Whether text may be posted AT ALL is decided
    structurally (a turn this adapter dispatched must be pending), not here.
    """
    t = _norm_notice(text)
    if not t:
        return None
    if t.startswith(notices.PLAIN_ERROR_PREFIXES):
        return "error"
    if t.startswith(notices.SYMBOL_PREFIXES):
        if t.startswith(_LIFECYCLE_SILENT):
            return "silent"
        return "error" if t.startswith(_ERROR_LEADS) else "silent"
    if t.startswith(notices.PLAIN_SILENT_PREFIXES):
        return "silent"
    return None
