"""Twitch chat platform adapter for Hermes Agent.

Stdlib only (asyncio + ssl + urllib). Joins one Twitch channel over IRC (TLS, irc.chat.twitch.tv:6697),
answers viewers who address the bot (``!ask <question>`` or ``@botname ...``) and posts the reply back
into chat. Modelled on the bundled IRC platform plugin.

Environment (scoped secrets, set through Hermes): TWITCH_TOKEN (secret), TWITCH_CHANNEL,
optional TWITCH_BOT_USERNAME, TWITCH_TRIGGER (default "!ask"), TWITCH_COOLDOWN_SECONDS (default 20),
TWITCH_ALLOWED_USERS, TWITCH_ALLOW_ALL_USERS (default true).

Security: Twitch viewers are anonymous strangers. Turns that originate from Twitch get only the
``search`` toolset (see ``TwitchAdapter.toolsets_for_source``) and cannot run gateway slash commands.
"""

import asyncio
import contextlib
import datetime
import json
import logging
import ssl
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from typing import Any, Optional

from gateway.config import Platform
from gateway.platforms._shared import get_scoped_secret as _get_scoped_secret, seed_extra_from_env as _seed_extra_from_env
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms.helpers import cancel_task

from .protocol import (
    MIN_SEND_INTERVAL, Cooldown, SentTracker, extract_question, is_auth_failure,
    parse_line, parse_privmsg, split_message,
)

logger = logging.getLogger(__name__)

IRC_HOST = "irc.chat.twitch.tv"
IRC_PORT = 6697
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
SAFE_TOOLSETS = ["search", "no_mcp"]  # web_search only; "no_mcp" also blocks every configured MCP server
_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


def _clean_token(raw: str) -> str:
    token = (raw or "").strip()
    return token[6:] if token.lower().startswith("oauth:") else token


def _clean_channel(raw: str) -> str:
    return (raw or "").strip().lstrip("#").lower()


def _validate_token_sync(token: str) -> tuple:
    """Return (status, login). status: "ok" | "invalid" | "unknown" (network problem; do not block)."""
    req = urllib.request.Request(VALIDATE_URL, headers={"Authorization": f"OAuth {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return "ok", str(data.get("login") or "").lower()
    except urllib.error.HTTPError as e:
        return ("invalid" if e.code == 401 else "unknown"), ""
    except Exception:
        return "unknown", ""


class TwitchAdapter(BasePlatformAdapter):
    """Async Twitch IRC adapter implementing the BasePlatformAdapter interface."""

    serial_receive_loop = True

    def __init__(self, config, **kwargs):
        super().__init__(config=config, platform=Platform("twitch"))
        extra = getattr(config, "extra", {}) or {}
        self.channel = _clean_channel(_get_scoped_secret("TWITCH_CHANNEL", "") or extra.get("channel", ""))
        self.bot_login = (_get_scoped_secret("TWITCH_BOT_USERNAME", "") or extra.get("bot_username", "") or "").strip().lower()
        self.trigger = (_get_scoped_secret("TWITCH_TRIGGER", "") or extra.get("trigger") or "!ask").strip()
        try:
            cooldown = float(_get_scoped_secret("TWITCH_COOLDOWN_SECONDS", "") or extra.get("cooldown_seconds", 20))
        except (TypeError, ValueError):
            cooldown = 20.0
        self._cooldown = Cooldown(cooldown)
        self._sent = SentTracker()
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._welcome = asyncio.Event()
        self._auth_failed = False
        self._joined = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._last_send = 0.0
        self._senders: "OrderedDict[str, tuple]" = OrderedDict()  # message id -> (display name, is_broadcaster)

    @property
    def name(self) -> str:
        return "Twitch"

    def _fail(self, code: str, message: str, *, retryable: bool) -> bool:
        self._set_fatal_error(code, message, retryable=retryable)
        return False

    # -- lifecycle ----------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        token = _clean_token(_get_scoped_secret("TWITCH_TOKEN", ""))
        if not token or not self.channel:
            logger.error("Twitch: TWITCH_TOKEN and TWITCH_CHANNEL must be set")
            return self._fail("config_missing", "TWITCH_TOKEN and TWITCH_CHANNEL must be set", retryable=False)
        status, login = await asyncio.to_thread(_validate_token_sync, token)
        if status == "invalid":
            logger.error("Twitch: token rejected by Twitch token validation (expired or revoked); not connecting")
            return self._fail("auth_failed", "Twitch rejected TWITCH_TOKEN", retryable=False)
        if not self.bot_login:
            self.bot_login = login or self.channel
        if not self._acquire_platform_lock("twitch", f"{self.channel}:{self.bot_login}", f"Twitch {self.bot_login}@{self.channel}"):
            return False
        self._welcome.clear()
        self._joined.clear()
        self._auth_failed = False
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(IRC_HOST, IRC_PORT, ssl=ssl.create_default_context()), timeout=30.0)
        except Exception as e:
            logger.error("Twitch: failed to connect to %s:%s: %s", IRC_HOST, IRC_PORT, e)
            return self._fail("connect_failed", str(e), retryable=True)
        await self._send_raw(f"PASS oauth:{token}", secret=True)
        await self._send_raw(f"NICK {self.bot_login}")
        await self._send_raw("CAP REQ :twitch.tv/tags twitch.tv/commands")
        self._recv_task = asyncio.create_task(self._receive_loop())
        try:
            await asyncio.wait_for(self._welcome.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            logger.error("Twitch: login timed out (no welcome from server)")
            await self.disconnect()
            return self._fail("registration_timeout", "Twitch did not welcome the connection", retryable=True)
        if self._auth_failed:
            logger.error("Twitch: login authentication failed (check TWITCH_TOKEN and TWITCH_BOT_USERNAME); not connecting")
            await self.disconnect()
            return self._fail("auth_failed", "Twitch login authentication failed", retryable=False)
        await self._send_raw(f"JOIN #{self.channel}")
        try:
            await asyncio.wait_for(self._joined.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("Twitch: no JOIN confirmation for #%s within 10s; continuing", self.channel)
        self._mark_connected()
        logger.info("Twitch: connected to %s:%s as %s, joined #%s (trigger %r)",
                    IRC_HOST, IRC_PORT, self.bot_login, self.channel, self.trigger)
        self._wire_plugin_handlers(None)
        return True

    async def disconnect(self) -> None:
        with contextlib.suppress(Exception):
            self._release_platform_lock()
        was_connected = self.is_connected
        self._mark_disconnected()
        if self._writer and not self._writer.is_closing():
            with contextlib.suppress(Exception):
                await self._send_raw(f"PART #{self.channel}")
            with contextlib.suppress(Exception):
                self._writer.close()
                await asyncio.wait_for(self._writer.wait_closed(), timeout=5.0)
        await cancel_task(self._recv_task)
        self._reader = None
        self._writer = None
        self._recv_task = None
        if was_connected:
            logger.info("Twitch: disconnected from #%s", self.channel)

    # -- outbound -----------------------------------------------------------

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None) -> SendResult:
        if not self._writer or self._writer.is_closing():
            return SendResult(success=False, error="Not connected", retryable=True)
        target = _clean_channel(chat_id) or self.channel
        user = self._senders.get(str(reply_to), (None, False))[0] if reply_to else None
        if not user and metadata:
            user = metadata.get("user_name") or None
        parts = split_message(content, user)
        if not parts:
            return SendResult(success=False, error="Empty message")
        last_id = None
        for part in parts:
            try:
                async with self._send_lock:
                    wait = MIN_SEND_INTERVAL - (time.monotonic() - self._last_send)
                    if wait > 0:
                        await asyncio.sleep(wait)
                    self._sent.remember(part)
                    await self._send_raw(f"PRIVMSG #{target} :{part}")
                    self._last_send = time.monotonic()
                last_id = str(int(time.time() * 1000))
            except Exception as e:
                return SendResult(success=False, error=str(e), retryable=True)
        return SendResult(success=True, message_id=last_id)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Twitch has no typing indicator."""

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": _clean_channel(chat_id) or self.channel, "type": "group"}

    def toolsets_for_source(self, source) -> Optional[list]:
        """SECURITY BOUNDARY: every Twitch-originated turn gets web search only (replaces
        ``platform_toolsets.twitch`` / the implicit ``hermes-twitch`` core bundle)."""
        return list(SAFE_TOOLSETS)

    async def _send_raw(self, line: str, *, secret: bool = False) -> None:
        if not self._writer or self._writer.is_closing():
            return
        line = line.replace("\r", " ").replace("\n", " ")
        self._writer.write((line + "\r\n").encode("utf-8"))
        await self._writer.drain()

    # -- inbound ------------------------------------------------------------

    async def _receive_loop(self) -> None:
        buffer = b""
        try:
            while self._reader and not self._reader.at_eof():
                data = await self._reader.read(4096)
                if not data:
                    break
                buffer += data
                while b"\r\n" in buffer:
                    line, buffer = buffer.split(b"\r\n", 1)
                    try:
                        await self._handle_line(line.decode("utf-8", errors="replace"))
                    except Exception as e:
                        logger.warning("Twitch: error handling line: %s", e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Twitch: receive loop error: %s", e)
        finally:
            self._welcome.set()  # unblock connect() if the socket died during login
            if self.is_connected:
                logger.warning("Twitch: connection lost, marking disconnected")
                self._set_fatal_error("connection_lost", "Twitch connection closed unexpectedly", retryable=True)
                await self._notify_fatal_error()

    async def _handle_line(self, raw: str) -> None:
        msg = parse_line(raw)
        command, params = msg["command"], msg["params"]
        if command == "PING":
            await self._send_raw(f"PONG :{params[0] if params else 'tmi.twitch.tv'}")
        elif command == "001":
            self._welcome.set()
        elif command == "NOTICE" and is_auth_failure(msg):
            self._auth_failed = True
            self._welcome.set()
        elif command == "JOIN" and msg["prefix"].split("!")[0].lower() == self.bot_login:
            self._joined.set()
        elif command == "RECONNECT":
            logger.warning("Twitch: server requested reconnect")
            with contextlib.suppress(Exception):
                self._writer.close()
        elif command == "PRIVMSG":
            chat = parse_privmsg(msg)
            if chat is not None:
                await self._handle_chat(chat)

    async def _handle_chat(self, chat) -> None:
        if chat.channel != self.channel:
            return
        if chat.login == self.bot_login and self._sent.is_own(chat.text):
            return  # something this adapter posted itself
        question = extract_question(chat.text, self.trigger, self.bot_login)
        if question is None:
            return  # not addressed to the bot
        if not self._cooldown.allow(chat.user_id):
            logger.debug("Twitch: %s is on cooldown, ignoring", chat.login)
            return
        if not self._message_handler:
            return
        if chat.message_id:
            # Hermes' native admission refuses event.metadata from plugin platforms, so per-message
            # sender facts (display name, broadcaster badge) are kept here, keyed by Twitch message id.
            self._senders[chat.message_id] = (chat.display_name, chat.is_broadcaster)
            while len(self._senders) > 200:
                self._senders.popitem(last=False)
        source = self.build_source(
            chat_id=chat.channel, chat_name=chat.channel, chat_type="group",
            user_id=chat.user_id, user_name=chat.display_name, message_id=chat.message_id or None)
        logger.info("Twitch: dispatching message from %s (%d chars)", chat.display_name, len(question))
        await self.handle_message(MessageEvent(
            text=question, message_type=MessageType.TEXT, source=source,
            message_id=chat.message_id or None, timestamp=datetime.datetime.now(),
            reply_expected=True, allow_gateway_control=False))  # viewers cannot run /commands


# -- Plugin registration ------------------------------------------------------

def check_requirements() -> bool:
    return bool(_get_scoped_secret("TWITCH_TOKEN", "") and _get_scoped_secret("TWITCH_CHANNEL", ""))


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    return bool(_get_scoped_secret("TWITCH_TOKEN", "") and (_get_scoped_secret("TWITCH_CHANNEL", "") or extra.get("channel")))


def is_connected(config) -> bool:
    return validate_config(config)


def _env_enablement() -> Optional[dict]:
    """Seed ``PlatformConfig.extra`` from env before adapter construction (None = not configured).

    Viewers are allowed to talk to the bot by default (``allowed_users: ["*"]``) because the Twitch
    toolset is locked to web search; set TWITCH_ALLOW_ALL_USERS=false (and TWITCH_ALLOWED_USERS) to restrict.
    """
    token = str(_get_scoped_secret("TWITCH_TOKEN", "") or "").strip()
    channel = _clean_channel(str(_get_scoped_secret("TWITCH_CHANNEL", "") or ""))
    if not (token and channel):
        return None
    seed = _seed_extra_from_env((
        ("TWITCH_BOT_USERNAME", "bot_username", None), ("TWITCH_TRIGGER", "trigger", None),
        ("TWITCH_COOLDOWN_SECONDS", "cooldown_seconds", float),
    ), home_env="TWITCH_HOME_CHANNEL", home_default=channel)  # home = the channel, so the gateway never posts its "set a home channel" nag into public chat
    allow_all = str(_get_scoped_secret("TWITCH_ALLOW_ALL_USERS", "") or "").strip().lower()
    extra = {"channel": channel, **seed}
    if allow_all not in _FALSY:
        extra["allowed_users"] = ["*"]
    return extra


def register(ctx):
    ctx.register_platform(
        name="twitch",
        label="Twitch",
        adapter_factory=TwitchAdapter,
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["TWITCH_TOKEN", "TWITCH_CHANNEL"],
        install_hint="No extra packages needed (stdlib only)",
        env_enablement_fn=_env_enablement,
        allowed_users_env="TWITCH_ALLOWED_USERS",
        allow_all_env="TWITCH_ALLOW_ALL_USERS",
        max_message_length=1440,
        display_tier="low",  # no message editing: keep tool-progress / interim chatter out of public chat
        emoji="🎮",
        pii_safe=True,
        allow_update_command=False,
        platform_hint=(
            "You are the co-host in a live Twitch chat, answering viewers who address you. Viewers are "
            "anonymous strangers. Reply in short plain text, at most 450 characters, with no markdown, "
            "no code blocks and no lists. Be friendly and concise."))
