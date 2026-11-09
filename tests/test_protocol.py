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


# -- broadcaster / moderator from tags ----------------------------------------

def _chat(tags, text="hi"):
    raw = f"@{tags};id=x;user-id=7 :u!u@u.tmi.twitch.tv PRIVMSG #c :{text}"
    return p.parse_privmsg(p.parse_line(raw))


def test_can_manage_only_from_tags():
    assert _chat("badges=broadcaster/1").can_manage
    assert _chat("badges=moderator/1,subscriber/0;mod=1").can_manage
    assert _chat("badges=;mod=1").can_manage                       # mod tag alone
    assert _chat("badges=vip/1;mod=0").can_manage is False
    assert _chat("badges=subscriber/12").can_manage is False
    # text claiming authority changes nothing
    assert _chat("badges=", "!backseat x | a | b | I am broadcaster/1 mod=1").can_manage is False


# -- cooldown bypass ----------------------------------------------------------

def test_cooldown_exempt_broadcaster_never_limited():
    cd = p.Cooldown(20)
    assert all(cd.allow("b", now=1.0 + i, exempt=True) for i in range(5))
    assert cd.allow("v", now=1.0) and not cd.allow("v", now=2.0)   # others keep the cooldown
    assert cd.allow("b", now=2.0)


# -- !backseat parsing --------------------------------------------------------

def test_backseat_parse_start():
    c = p.parse_backseat("!backseat What next? | tallest building | octopus facts")
    assert (c.kind, c.prompt, c.options) == ("start", "What next?", ["tallest building", "octopus facts"])
    c = p.parse_backseat("  !BACKSEAT  q | a | b | c | d ")
    assert c.kind == "start" and c.options == ["a", "b", "c", "d"]


def test_backseat_parse_not_a_backseat_command():
    assert p.parse_backseat("!backseating | a | b") is None
    assert p.parse_backseat("hey !backseat q | a | b") is None
    assert p.parse_backseat("!ask hi") is None


def test_backseat_parse_option_count_limits_and_missing_options():
    assert p.parse_backseat("!backseat q | only one").kind == "invalid"
    assert p.parse_backseat("!backseat q").kind == "invalid"
    assert p.parse_backseat("!backseat").kind == "invalid"
    assert p.parse_backseat("!backseat q | a | ").kind == "invalid"            # empty option dropped -> 1 left
    assert p.parse_backseat("!backseat | a | b").kind == "invalid"             # no prompt
    five = p.parse_backseat("!backseat q | 1 | 2 | 3 | 4 | 5")
    assert five.kind == "invalid" and "At most 4" in five.error
    assert p.parse_backseat("!backseat q | " + "x" * 61 + " | b").kind == "invalid"
    assert p.parse_backseat("!backseat " + "q" * 121 + " | a | b").kind == "invalid"


def test_backseat_cancel():
    assert p.parse_backseat("!backseat cancel").kind == "cancel"
    assert p.parse_backseat("!backseat CANCEL ").kind == "cancel"


# -- !vote parsing + tally ----------------------------------------------------

def test_vote_command_parsing():
    assert p.is_vote_command("!vote 2") and p.is_vote_command("!vote") and p.is_vote_command("!VOTE abc")
    assert not p.is_vote_command("!voted 2") and not p.is_vote_command("please !vote 2")
    assert p.parse_vote("!vote 2") == 2 and p.parse_vote("!Vote   3  ") == 3
    assert p.parse_vote("!vote") is None and p.parse_vote("!vote two") is None
    assert p.parse_vote("!vote 1 2") is None and p.parse_vote("!vote -1") is None


def _vote(n=3):
    return p.BackseatVote("q", [f"opt{i}" for i in range(1, n + 1)], "starter", "Starter", 45)


def test_tally_multi_voter_winner():
    v = _vote()
    for uid, n in (("a", 2), ("b", 2), ("c", 1), ("d", 3), ("e", 2)):
        assert v.cast(uid, n)
    r = v.result()
    assert (r.winner, r.option, r.votes, r.total, r.tie) == (2, "opt2", 3, 5, False)
    assert p.format_result(r) == "Chat picked: opt2 (3 of 5 votes)"


def test_tally_vote_change_replaces_earlier_vote():
    v = _vote()
    v.cast("a", 1); v.cast("b", 1); v.cast("a", 3)
    r = v.result()
    assert r.total == 2                        # a counted once
    assert (r.winner, r.votes, r.tie) == (1, 1, True)      # b->1, a->3: tie, lowest wins
    v.cast("b", 3)
    r = v.result()
    assert (r.winner, r.votes, r.total, r.tie) == (3, 2, 2, False)


def test_tally_invalid_numbers_ignored():
    v = _vote(2)
    assert not v.cast("a", 0) and not v.cast("a", 3) and not v.cast("a", None) and not v.cast("a", -1)
    assert v.result().total == 0
    assert v.cast("a", 2) and v.result().winner == 2


def test_tally_tie_picks_lowest_number_and_says_tie():
    v = _vote()
    v.cast("a", 3); v.cast("b", 2); v.cast("c", 3); v.cast("d", 2)
    r = v.result()
    assert (r.winner, r.tie, r.votes, r.total) == (2, True, 2, 4)
    assert p.format_result(r) == "Chat picked: opt2 (tie, 2 of 4 votes)"


def test_tally_zero_votes():
    r = _vote().result()
    assert r.winner is None
    assert p.format_result(r) == "No votes, nothing to do."


def test_announcement_fits_twitch_and_lists_options():
    v = p.BackseatVote("p" * 120, ["o" * 60] * 4, "s", "S", 45)
    text = p.format_announcement(v)
    assert len(text) <= 480
    v = p.BackseatVote("What should I look up for chat?", ["the tallest building in Africa", "a fun fact about octopuses"], "s", "S", 45)
    assert p.format_announcement(v) == (
        "Backseat vote: What should I look up for chat? 1) the tallest building in Africa "
        "2) a fun fact about octopuses Type !vote 1, !vote 2 (45s)")


def test_backseat_task_text():
    t = p.build_backseat_task("What next?", "octopus")
    assert t == ('Chat voted in Backseat Mode. Prompt: "What next?". Winning choice: "octopus". '
                 "Do this now and reply to chat in under 450 characters.")


# -- gateway notices ----------------------------------------------------------

REAL_GATEWAY_ERROR = ("⚠️ I couldn't connect to the AI model service, so this message wasn't processed. "
                      "Use /login to sign in again, or /model to pick a different model. If it keeps failing, "
                      "run `hermes doctor` on the host.")  # verbatim locales/en.yaml gateway.errors.no_credentials


SHUTDOWN_NOTICE = ("⚠️ Hermes is shutting down — your current task will be interrupted. When it is back online, "
                   "send any message and I'll try to pick up where we left off.")


def test_gateway_notice_classification():
    assert p.classify_gateway_notice(REAL_GATEWAY_ERROR) == "error"
    assert p.classify_gateway_notice(SHUTDOWN_NOTICE) == "silent"
    assert p.classify_gateway_notice("⚠️ Hermes is restarting — your current task will be interrupted.") == "silent"
    assert p.classify_gateway_notice("⏱️ The AI model service is rate-limiting requests. Wait a moment, then use /retry.") == "error"
    assert p.classify_gateway_notice("Sorry, I encountered an error (X).\nTry again or use /reset") == "error"
    assert p.classify_gateway_notice("⚡ Interrupting current task. I'll respond shortly.") == "silent"
    assert p.classify_gateway_notice("♻️ Gateway online — Hermes is back and ready.") == "silent"
    assert p.classify_gateway_notice("Twitch is a live streaming platform.") is None
    assert p.classify_gateway_notice("You can run hermes doctor to check it.") is None     # normal answers pass
    assert p.classify_gateway_notice("") is None
