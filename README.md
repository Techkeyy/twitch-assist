# Twitch Assist

A Hermes Agent platform plugin that puts your own Hermes in your Twitch chat as a co-host: viewers ask it things, and chat can vote on what it does next.

**[Repo](https://github.com/Techkeyy/twitch-assist)** · **[Real chat round trip (screenshot)](docs/chat-proof.jpg)**

> *"Someone asked a question ten minutes ago and I was mid-fight. Did anyone ever answer it?"*

If you stream, you cannot read chat and play at the same time. Viewers ask, nobody answers, and they drift off. Chat bots mostly run fixed commands. This plugin connects the Hermes agent you already run to your channel, so a viewer who types `!ask` gets a real answer from your model, in chat, addressed to them.

*Built for the Hermes Agent Plugin Hackathon (Nous Research), November 2026.*

![Real Twitch chat: a viewer asks, Hermes answers; a Backseat vote picks a topic](docs/chat-proof.jpg)

*Real chat in #iszee24, 2026-11-09. Viewer `iszee23` typed `!ask what's a fun fact about space?` and Hermes (DeepSeek Flash) replied in chat. The same screenshot shows a Backseat vote resolving to "a fun fact about octopuses" and Hermes answering it.*

## Why a co-host, not a bot

Fixed-command bots answer what someone scripted in advance. Twitch Assist hands the question to your Hermes agent and posts the model's answer, so it can answer things nobody anticipated. The second half is **Backseat Mode**: you start a chat vote, chat picks one of your options, and the winner is handed to Hermes as its next task. Chat steers the agent, with the broadcaster or a mod holding the start and cancel controls.

It is also locked down by default. Every Twitch turn gets web search only (see Safety). A stranger in chat cannot make your agent touch your files, terminal, browser or MCP servers.

## What it does

1. **Connects** to one Twitch channel over IRC on TLS (`irc.chat.twitch.tv:6697`), using stdlib `asyncio` and `ssl`. No extra packages.
2. **Ask.** A viewer types `!ask <question>` (or @mentions the bot). The adapter filters, applies a per-viewer cooldown, and hands the question to a real Hermes agent turn.
3. **Answers** in chat as `@viewer ...`. Replies are stripped of markdown and split into at most three chat messages that fit Twitch's 500 character limit.
4. **Backseat.** The broadcaster or a mod types `!backseat <prompt> | <option 1> | <option 2>` (2 to 4 options). Chat votes with `!vote N` for the vote window. The result is posted, and the winning option becomes a Hermes task whose answer goes to chat.
5. **Stays safe.** Twitch turns get only the `search` and `no_mcp` toolsets, viewers cannot run gateway `/commands`, and gateway status and error text never reaches public chat.

## Chat commands

| Command | Who can use it | What happens |
|---|---|---|
| `!ask <question>` | Any viewer (default), or only `TWITCH_ALLOWED_USERS` if you restrict it | Real Hermes turn, answer posted in chat. Per-viewer cooldown applies, the broadcaster is exempt |
| `@botname <question>` | Same as `!ask` | Same as `!ask` |
| `!backseat <prompt> \| <opt 1> \| <opt 2> [\| ...]` | Broadcaster or a mod | Starts a vote with 2 to 4 options. A bad format gets a usage line. Anyone else is ignored silently |
| `!vote N` | Any viewer | Counts one vote for option N. Voting again replaces your earlier vote. Never starts an agent turn |
| `!backseat cancel` | Broadcaster or a mod | Cancels the running vote, nothing is dispatched |

Whether someone is the broadcaster or a mod comes from Twitch's server-supplied message tags, never from the message text.

## Quickstart

You need Hermes 0.23.0 or newer with a model provider configured, and a Twitch chat token (next section).

```bash
# 1. Install and enable (the plugin lives in the twitch_assist subdirectory of this repo)
hermes plugins install Techkeyy/twitch-assist#twitch_assist --enable

# 2. Configure (these write to Hermes's .env; find it with `hermes config env-path`)
hermes config set TWITCH_TOKEN <your chat token>
hermes config set TWITCH_CHANNEL <your channel, no #>

# 3. Run
hermes gateway run
```

Then type `!ask hello` in your channel from a second account. A viewer account is the real test, because the broadcaster is treated a little differently (no cooldown).

Once the plugin is in the Hermes catalog, `hermes plugins install twitch-assist` also works. `hermes plugins install` accepts a Git URL, `owner/repo`, or `owner/repo#subdir`, and does not accept local paths.

**Belt and braces.** The adapter sets the Twitch toolsets itself. If the gateway ever cannot resolve the adapter for a session, it falls back to platform defaults, so I also recommend adding this row under the existing `platform_toolsets:` section of `config.yaml` (I run with it, and `hermes config check` accepts it):

```yaml
platform_toolsets:
  twitch: [search, no_mcp]
```

### Getting a chat token

The token needs the scopes `chat:read` and `chat:edit`. Use a dedicated bot account if you can, not your main one.

- **Preferred:** register an app at https://dev.twitch.tv/console (Twitch requires 2FA on the account), then run an OAuth flow for that app with the scopes `chat:read chat:edit` and copy the user access token it returns. The token is then issued through your own app.
- **Alternative:** a third-party token generator such as twitchtokengenerator.com. Caution: the token is issued through someone else's Twitch app, so only do this with a throwaway or dedicated bot account. You can revoke it any time in Twitch under Settings, Connections.

I used the alternative, with a dedicated account, for the live runs above. Never paste a real token into an issue, a screenshot or a commit.

## Configuration

Set these through Hermes (`hermes config set NAME value`). Defaults are the ones in the code.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `TWITCH_TOKEN` | yes | none | Chat OAuth token with `chat:read chat:edit`. Stored by Hermes as a secret |
| `TWITCH_CHANNEL` | yes | none | Channel to join, without `#` |
| `TWITCH_BOT_USERNAME` | no | the login the token belongs to (from Twitch token validation), else the channel | Bot account login, used for @mentions and for ignoring its own messages |
| `TWITCH_TRIGGER` | no | `!ask` | Chat trigger word. An @mention of the bot also works |
| `TWITCH_COOLDOWN_SECONDS` | no | `20` | Per-viewer cooldown between questions. `0` disables it. Broadcaster is always exempt |
| `TWITCH_VOTE_SECONDS` | no | `45` | Backseat vote length. Clamped to 5 to 600 |
| `TWITCH_ALLOW_ALL_USERS` | no | `true` | Let any viewer talk to the bot. Set `false` to restrict |
| `TWITCH_ALLOWED_USERS` | no | none | Comma-separated Twitch user IDs allowed when `TWITCH_ALLOW_ALL_USERS=false` |

## How I tried to break it

Every row below is a test in this repo (46 pass) or a live run.

| Attempt | Result | Where |
|---|---|---|
| Real viewer account asks a question in a real channel | Hermes answers in chat, addressed to the viewer | Live run, `docs/chat-proof.jpg` |
| Ordinary chat that is not addressed to the bot | Ignored, no agent turn | `tests/test_protocol.py` (`test_trigger_filter`, `test_mention_filter`) |
| One viewer spamming `!ask` | Per-viewer cooldown blocks it, broadcaster is exempt | `test_cooldown_per_user`, `test_broadcaster_never_on_cooldown_viewers_are` |
| Viewer tries `!backseat`, or types `mod=1` / `broadcaster/1` inside the message text | Ignored, no vote starts | `tests/test_adapter.py::test_backseat_non_mod_ignored_mod_allowed` |
| Real vote in live chat: `!backseat`, `!vote 2` | "Chat picked: a fun fact about octopuses (1 of 1 votes)", then Hermes answers | Live run (one voter only) |
| Several voters, changed votes, invalid numbers, tie, zero votes, cancel | Correct winner, lowest number wins a tie and says so, zero votes does nothing, cancel dispatches nothing | `test_tally_*`, `test_backseat_cancel_and_no_dispatch`, `test_backseat_zero_votes_and_tie` |
| Vote winner reaches Hermes with safe settings | Dispatched once, with viewer-level limits | `test_backseat_full_vote_dispatches_winner_safely` |
| Gateway error text (for example "run hermes doctor") | Replaced by one line, "Sorry, I can't answer right now."; original goes to the log | `test_gateway_error_is_replaced_and_original_logged` |
| Gateway shutdown and status notices | Never posted to chat | `test_shutdown_notice_never_posted_marker_and_text`, `test_status_notice_dropped_and_normal_answer_passes` |
| Very long answer, markdown | Stripped, split into at most 3 messages under the limit | `test_split_long_reply_limits`, `test_markdown_stripped_and_newlines_collapsed` |
| Bad or expired token | One attempt, then "Twitch login failed: check TWITCH_TOKEN", no retry loop | `test_bad_token_login_failed_no_retry_loop`, `test_token_rejected_by_validation_never_opens_connection` |
| Connection drops | Reported as retryable, reconnect works against a local fake IRC server | `test_unexpected_drop_signals_retryable_fatal_then_reconnect_works` |
| Clean stop | No ERROR lines in the log | `test_clean_disconnect_logs_no_error` |
| **Known blind spot:** a real answer that starts with an emoji such as a warning sign or a lightning bolt | Treated as a gateway notice and dropped | Documented limit, see Limitations |

**Key invariant: a viewer's message cannot start a vote or run a gateway command, and the adapter gives viewer turns web search only (pin it in `config.yaml` too, see Quickstart).**

Trust architecture in one sentence: who may start a vote is decided from Twitch's server tags, and what a viewer's turn may do is decided by the adapter's toolset list, never by message text.

Run the tests yourself: the adapter tests need the Hermes source importable and are skipped otherwise (see the header of `tests/test_adapter.py`). I ran all 46 with Hermes's own Python.

## Safety

- **Tools:** `toolsets_for_source` returns `["search", "no_mcp"]` for every Twitch turn. That is web search and nothing else: no terminal, files, code execution, browser or MCP.
- **No gateway control:** Twitch messages are dispatched with `allow_gateway_control=False`, so viewers cannot run `/commands`.
- **Vote authority:** only the broadcaster or a mod, taken from server tags.
- **Public chat hygiene:** gateway lifecycle notices are kept out of chat, and provider errors become one friendly line.
- **Approvals:** the plugin does not touch Hermes's approval system.

## Architecture

| Module | Job |
|---|---|
| `twitch_assist/plugin.yaml` | Manifest: name, version, `requires_hermes`, env vars |
| `twitch_assist/__init__.py` | Exposes `register` to Hermes |
| `twitch_assist/adapter.py` | IRC connection, gateway adapter, dispatch, Backseat vote runner, `register()` |
| `twitch_assist/protocol.py` | Pure logic: IRC parsing, trigger filter, cooldown, vote tally, reply splitting, notice filter |
| `tests/` | 30 protocol tests and 16 adapter tests against a fake IRC server |
| `scripts/live_test.py` | Dev helper: posts as the bot and records chat with an anonymous reader |
| `scripts/dev-run.ps1` | Dev helper: runs the gateway with the token loaded into that process only |

Hermes surfaces used: `ctx.register_platform`, `BasePlatformAdapter`, `toolsets_for_source`, `platform_hint`, and env enablement.

## Privacy and network disclosure

The plugin makes exactly two kinds of network call:

- `irc.chat.twitch.tv:6697`, TLS IRC, to read and post in your channel.
- `https://id.twitch.tv/oauth2/validate`, to check your token and learn its login.

It has no telemetry, runs no shell commands, starts no background process beyond the gateway connection, and reads credentials only through Hermes's secret scope. Questions typed in your chat are sent to the model provider you configured in Hermes, and web search runs through whatever search backend your Hermes uses. Both are Hermes behaviour, not this plugin's.

## Limitations

- **Real network drop is not proven live.** Reconnect is proven against a local fake IRC server. On a real drop the Hermes base-class watcher retries with a 30 to 300 second backoff.
- **A running vote is lost** if the connection drops or the gateway restarts.
- **Emoji-led answers:** a real answer that starts with certain emoji (warning sign, lightning bolt and similar) is treated as a gateway notice and dropped.
- **Backseat tested live with one voter.** Multi-voter, tie, cancel and mod paths are covered by tests only.
- **No native polls or channel points.** Votes are chat commands, because my channel is not Affiliate. No moderation actions (timeouts, bans) are implemented.
- **One channel per gateway.**

## License

[MIT](LICENSE)
