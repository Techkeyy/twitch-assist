"""Pure-logic tests for twitch_assist.protocol (no Hermes needed).

protocol.py is loaded straight from its file so these run without importing the plugin package
(whose __init__ imports Hermes modules).
"""
import importlib.util
import pathlib
import sys

_PATH = pathlib.Path(__file__).resolve().parents[1] / "twitch_assist" / "protocol.py"
_spec = importlib.util.spec_from_file_location("twitch_protocol", _PATH)
p = importlib.util.module_from_spec(_spec)
sys.modules["twitch_protocol"] = p
_spec.loader.exec_module(p)

RAW = (
    "@badge-info=;badges=broadcaster/1,premium/1;color=#FF0000;display-name=Is\\sZee;emotes=;"
    "id=abc-123;mod=0;room-id=42;user-id=999;tmi-sent-ts=1 "
    ":iszee24!iszee24@iszee24.tmi.twitch.tv PRIVMSG #iszee24 :!ask what is up?"
)


# -- parsing -----------------------------------------------------------------

def test_parse_privmsg_tags():
    chat = p.parse_privmsg(p.parse_line(RAW))
    assert chat.channel == "iszee24"
    assert chat.login == "iszee24"
    assert chat.display_name == "Is Zee"          # \s unescaped
    assert chat.user_id == "999"
    assert chat.message_id == "abc-123"
    assert chat.text == "!ask what is up?"
    assert chat.badges == ["broadcaster/1", "premium/1"]
    assert chat.is_broadcaster is True


def test_parse_privmsg_viewer_not_broadcaster_and_missing_display_name():
    raw = "@badges=subscriber/3;id=x;user-id=5 :bob!bob@bob.tmi.twitch.tv PRIVMSG #chan :hi"
    chat = p.parse_privmsg(p.parse_line(raw))
    assert chat.display_name == "bob"
    assert chat.is_broadcaster is False


def test_parse_ignores_actions_and_non_privmsg():
    assert p.parse_privmsg(p.parse_line(":a!a@a PRIVMSG #c :\x01ACTION waves\x01")) is None
    assert p.parse_privmsg(p.parse_line(":tmi.twitch.tv 001 bot :Welcome")) is None
    assert p.parse_privmsg(p.parse_line(":a!a@a PRIVMSG botname :whisper-ish")) is None


def test_parse_ping_and_tag_escapes():
    msg = p.parse_line("PING :tmi.twitch.tv")
    assert msg["command"] == "PING" and msg["params"] == ["tmi.twitch.tv"]
    assert p.parse_line("@k=a\\:b\\\\c :x PING")["tags"]["k"] == "a;b\\c"


# -- auth failure ------------------------------------------------------------

def test_auth_failure_detection():
    assert p.is_auth_failure(p.parse_line(":tmi.twitch.tv NOTICE * :Login authentication failed"))
    assert p.is_auth_failure(p.parse_line(":tmi.twitch.tv NOTICE * :Improperly formatted auth"))
    assert p.is_auth_failure(p.parse_line(":tmi.twitch.tv NOTICE * :Login unsuccessful"))
    assert not p.is_auth_failure(p.parse_line(":tmi.twitch.tv NOTICE #c :Slow mode is on"))
    assert not p.is_auth_failure(p.parse_line(":tmi.twitch.tv 001 bot :Welcome, GLHF!"))


# -- trigger / mention filter ------------------------------------------------

def test_trigger_filter():
    assert p.extract_question("!ask what is rust", "!ask", "bot") == "what is rust"
    assert p.extract_question("  !ASK   spaced  ", "!ask", "bot") == "spaced"
    assert p.extract_question("hello chat", "!ask", "bot") is None
    assert p.extract_question("!asking is not the trigger", "!ask", "bot") is None
    assert p.extract_question("!ask", "!ask", "bot") is None            # empty question
    assert p.extract_question("hey !ask in the middle", "!ask", "bot") is None


def test_mention_filter():
    assert p.extract_question("@Bot what time is it", "!ask", "bot") == "what time is it"
    assert p.extract_question("hey @bot how are you", "!ask", "bot") == "hey how are you"
    assert p.extract_question("hello @botfriend", "!ask", "bot") is None
    assert p.extract_question("@bot", "!ask", "bot") is None
    assert p.extract_question("mail me@bot.com", "!ask", "bot") is None


def test_custom_trigger():
    assert p.extract_question("!hermes hi", "!hermes", "bot") == "hi"
    assert p.extract_question("!ask hi", "!hermes", "bot") is None


# -- cooldown ----------------------------------------------------------------

def test_cooldown_per_user():
    cd = p.Cooldown(20)
    assert cd.allow("u1", now=100.0)
    assert not cd.allow("u1", now=110.0)        # still cooling
    assert cd.allow("u2", now=110.0)            # other user unaffected
    assert cd.allow("u1", now=120.0)            # elapsed
    assert not cd.allow("u1", now=121.0)


def test_cooldown_zero_never_blocks():
    cd = p.Cooldown(0)
    assert cd.allow("u", now=1.0) and cd.allow("u", now=1.0)


# -- own messages ------------------------------------------------------------

def test_sent_tracker_own_messages_only():
    st = p.SentTracker(ttl=60)
    st.remember("@bob Hello   there", now=0.0)
    assert st.is_own("@bob hello there", now=10.0)       # normalised match
    assert not st.is_own("!ask something new", now=10.0)  # same login, different text: NOT own
    assert not st.is_own("@bob Hello there", now=100.0)   # expired


# -- outbound ----------------------------------------------------------------

def test_markdown_stripped_and_newlines_collapsed():
    md = "# Title\n\n**Bold** and *italic* and `code`.\n- one\n- two\n[site](https://x.io)\n```py\nprint(1)\n```"
    out = p.to_plain_text(md)
    assert "\n" not in out
    for junk in ("**", "`", "# ", "```", "["):
        assert junk not in out
    assert "Bold and italic and code." in out
    assert "site (https://x.io)" in out


def test_split_short_reply_prefix():
    assert p.split_message("Hello there", "Bob") == ["@Bob Hello there"]
    assert p.split_message("Hello there") == ["Hello there"]
    assert p.split_message("   \n ") == []


def test_split_long_reply_limits():
    text = " ".join(f"word{i}" for i in range(600))        # ~4000 chars
    parts = p.split_message(text, "Bob")
    assert len(parts) == 3
    assert all(len(x) <= 480 for x in parts)
    assert all(len(x) < 500 for x in parts)                # Twitch hard limit
    assert parts[0].startswith("@Bob ")
    assert parts[-1].endswith("…")                         # truncated marker


def test_split_medium_reply_two_parts_no_loss():
    text = " ".join(f"w{i}" for i in range(250))            # ~1200 chars -> 3 parts, no truncation
    parts = p.split_message(text, None)
    assert 2 <= len(parts) <= 3
    assert " ".join(parts) == text


def test_split_exact_boundaries():
    assert p.split_message("a" * 480) == ["a" * 480]
    parts = p.split_message("a" * 481)
    assert len(parts) == 2 and all(len(x) <= 480 for x in parts) and "".join(parts) == "a" * 481
    # a single unbroken 2000-char token still respects the limit
    parts = p.split_message("x" * 2000, "u")
    assert len(parts) == 3 and all(len(x) <= 480 for x in parts)
