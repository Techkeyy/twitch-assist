"""v0.3.0: private by default, open mode fails closed, authorization before cooldown/cap/pending turn.

Same local fake IRC server as ``test_adapter.py`` (needs Hermes importable). Nothing touches Twitch or a model.
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

BOSS = dict(uid="1", badges="broadcaster/1")
START = "!backseat What next? | tallest building | octopus facts"
UNSAFE = (False, "memory.memory_enabled and memory.user_profile_enabled must both be false")


@pytest.fixture
def private(env, monkeypatch):
    """Nothing set: the default. The safety check is made unsafe so any consultation in private mode would show."""
    monkeypatch.delenv("TWITCH_ALLOW_ALL_USERS", raising=False)
    monkeypatch.delenv("TWITCH_ALLOWED_USERS", raising=False)
    monkeypatch.setattr(ad, "_open_mode_check", lambda: UNSAFE)


def _open(monkeypatch, check):
    monkeypatch.setenv("TWITCH_ALLOW_ALL_USERS", "true")
    monkeypatch.setattr(ad, "_open_mode_check", lambda: check)


# -- item 1: private by default -------------------------------------------------------------------

@arun
async def test_default_is_private_broadcaster_and_allowlisted_only(private, monkeypatch):
    monkeypatch.setenv("TWITCH_ALLOWED_USERS", "77,78")
    srv, a = await up(monkeypatch)
    assert a._open is False
    assert await a.connect()
    assert await ask(srv, a, "stranger", "!ask hi", uid="5") is None
    assert await ask(srv, a, "stranger", "@bot hello there", uid="5") is None
    assert await ask(srv, a, "boss", "!ask hi", **BOSS) is not None
    assert await ask(srv, a, "friend", "!ask hi", uid="77") is not None           # allowlisted id, even on an unsafe config
    assert await ask(srv, a, "friend2", "@bot hello there", uid="78") is not None
    assert [e.source.user_id for e in a.events] == ["1", "77", "78"]
    await a.disconnect()
    await srv.stop()


@arun
async def test_unset_empty_false_and_junk_all_mean_private(private, monkeypatch):
    srv, a = await up(monkeypatch)
    for raw in ("", "false", "0", "no", "maybe", "  "):
        monkeypatch.setenv("TWITCH_ALLOW_ALL_USERS", raw)
        assert ad.TwitchAdapter(a.config)._open is False, raw
    for raw in ("true", "1", "yes", "on", "TRUE"):
        monkeypatch.setenv("TWITCH_ALLOW_ALL_USERS", raw)
        assert ad.TwitchAdapter(a.config)._open is True, raw
    await srv.stop()


@arun
async def test_private_mode_is_silent_to_refused_viewers(private, monkeypatch):
    srv, a = await up(monkeypatch)
    assert await a.connect()
    await ask(srv, a, "stranger", "!ask hi", uid="5")
    await srv.say("stranger", START, uid="5", badges="moderator/1", mod=1)
    assert srv.posted == [] and a.events == [] and a._vote is None
    await a.disconnect()
    await srv.stop()


def test_env_seed_private_by_default_and_open_only_when_explicit(monkeypatch):
    monkeypatch.setenv("TWITCH_TOKEN", "t")
    monkeypatch.setenv("TWITCH_CHANNEL", "chan")
    for var in ("TWITCH_ALLOW_ALL_USERS", "TWITCH_ALLOWED_USERS"):
        monkeypatch.delenv(var, raising=False)
    assert "allowed_users" not in ad._env_enablement()                       # unset: no "*"
    monkeypatch.setenv("TWITCH_ALLOWED_USERS", "12, 11")
    assert ad._env_enablement()["allowed_users"] == ["11", "12"]             # the allowlist only
    for raw in ("false", "", "junk"):
        monkeypatch.setenv("TWITCH_ALLOW_ALL_USERS", raw)
        assert "*" not in ad._env_enablement().get("allowed_users", [])
    for raw in ("true", "1", "yes", "on"):
        monkeypatch.setenv("TWITCH_ALLOW_ALL_USERS", raw)
        assert ad._env_enablement()["allowed_users"] == ["*"]                # explicit opt-in
    monkeypatch.setenv("TWITCH_ALLOW_ALL_USERS", "false")
    monkeypatch.setenv("TWITCH_ALLOWED_USERS", "*")
    assert ad._env_enablement()["allowed_users"] == ["*"]                    # "*" in the list is also an explicit opt-in


def test_register_declares_the_gateway_allowlist_envs():
    seen = {}

    class Ctx:
        def register_hook(self, *a, **k):
            pass

        def register_platform(self, **kw):
            seen.update(kw)
    ad.register(Ctx())
    assert seen["allowed_users_env"] == "TWITCH_ALLOWED_USERS" and seen["allow_all_env"] == "TWITCH_ALLOW_ALL_USERS"


@arun
async def test_gateway_is_told_about_the_broadcaster_never_about_a_wildcard(private, monkeypatch):
    monkeypatch.setenv("TWITCH_ALLOWED_USERS", "77")
    srv, a = await up(monkeypatch)
    a.config.extra["allowed_users"] = ["77"]                                  # what _env_enablement seeds
    assert await a.connect()
    await ask(srv, a, "stranger", "!ask hi", uid="5")
    assert a.resolved_allowlist_user_ids() == set() and a.config.extra["allowed_users"] == ["77"]
    await ask(srv, a, "boss", "!ask hi", **BOSS)
    assert a.resolved_allowlist_user_ids() == {"1"}
    assert a.config.extra["allowed_users"] == ["77", "1"] and "*" not in a.config.extra["allowed_users"]
    await a.disconnect()
    await srv.stop()


# -- item 4: authorization before cooldown / cap / pending turn --------------------------------------

@arun
async def test_refused_viewer_registers_no_turn_takes_no_slot_no_cooldown(private, monkeypatch):
    monkeypatch.setenv("TWITCH_ALLOWED_USERS", "77")
    srv, a = await up(monkeypatch, cooldown=60)
    a._max_concurrent = 1
    assert await a.connect()
    for i in range(5):
        await ask(srv, a, "stranger", f"!ask q{i}", uid="5")
        await ask(srv, a, "stranger2", f"@bot q{i}", uid="6")
    assert a.events == [] and len(a._turns) == 0 and a._active_turns() == 0     # no pending turn, no slot held
    assert "5" not in a._cooldown._last and "6" not in a._cooldown._last        # no cooldown started
    assert await ask(srv, a, "friend", "!ask real", uid="77") is not None       # the single slot is still free
    await a.disconnect()
    await srv.stop()


@arun
async def test_unsafe_open_refusal_also_takes_no_turn_slot_or_cooldown(env, monkeypatch):
    _open(monkeypatch, UNSAFE)
    srv, a = await up(monkeypatch, cooldown=60)
    a._max_concurrent = 1
    assert await a.connect()
    for i in range(4):
        await ask(srv, a, "stranger", f"!ask q{i}", uid="5")
    assert a.events == [] and len(a._turns) == 0 and a._active_turns() == 0 and "5" not in a._cooldown._last
    await a.disconnect()
    await srv.stop()


# -- item 2: open mode fails closed ---------------------------------------------------------------------

@arun
async def test_open_unsafe_drops_non_broadcaster_logs_once_broadcaster_still_works(env, monkeypatch, caplog):
    _open(monkeypatch, UNSAFE)
    srv, a = await up(monkeypatch)
    with caplog.at_level(logging.DEBUG):
        assert await a.connect()
        for i in range(4):
            assert await ask(srv, a, f"v{i}", f"!ask q{i}", uid=f"5{i}") is None
        assert await ask(srv, a, "v9", "@bot hello", uid="59") is None
        assert await ask(srv, a, "boss", "!ask mine", **BOSS) is not None
    refusals = [r for r in caplog.records if r.levelno >= logging.WARNING and "not safe" in r.getMessage()]
    assert len(refusals) == 1                                                  # exactly once, with the reason
    assert "memory.memory_enabled" in refusals[0].getMessage()
    assert srv.posted == []                                                    # refused silently in chat
    await a.disconnect()
    await srv.stop()


@arun
async def test_open_unsafe_refusal_is_logged_once_even_without_connect_log(env, monkeypatch, caplog):
    _open(monkeypatch, UNSAFE)
    srv, a = await up(monkeypatch)
    a._joined.set()
    with caplog.at_level(logging.WARNING):
        await a._handle_chat(ad.parse_privmsg(ad.parse_line(
            "@badges=;display-name=V;id=m1;mod=0;user-id=5 :v!v@v.tmi.twitch.tv PRIVMSG #chan :!ask hi")))
        await a._handle_chat(ad.parse_privmsg(ad.parse_line(
            "@badges=;display-name=V;id=m2;mod=0;user-id=6 :w!w@w.tmi.twitch.tv PRIVMSG #chan :!ask hi")))
    assert len([r for r in caplog.records if "not safe" in r.getMessage()]) == 1
    await srv.stop()


@arun
async def test_open_safe_dispatches_non_broadcaster(env, monkeypatch):
    _open(monkeypatch, (True, ""))
    srv, a = await up(monkeypatch)
    assert await a.connect()
    assert await ask(srv, a, "v", "!ask hi", uid="5") is not None
    assert await ask(srv, a, "w", "@bot hi", uid="6") is not None
    await a.disconnect()
    await srv.stop()


@arun
async def test_open_unsafe_votes_still_tally_but_cannot_start_an_agent_turn(env, monkeypatch):
    _open(monkeypatch, UNSAFE)
    srv, a = await up(monkeypatch)
    assert await a.connect()
    await srv.say("modx", START, uid="5", badges="moderator/1", mod=1)         # a mod may not start it
    assert a._vote is None
    await srv.say("boss", START, **BOSS)                                        # the broadcaster may
    assert a._vote is not None
    await srv.say("anyone", "!vote 2", uid="99")                                # !vote is not an agent turn
    assert a._vote._votes == {"99": 2}
    await asyncio.sleep(1.4)
    assert len(a.events) == 1 and a.events[0].source.user_id == "1"             # broadcaster-started winner dispatched
    await a.disconnect()
    await srv.stop()


@arun
async def test_backseat_winner_from_non_broadcaster_starter_not_dispatched_when_unsafe(env, monkeypatch):
    state = {"check": (True, "")}
    monkeypatch.setenv("TWITCH_ALLOW_ALL_USERS", "true")
    monkeypatch.setattr(ad, "_open_mode_check", lambda: state["check"])
    srv, a = await up(monkeypatch)
    assert await a.connect()
    await srv.say("modx", START, uid="5", badges="moderator/1", mod=1)         # safe config: the mod starts a vote
    assert a._vote is not None
    await srv.say("anyone", "!vote 1", uid="99")
    a._open_check = state["check"] = UNSAFE                                      # config becomes unsafe before the end
    await asyncio.sleep(1.4)
    assert srv.posted[-1].startswith("Chat picked")                              # result still announced
    assert a.events == []                                                        # but never reaches the agent
    await a.disconnect()
    await srv.stop()


@arun
async def test_private_mode_allowlisted_mod_backseat_dispatches_on_unsafe_config(private, monkeypatch):
    monkeypatch.setenv("TWITCH_ALLOWED_USERS", "77")
    srv, a = await up(monkeypatch)
    assert await a.connect()
    await srv.say("m", START, uid="77", badges="moderator/1", mod=1)
    await srv.say("anyone", "!vote 1", uid="77")
    await asyncio.sleep(1.4)
    assert len(a.events) == 1 and a.events[0].source.user_id == "77"
    await a.disconnect()
    await srv.stop()


# -- the one safety check ---------------------------------------------------------------------------------

def test_open_mode_check_needs_memory_off_and_pin(monkeypatch):
    import hermes_cli.config as hc
    safe = {"platform_toolsets": {"twitch": ["search", "no_mcp"]},
            "memory": {"memory_enabled": False, "user_profile_enabled": False}}
    monkeypatch.setattr(hc, "load_config_readonly", lambda: safe)
    assert ad._open_mode_check() == (True, "")
    for cfg, word in (
            ({**safe, "memory": {}}, "memory.memory_enabled"),
            ({**safe, "memory": {"memory_enabled": False}}, "memory.memory_enabled"),
            ({**safe, "memory": {"memory_enabled": False, "user_profile_enabled": True}}, "memory.memory_enabled"),
            ({"memory": safe["memory"]}, "platform_toolsets.twitch"),
            ({**safe, "platform_toolsets": {"twitch": ["search", "terminal"]}}, "platform_toolsets.twitch"),
            ({**safe, "platform_toolsets": {"twitch": ["search"]}}, "platform_toolsets.twitch")):
        monkeypatch.setattr(hc, "load_config_readonly", lambda cfg=cfg: cfg)
        ok, reason = ad._open_mode_check()
        assert ok is False and word in reason, cfg

    def boom():
        raise RuntimeError("no config")
    monkeypatch.setattr(hc, "load_config_readonly", boom)
    assert ad._open_mode_check()[0] is False                                    # unreadable config: fail closed
