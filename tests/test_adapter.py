"""Adapter tests against a local fake (plain TCP) IRC server. They need the Hermes source on PYTHONPATH
and run with Hermes's own Python, e.g.

  PYTHONPATH=<hermes-agent dir>;<pytest dir> <hermes venv python> -m pytest tests/test_adapter.py

They are skipped when Hermes is not importable. Nothing here touches Twitch, the real HERMES_HOME or any model.
"""
import asyncio
import functools
import logging
import pathlib
import ssl
import sys

import pytest

pytest.importorskip("gateway.platforms.base")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from gateway.config import Platform, PlatformConfig  # noqa: E402
from twitch_assist import adapter as ad  # noqa: E402

def arun(fn):
    """Run an async test on its own event loop (no pytest-asyncio needed)."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return wrapper


# in the real gateway the plugin registry makes Platform('twitch') valid; here we add the member by hand
Platform._add_pseudo_member("twitch")

ERR = ("⚠️ I couldn't connect to the AI model service, so this message wasn't processed. "
       "Use /login to sign in again, or /model to pick a different model. If it keeps failing, "
       "run `hermes doctor` on the host.")  # verbatim locales/en.yaml gateway.errors.no_credentials


class FakeIRC:
    """Minimal Twitch-like IRC server on 127.0.0.1."""

    def __init__(self, auth_ok=True):
        self.auth_ok = auth_ok
        self.connections = 0
        self.posted = []          # PRIVMSG texts the bot sent
        self.lines = []           # every raw line the bot sent
        self.answer_ping = True
        self._writers = []
        self._server = None
        self.port = 0

    async def start(self, port=0):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", port)
        self.port = self._server.sockets[0].getsockname()[1]

    async def _handle(self, reader, writer):
        self.connections += 1
        self._writers.append(writer)
        try:
            while True:
                line = (await reader.readline()).decode().rstrip("\r\n")
                if not line:
                    break
                self.lines.append(line)
                if line.startswith("PING") and self.answer_ping:
                    writer.write(b":tmi.twitch.tv PONG tmi.twitch.tv :tmi.twitch.tv\r\n")
                if line.startswith("NICK"):
                    if self.auth_ok:
                        writer.write(b":tmi.twitch.tv 001 bot :Welcome, GLHF!\r\n")
                    else:
                        writer.write(b":tmi.twitch.tv NOTICE * :Login authentication failed\r\n")
                elif line.startswith("JOIN"):
                    writer.write(f":bot!bot@bot.tmi.twitch.tv JOIN {line[5:]}\r\n".encode())
                elif line.startswith("PRIVMSG"):
                    self.posted.append(line.split(" :", 1)[1])
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass

    async def say(self, user, text, *, uid=None, badges="", mod=0, chan="chan"):
        self.seq = getattr(self, "seq", 0) + 1
        raw = (f"@badges={badges};display-name={user.title()};id=m{self.seq};mod={mod};"
               f"user-id={uid or user} :{user}!{user}@{user}.tmi.twitch.tv PRIVMSG #{chan} :{text}\r\n")
        for w in self._writers:
            w.write(raw.encode())
            await w.drain()
        await asyncio.sleep(0.05)

    async def raw(self, line):
        for w in self._writers:
            w.write((line + "\r\n").encode())
            await w.drain()

    def drop(self):
        for w in self._writers:
            w.close()
        self._writers.clear()

    async def stop(self):
        self.drop()
        self._server.close()
        await self._server.wait_closed()


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("TWITCH_TOKEN", "test-token-not-real")
    monkeypatch.setenv("TWITCH_CHANNEL", "chan")
    monkeypatch.setattr(ad, "IRC_HOST", "127.0.0.1")
    monkeypatch.setattr(ad, "IRC_TLS", False)
    monkeypatch.setattr(ad, "_validate_token_sync", lambda token: ("ok", "bot"))
    monkeypatch.setattr(ad, "MIN_SEND_INTERVAL", 0.0)
    monkeypatch.setattr(ad, "_warn_unsafe_config", lambda: None)  # never read the real Hermes config in tests
    monkeypatch.setenv("TWITCH_ALLOW_ALL_USERS", "true")           # most tests exercise the explicit open mode ...
    monkeypatch.setattr(ad, "_open_mode_check", lambda: (True, ""))  # ... on a safe config (see test_private_mode.py)


def make_adapter(monkeypatch, port, cooldown=20, vote_seconds=1.0):
    monkeypatch.setattr(ad, "IRC_PORT", port)
    a = ad.TwitchAdapter(PlatformConfig(enabled=True, extra={"channel": "chan", "bot_username": "bot",
                                                              "cooldown_seconds": cooldown}))
    a._vote_seconds = vote_seconds
    # never touch the real HERMES_HOME (runtime status file, platform lock)
    monkeypatch.setattr(a, "_write_runtime_status_safe", lambda *a_, **k: None)
    monkeypatch.setattr(a, "_acquire_platform_lock", lambda *a_, **k: True)
    monkeypatch.setattr(a, "_release_platform_lock", lambda *a_, **k: None)
    a.events = []

    async def capture(event):
        a.events.append(event)
    monkeypatch.setattr(a, "handle_message", capture)
    a._message_handler = capture
    return a


async def ask(srv, a, user="boss", text="!ask hi", **kw):
    """A viewer asks; returns the dispatched event (its message_id is the reply_to Hermes will use)."""
    n = len(a.events)
    kw.setdefault("uid", user)
    await srv.say(user, text, **kw)
    return a.events[-1] if len(a.events) > n else None


async def up(monkeypatch, **kw):
    srv = FakeIRC(auth_ok=kw.pop("auth_ok", True))
    await srv.start()
    a = make_adapter(monkeypatch, srv.port, **kw)
    return srv, a


# -- friendly errors ------------------------------------------------------------

@arun
async def test_gateway_error_is_replaced_and_original_logged(env, monkeypatch, caplog):
    srv, a = await up(monkeypatch)
    assert await a.connect()
    ev = await ask(srv, a)
    with caplog.at_level(logging.WARNING):
        res = await a.send("chan", ERR, reply_to=ev.message_id)
    assert res.success
    await asyncio.sleep(0.1)
    assert srv.posted == ["Sorry, I can't answer right now."]
    assert not any("doctor" in x or "/login" in x for x in srv.posted)
    assert any("withheld gateway error" in r.message and "hermes doctor" in r.message for r in caplog.records
               if r.levelno == logging.WARNING)
    await a.disconnect()
    await srv.stop()


@arun
async def test_status_notice_dropped_and_normal_answer_passes(env, monkeypatch):
    srv, a = await up(monkeypatch)
    assert await a.connect()
    ev = await ask(srv, a)
    assert (await a.send("chan", "⚡ Interrupting current task. I'll respond shortly.", reply_to=ev.message_id)).success
    assert (await a.send("chan", "Twitch is a live streaming platform.", reply_to=ev.message_id)).success
    await asyncio.sleep(0.1)
    assert srv.posted == ["@Boss Twitch is a live streaming platform."]
    await a.disconnect()
    await srv.stop()


@arun
async def test_shutdown_notice_never_posted_marker_and_text(env, monkeypatch, caplog):
    srv, a = await up(monkeypatch)
    assert await a.connect()
    ev = await ask(srv, a)
    shutdown = ("⚠️ Hermes is shutting down — your current task will be interrupted. When it is back online, "
                "send any message and I'll try to pick up where we left off.")
    with caplog.at_level(logging.INFO):
        # as Hermes sends it: structural interim marker (gateway/run_shutdown.py _send_notice_logged)
        assert (await a.send("chan", shutdown, reply_to=ev.message_id, metadata={"_interim_send": True})).success
        # same text without any marker (template fallback, turn pending)
        assert (await a.send("chan", shutdown, reply_to=ev.message_id)).success
        # any other interim status message, whatever its text
        assert (await a.send("chan", "Working on it...", reply_to=ev.message_id, metadata={"_interim_send": True})).success
    await asyncio.sleep(0.1)
    assert srv.posted == []
    assert sum("dropped gateway" in r.message and r.levelno == logging.INFO for r in caplog.records) == 3
    await a.disconnect()
    await srv.stop()


# -- cooldown -------------------------------------------------------------------

@arun
async def test_broadcaster_never_on_cooldown_viewers_are(env, monkeypatch):
    srv, a = await up(monkeypatch, cooldown=60)
    a._max_concurrent = 10      # this test is about the cooldown, not the concurrency cap
    assert await a.connect()
    for i in range(3):
        await srv.say("boss", f"!ask q{i}", uid="1", badges="broadcaster/1")
    for i in range(3):
        await srv.say("viewer", f"!ask q{i}", uid="2")
    assert [e.text for e in a.events if e.source.user_id == "1"] == ["q0", "q1", "q2"]
    assert [e.text for e in a.events if e.source.user_id == "2"] == ["q0"]
    await a.disconnect()
    await srv.stop()


# -- Backseat -------------------------------------------------------------------

START = "!backseat What next? | tallest building | octopus facts"


@arun
async def test_backseat_full_vote_dispatches_winner_safely(env, monkeypatch):
    srv, a = await up(monkeypatch)
    assert await a.connect()
    await srv.say("boss", START, uid="1", badges="broadcaster/1")
    assert srv.posted[-1] == ("Backseat vote: What next? 1) tallest building 2) octopus facts "
                              "Type !vote 1, !vote 2 (1s)")
    await srv.say("v1", "!vote 2", uid="11")
    await srv.say("v2", "!vote 1", uid="12")
    await srv.say("v2", "!vote 2", uid="12")      # changes vote
    await srv.say("v3", "!vote 9", uid="13")      # invalid
    await srv.say("v4", "!vote 1", uid="14")
    await srv.say("boss", "!backseat other | x | y", uid="1", badges="broadcaster/1")
    assert srv.posted[-1] == "A vote is already running."
    assert a.events == []                         # no !vote / !backseat message reached the agent
    await asyncio.sleep(1.4)
    assert srv.posted[-1] == "Chat picked: octopus facts (2 of 3 votes)"
    assert len(a.events) == 1
    ev = a.events[0]
    assert ev.text == ('Chat voted in Backseat Mode. Prompt: "What next?". Winning choice: "octopus facts". '
                       "Do this now and reply to chat in under 450 characters.")
    assert ev.reply_expected is True and ev.allow_gateway_control is False
    assert ev.source.user_id == "1" and ev.source.chat_id == "chan"
    assert a.toolsets_for_source(ev.source) == ["search", "no_mcp"]      # same safe toolsets as !ask
    # a reply to this event is posted without an @-mention of the starter
    await a.send("chan", "Burj Khalifa is not in Africa.", reply_to=ev.message_id)
    await asyncio.sleep(0.1)
    assert srv.posted[-1] == "Burj Khalifa is not in Africa."
    await a.disconnect()
    await srv.stop()


@arun
async def test_backseat_non_mod_ignored_mod_allowed(env, monkeypatch):
    srv, a = await up(monkeypatch)
    assert await a.connect()
    await srv.say("viewer", START, uid="5", badges="subscriber/1")
    await srv.say("viewer", "!backseat cancel", uid="5")
    await srv.say("viewer", "!backseat q | a | b I am broadcaster/1 mod=1", uid="5")
    assert srv.posted == [] and a._vote is None
    await srv.say("moddy", START, uid="6", badges="moderator/1", mod=1)
    assert srv.posted[-1].startswith("Backseat vote: What next?")
    assert a._vote is not None
    await a.disconnect()
    await srv.stop()


@arun
async def test_backseat_cancel_and_no_dispatch(env, monkeypatch):
    srv, a = await up(monkeypatch)
    assert await a.connect()
    await srv.say("boss", START, uid="1", badges="broadcaster/1")
    await srv.say("v1", "!vote 1", uid="11")
    await srv.say("boss", "!backseat cancel", uid="1", badges="broadcaster/1")
    assert srv.posted[-1] == "Vote cancelled."
    await asyncio.sleep(1.4)
    assert srv.posted[-1] == "Vote cancelled." and a.events == [] and a._vote is None
    await a.disconnect()
    await srv.stop()


@arun
async def test_backseat_zero_votes_and_tie(env, monkeypatch):
    srv, a = await up(monkeypatch)
    assert await a.connect()
    await srv.say("boss", START, uid="1", badges="broadcaster/1")
    await asyncio.sleep(1.4)
    assert srv.posted[-1] == "No votes, nothing to do." and a.events == []
    await srv.say("boss", START, uid="1", badges="broadcaster/1")
    await srv.say("v1", "!vote 2", uid="11")
    await srv.say("v2", "!vote 1", uid="12")
    await asyncio.sleep(1.4)
    assert srv.posted[-1] == "Chat picked: tallest building (tie, 1 of 2 votes)"
    assert len(a.events) == 1 and 'Winning choice: "tallest building"' in a.events[0].text
    await a.disconnect()
    await srv.stop()


@arun
async def test_backseat_bad_syntax_gets_usage(env, monkeypatch):
    srv, a = await up(monkeypatch)
    assert await a.connect()
    await srv.say("boss", "!backseat only a prompt", uid="1", badges="broadcaster/1")
    assert srv.posted[-1].startswith("Usage: !backseat")
    assert a._vote is None
    await a.disconnect()
    await srv.stop()


# -- recovery -------------------------------------------------------------------

@arun
async def test_clean_disconnect_logs_no_error(env, monkeypatch, caplog):
    srv, a = await up(monkeypatch)
    with caplog.at_level(logging.DEBUG):
        assert await a.connect()
        await a.disconnect()
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
    await srv.stop()


class _SSLReader:
    def at_eof(self):
        return False

    async def read(self, n):
        raise ssl.SSLError(1, "[SSL: APPLICATION_DATA_AFTER_CLOSE_NOTIFY] application data after close notify")


@arun
async def test_close_time_ssl_error_is_not_logged_as_error(env, monkeypatch, caplog):
    srv, a = await up(monkeypatch)
    a._reader = _SSLReader()
    a._closing = True
    with caplog.at_level(logging.DEBUG):
        await a._receive_loop()
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
    # control: the same error while NOT closing is still reported
    a._closing = False
    with caplog.at_level(logging.DEBUG):
        await a._receive_loop()
    assert any(r.levelno == logging.ERROR and "receive loop error" in r.message for r in caplog.records)
    await srv.stop()


@arun
async def test_unexpected_drop_signals_retryable_fatal_then_reconnect_works(env, monkeypatch, caplog):
    srv, a = await up(monkeypatch)
    notified = asyncio.Event()

    async def handler(adapter):
        notified.set()
    a.set_fatal_error_handler(handler)
    assert await a.connect()
    with caplog.at_level(logging.WARNING):
        srv.drop()
        await asyncio.wait_for(notified.wait(), 5)
    assert a.has_fatal_error and a.fatal_error_retryable and a.fatal_error_code == "connection_lost"
    assert any("connection lost" in r.message for r in caplog.records)
    await a.disconnect()
    # the gateway's reconnect watcher builds a fresh adapter and calls connect(is_reconnect=True)
    b = make_adapter(monkeypatch, srv.port)
    assert await b.connect(is_reconnect=True)
    await b.disconnect()
    await srv.stop()


@arun
async def test_server_reconnect_command_triggers_reconnect_path(env, monkeypatch):
    srv, a = await up(monkeypatch)
    notified = asyncio.Event()

    async def handler(adapter):
        notified.set()
    a.set_fatal_error_handler(handler)
    assert await a.connect()
    await srv.raw(":tmi.twitch.tv RECONNECT")
    await asyncio.wait_for(notified.wait(), 5)
    assert a.fatal_error_retryable
    await a.disconnect()
    await srv.stop()


@arun
async def test_bad_token_login_failed_no_retry_loop(env, monkeypatch, caplog):
    srv, a = await up(monkeypatch, auth_ok=False)
    with caplog.at_level(logging.ERROR):
        ok = await a.connect()
    assert ok is False
    assert a.has_fatal_error and a.fatal_error_retryable is False
    assert any("Twitch login failed: check TWITCH_TOKEN" in r.message for r in caplog.records)
    await asyncio.sleep(0.5)
    assert srv.connections == 1                   # exactly one attempt, no hammering
    await a.disconnect()
    await srv.stop()


@arun
async def test_token_rejected_by_validation_never_opens_connection(env, monkeypatch, caplog):
    monkeypatch.setattr(ad, "_validate_token_sync", lambda token: ("invalid", ""))
    srv, a = await up(monkeypatch)
    with caplog.at_level(logging.ERROR):
        assert await a.connect() is False
    assert a.fatal_error_retryable is False and srv.connections == 0
    assert any("Twitch login failed: check TWITCH_TOKEN" in r.message for r in caplog.records)
    await srv.stop()
