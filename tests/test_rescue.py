"""Release-rescue tests: cost/flood control, public-chat truth, liveness, output hygiene, allowlist, vote races.

Adapter-level tests run against the same local fake IRC server as ``test_adapter.py`` (needs Hermes importable).
"""
import asyncio
import logging
import pathlib
import sys

import pytest

pytest.importorskip("gateway.platforms.base")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_adapter import arun, ask, env, make_adapter, up  # noqa: E402,F401
from twitch_assist import adapter as ad  # noqa: E402
from twitch_assist import protocol as pr  # noqa: E402

START = "!backseat What next? | tallest building | octopus facts"
BOSS = dict(uid="1", badges="broadcaster/1")


# -- WP3: concurrency cap, rate, stale ------------------------------------------------------

@arun
async def test_concurrency_cap_limits_parallel_viewers_and_slot_frees_after_reply(env, monkeypatch, caplog):
    srv, a = await up(monkeypatch)
    assert a._max_concurrent == 3                       # default
    assert await a.connect()
    with caplog.at_level(logging.INFO):
        evs = [await ask(srv, a, f"v{i}", "!ask hello") for i in range(6)]
    assert [e is not None for e in evs] == [True, True, True, False, False, False]   # at most the cap is dispatched
    assert any("TWITCH_MAX_CONCURRENT" in r.message and r.levelno == logging.INFO for r in caplog.records)
    assert srv.posted == []                              # dropped silently: nothing posted
    # one answer goes out: its slot frees and the next viewer gets through
    await a.send("chan", "Answer one.", reply_to=evs[0].message_id)
    assert (await ask(srv, a, "v7", "!ask hello")) is not None
    assert (await ask(srv, a, "v8", "!ask hello")) is None
    await a.disconnect()
    await srv.stop()


@arun
async def test_broadcaster_exempt_from_cap(env, monkeypatch):
    srv, a = await up(monkeypatch)
    a._max_concurrent = 1
    assert await a.connect()
    assert await ask(srv, a, "v1", "!ask a") is not None
    assert await ask(srv, a, "v2", "!ask b") is None
    assert await ask(srv, a, "boss", "!ask c", **BOSS) is not None
    assert await ask(srv, a, "boss", "!ask d", **BOSS) is not None
    await a.disconnect()
    await srv.stop()


@arun
async def test_max_concurrent_from_env(env, monkeypatch):
    srv, a = await up(monkeypatch)
    monkeypatch.setenv("TWITCH_MAX_CONCURRENT", "2")
    assert ad.TwitchAdapter(a.config)._max_concurrent == 2
    monkeypatch.setenv("TWITCH_MAX_CONCURRENT", "junk")
    assert ad.TwitchAdapter(a.config)._max_concurrent == 3
    await srv.stop()


@arun
async def test_stale_reply_dropped_and_timeout_frees_slot(env, monkeypatch, caplog):
    monkeypatch.setattr(ad, "TURN_TIMEOUT", 0.3)
    srv, a = await up(monkeypatch)
    a._max_concurrent = 1
    assert await a.connect()
    ev = await ask(srv, a, "v1", "!ask slow one")
    assert await ask(srv, a, "v2", "!ask other") is None      # slot held
    await asyncio.sleep(0.4)                                   # the turn is now stale
    with caplog.at_level(logging.INFO):
        res = await a.send("chan", "A very late answer.", reply_to=ev.message_id)
    assert res.success and srv.posted == []                    # dropped, not posted
    assert any("no pending turn" in r.message for r in caplog.records)
    assert await ask(srv, a, "v3", "!ask again") is not None   # timeout freed the slot
    await a.disconnect()
    await srv.stop()


def test_min_send_interval_is_twitch_safe():
    assert pr.MIN_SEND_INTERVAL >= 1.5          # 20 messages per 30 s for a non-mod account


@arun
async def test_send_rate_limit_spaces_parts(env, monkeypatch):
    srv, a = await up(monkeypatch)
    monkeypatch.setattr(ad, "MIN_SEND_INTERVAL", 0.25)
    assert await a.connect()
    ev = await ask(srv, a)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await a.send("chan", "word " * 200, reply_to=ev.message_id)   # three parts
    assert loop.time() - t0 >= 0.45
    await a.disconnect()
    await srv.stop()


# -- WP4: public-chat truth -----------------------------------------------------------------

AUTO_RESET = ("◐ Session reset after being stopped. Conversation history cleared.\nUse /resume to browse and "
              "restore a previous session.\n\n\n◆ Model: `deepseek-v4-flash`\n◆ Provider: deepseek\n"
              "◆ Endpoint: http://127.0.0.1:8080/v1")
CRON_OK = "✅ Cron store /home/x/.hermes/cron is writable again; 2 skipped run(s), catching up once per job."
ONLINE = "♻️ Gateway online — Hermes is back and ready."
SETHOME = ("📬 No home channel is set for Twitch. A home channel is where Hermes delivers cron job results and "
           "cross-platform messages.\n\nType /sethome to make this chat your home channel, or ignore to skip.")


@arun
async def test_nothing_posted_without_a_pending_turn(env, monkeypatch):
    srv, a = await up(monkeypatch)
    assert await a.connect()
    for text in (ONLINE, CRON_OK, SETHOME, AUTO_RESET, "Daily digest: it is sunny.", "Real looking answer"):
        assert (await a.send("chan", text)).success
        assert (await a.send("chan", text, reply_to="never-dispatched")).success
    await asyncio.sleep(0.1)
    assert srv.posted == []
    await a.disconnect()
    await srv.stop()


@arun
async def test_notice_templates_dropped_inside_a_turn(env, monkeypatch):
    srv, a = await up(monkeypatch)
    assert await a.connect()
    ev = await ask(srv, a)
    for text in (AUTO_RESET, CRON_OK, ONLINE, SETHOME):
        assert (await a.send("chan", text)).success             # no reply_to, like the gateway's notices
    await asyncio.sleep(0.1)
    assert srv.posted == []
    assert (await a.send("chan", "It is sunny.", reply_to=ev.message_id)).success
    await asyncio.sleep(0.1)
    assert srv.posted == ["@Boss It is sunny."]
    await a.disconnect()
    await srv.stop()


@arun
async def test_real_answers_with_emoji_or_notice_phrases_pass(env, monkeypatch):
    srv, a = await up(monkeypatch)
    a._max_concurrent = 20
    assert await a.connect()
    answers = ["❓ Great question, the answer is 42.", "💬 Chat is lively today.", "⚡ Lightning is hotter than the sun.",
               "▶ Press play to start the game.", "Somebody said hermes is restarting, but that is just a myth.",
               "⚠️ Careful: that link looks suspicious."]
    for i, text in enumerate(answers):
        ev = await ask(srv, a, f"u{i}", "!ask q")
        await a.send("chan", text, reply_to=ev.message_id)
    await asyncio.sleep(0.2)
    assert srv.posted == [f"@U{i} {t}" for i, t in enumerate(answers)]
    await a.disconnect()
    await srv.stop()


@arun
async def test_private_platform_notice_is_never_posted(env, monkeypatch):
    srv, a = await up(monkeypatch)
    assert await a.connect()
    await ask(srv, a)
    res = await a.send_private_notice("chan", "1", SETHOME)
    assert res.success
    await asyncio.sleep(0.1)
    assert srv.posted == []
    await a.disconnect()
    await srv.stop()


def test_env_seed_private_notices_and_no_home_channel(monkeypatch):
    monkeypatch.setenv("TWITCH_TOKEN", "t")
    monkeypatch.setenv("TWITCH_CHANNEL", "#Chan")
    monkeypatch.setenv("TWITCH_MAX_CONCURRENT", "5")
    seed = ad._env_enablement()
    assert seed["notice_delivery"] == "private" and "home_channel" not in seed
    assert seed["max_concurrent"] == 5 and seed["channel"] == "chan" and seed["allowed_users"] == ["*"]
    monkeypatch.setenv("TWITCH_ALLOW_ALL_USERS", "false")
    assert "allowed_users" not in ad._env_enablement()


def test_provider_error_inside_turn_is_friendly_line():
    err = ("⚠️ I couldn't connect to the AI model service, so this message wasn't processed. Use /login to sign "
           "in again, or /model to pick a different model. If it keeps failing, run `hermes doctor` on the host.")
    assert pr.classify_gateway_notice(err) == "error"
    assert pr.classify_gateway_notice(AUTO_RESET) == "silent"
    assert pr.classify_gateway_notice("✨ Session reset! Starting fresh.") == "silent"
    assert pr.classify_gateway_notice('🔄 Background task started: "x"') == "silent"
    assert pr.classify_gateway_notice("ℹ️ Context compression deferred — summary still streaming. Continuing without compression this turn.") == "silent"
    assert pr.classify_gateway_notice("❓ Great question!") is None
    assert pr.classify_gateway_notice("hermes is restarting soon, lol") is None


def test_notice_templates_cover_the_installed_hermes_catalog():
    """Drift detector: every symbol-led gateway.* string in Hermes's en catalog must classify as a notice."""
    i18n = pytest.importorskip("agent.i18n")
    catalog = i18n._load_bundled("en")
    missing = []
    for key, value in catalog.items():
        if not key.startswith("gateway.") or not isinstance(value, str):
            continue
        text = value.replace("️", "").lstrip()
        if not text or (text[0].isascii() and text[0].isalnum()) or text[0] in "*`'\"(/[_-.:{":
            continue
        rendered = text.format_map(type("D", (dict,), {"__missing__": lambda self, k: "x"})())
        if len(" ".join(text.split("{")[0].split())) >= 6 and pr.classify_gateway_notice(rendered) is None:
            missing.append(key)
    assert missing == []


# -- WP5: liveness ----------------------------------------------------------------------------

@arun
async def test_silent_server_triggers_ping_then_retryable_reconnect(env, monkeypatch, caplog):
    monkeypatch.setattr(ad, "PING_AFTER", 0.3)
    monkeypatch.setattr(ad, "READ_TIMEOUT", 0.7)
    srv, a = await up(monkeypatch)
    srv.answer_ping = False                                    # the server goes silent
    notified = asyncio.Event()

    async def handler(adapter):
        notified.set()
    a.set_fatal_error_handler(handler)
    assert await a.connect()
    with caplog.at_level(logging.WARNING):
        await asyncio.wait_for(notified.wait(), 5)
    assert any(line.startswith("PING") for line in srv.lines)  # we pinged first
    assert a.has_fatal_error and a.fatal_error_retryable and a.fatal_error_code == "connection_lost"
    assert any("no data from the server" in r.message for r in caplog.records)
    await a.disconnect()
    await srv.stop()


@arun
async def test_pong_keeps_the_connection_alive(env, monkeypatch):
    monkeypatch.setattr(ad, "PING_AFTER", 0.3)
    monkeypatch.setattr(ad, "READ_TIMEOUT", 0.7)
    srv, a = await up(monkeypatch)                             # server answers PING with PONG
    assert await a.connect()
    await asyncio.sleep(1.8)
    assert a.is_connected and not a.has_fatal_error
    assert sum(1 for line in srv.lines if line.startswith("PING")) >= 2
    await a.disconnect()
    await srv.stop()


# -- WP6: output hygiene -----------------------------------------------------------------------

def test_no_part_starts_with_a_command_char_and_every_part_is_addressed():
    text = ("a" * 470 + " !ban x " + "b" * 470 + " /ban y " + "c" * 100)
    parts = pr.split_message(text, "Bob")
    assert len(parts) == 3
    for part in parts:
        assert part.startswith("@Bob ")
        assert part[len("@Bob "):][0] not in "/.!"
        assert len(part) <= 480
    assert pr.split_message("!ban x", None) == ["ban x"]
    assert pr.split_message("/ban x . y", "Al") == ["@Al ban x . y"]
    assert pr.split_message("...and so on", None) == ["and so on"]


def test_no_user_parts_never_start_with_command_chars():
    parts = pr.split_message("w " * 400 + "!ban x " + "z " * 400)
    assert all(part[0] not in "/.! " for part in parts)


@arun
async def test_model_output_with_command_in_part_two_is_neutralised(env, monkeypatch):
    srv, a = await up(monkeypatch)
    assert await a.connect()
    ev = await ask(srv, a, "v1", "!ask x")
    await a.send("chan", ("word " * 94).strip() + " !ban x " + ("more " * 90) + " /ban y " + "tail " * 20,
                 reply_to=ev.message_id)
    await asyncio.sleep(0.1)
    assert len(srv.posted) >= 2
    assert all(m.startswith("@V1 ") and m[4] not in "/.!" for m in srv.posted)
    await a.disconnect()
    await srv.stop()


@arun
async def test_backseat_reply_has_no_command_start(env, monkeypatch):
    srv, a = await up(monkeypatch, vote_seconds=0.3)
    assert await a.connect()
    await srv.say("boss", START, **BOSS)
    await srv.say("v1", "!vote 1", uid="11")
    await asyncio.sleep(0.7)
    ev = a.events[-1]
    await a.send("chan", "!ban everyone", reply_to=ev.message_id)
    await asyncio.sleep(0.1)
    assert srv.posted[-1] == "ban everyone"
    await a.disconnect()
    await srv.stop()


# -- WP7: allowlist consistency ------------------------------------------------------------------

@arun
async def test_restricted_mode_requires_allowlist_for_vote_and_backseat(env, monkeypatch):
    monkeypatch.setenv("TWITCH_ALLOW_ALL_USERS", "false")
    monkeypatch.setenv("TWITCH_ALLOWED_USERS", "11,77")
    srv, a = await up(monkeypatch)
    assert await a.connect()
    await srv.say("modx", START, uid="5", badges="moderator/1", mod=1)     # a mod that is NOT allowlisted
    assert a._vote is None and srv.posted == []
    await srv.say("boss", START, **BOSS)                                    # broadcaster always allowed
    assert a._vote is not None
    await srv.say("allowed", "!vote 2", uid="11")
    await srv.say("stranger", "!vote 1", uid="12")
    await srv.say("stranger2", "!vote 1", uid="13")
    assert a._vote._votes == {"11": 2}                                      # only the allowlisted voter counted
    await srv.say("m2", "!backseat cancel", uid="77", badges="moderator/1", mod=1)   # allowlisted mod may cancel
    assert srv.posted[-1] == "Vote cancelled."
    await a.disconnect()
    await srv.stop()


@arun
async def test_open_mode_anyone_may_vote(env, monkeypatch):
    srv, a = await up(monkeypatch)
    assert await a.connect()
    await srv.say("boss", START, **BOSS)
    await srv.say("anyone", "!vote 1", uid="99")
    assert a._vote._votes == {"99": 1}
    await a.disconnect()
    await srv.stop()


# -- WP8: vote races -------------------------------------------------------------------------------

def _block_result_post(a, monkeypatch):
    gate = asyncio.Event()
    real_post = a._post

    async def slow_post(*args, **kw):                          # the result post is still in flight after the timer
        if "Chat picked" in args[1]:
            await gate.wait()
        return await real_post(*args, **kw)
    monkeypatch.setattr(a, "_post", slow_post)
    return gate


@arun
async def test_disconnect_cancels_pending_winner_dispatch(env, monkeypatch):
    srv, a = await up(monkeypatch, vote_seconds=0.3)
    assert await a.connect()
    await srv.say("boss", START, **BOSS)
    await srv.say("v1", "!vote 1", uid="11")
    gate = _block_result_post(a, monkeypatch)
    await asyncio.sleep(0.6)
    await a.disconnect()
    gate.set()
    await asyncio.sleep(0.2)
    assert a.events == []
    await srv.stop()


@arun
async def test_cancel_after_timer_before_dispatch_cancels(env, monkeypatch):
    srv, a = await up(monkeypatch, vote_seconds=0.3)
    assert await a.connect()
    await srv.say("boss", START, **BOSS)
    await srv.say("v1", "!vote 1", uid="11")
    gate = _block_result_post(a, monkeypatch)
    await asyncio.sleep(0.6)                                   # timer fired, result post is blocked, no dispatch yet
    assert a._vote is not None and a.events == []
    await srv.say("boss", "!backseat cancel", **BOSS)
    gate.set()
    await asyncio.sleep(0.3)
    assert a.events == [] and a._vote is None
    assert srv.posted[-1] == "Vote cancelled."
    await a.disconnect()
    await srv.stop()


# -- WP2: tool guard hook ------------------------------------------------------------------------------

def test_pre_tool_call_guard_blocks_everything_but_web_search_for_twitch():
    from gateway import session_context as sc
    token = sc._SESSION_PLATFORM.set("twitch")
    try:
        assert ad._guard_pre_tool_call(tool_name="web_search", args={}) is None
        for tool in ("terminal", "read_file", "memory", "mcp_fs_read", "some_plugin_tool", "execute_code"):
            res = ad._guard_pre_tool_call(tool_name=tool, args={})
            assert res and res["action"] == "block" and res["message"]
    finally:
        sc._SESSION_PLATFORM.reset(token)
    for platform in ("telegram", "cli", ""):
        token = sc._SESSION_PLATFORM.set(platform)
        try:
            assert ad._guard_pre_tool_call(tool_name="terminal", args={}) is None   # other platforms untouched
        finally:
            sc._SESSION_PLATFORM.reset(token)


def test_register_adds_the_guard_hook():
    calls = []

    class Ctx:
        def register_hook(self, name, cb):
            calls.append((name, cb))

        def register_platform(self, **kw):
            calls.append(("platform", kw["name"]))
    ad.register(Ctx())
    assert ("pre_tool_call", ad._guard_pre_tool_call) in calls and ("platform", "twitch") in calls


def test_config_warnings(monkeypatch, caplog):
    import hermes_cli.config as hc
    monkeypatch.setattr(hc, "load_config_readonly", lambda: {"platform_toolsets": {}})
    with caplog.at_level(logging.WARNING):
        ad._warn_unsafe_config()
    msgs = " ".join(r.message for r in caplog.records)
    assert ("platform_toolsets.twitch is not pinned to [search, no_mcp]; viewer turns may get full tools "
            "if the adapter is unresolved") in msgs
    assert "skip_context_files" in msgs
    caplog.clear()
    monkeypatch.setattr(hc, "load_config_readonly", lambda: {
        "platform_toolsets": {"twitch": ["search", "no_mcp"]},
        "gateway": {"platforms": {"twitch": {"skip_context_files": True}}},
        "memory": {"memory_enabled": False, "user_profile_enabled": False}})
    with caplog.at_level(logging.WARNING):
        ad._warn_unsafe_config()
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
    caplog.clear()
    monkeypatch.setattr(hc, "load_config_readonly", lambda: {"platform_toolsets": {"twitch": ["search", "terminal"]}})
    with caplog.at_level(logging.WARNING):
        ad._warn_unsafe_config()
    assert "not pinned" in " ".join(r.message for r in caplog.records)
