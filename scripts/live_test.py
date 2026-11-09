"""Live Twitch test helper (dev only). Never prints the token.

  python live_test.py say "<text>" [--wait N]   # post as the bot account over a SECOND connection (same token)
                                                # and capture what an anonymous justinfan reader sees
  python live_test.py read --wait N             # only capture with an anonymous reader
  python live_test.py seq --wait N --step "0:!backseat q | a | b" --step "8:!vote 2"
                                                # send a timed sequence of messages (second connection, delay
                                                # in seconds from start) while an anonymous reader records
                                                # every chat line with a timestamp
"""
import argparse
import asyncio
import os
import random
import re
import ssl
import time

# Set both env vars before running: a text file holding your chat token, and the channel to test in.
TOKEN_FILE = os.environ["TWITCH_TOKEN_FILE"]
CHANNEL = os.environ["TWITCH_CHANNEL"]


async def _conn():
    return await asyncio.open_connection("irc.chat.twitch.tv", 6697, ssl=ssl.create_default_context())


async def _send(w, line):
    w.write((line + "\r\n").encode())
    await w.drain()


async def reader_task(seconds, out):
    r, w = await _conn()
    await _send(w, f"NICK justinfan{random.randint(10000, 99999)}")
    await _send(w, "CAP REQ :twitch.tv/tags")
    await _send(w, f"JOIN #{CHANNEL}")
    end = time.time() + seconds
    while time.time() < end:
        try:
            data = await asyncio.wait_for(r.readuntil(b"\r\n"), timeout=max(0.1, end - time.time()))
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            break
        line = data.decode(errors="replace").rstrip("\r\n")
        if line.startswith("PING"):
            await _send(w, "PONG :tmi.twitch.tv")
        elif " PRIVMSG " in line:
            out.append((time.strftime("%H:%M:%S"), line))
    w.close()


async def open_sender():
    token = open(TOKEN_FILE).read().strip()
    token = token[6:] if token.lower().startswith("oauth:") else token
    r, w = await _conn()
    await _send(w, f"PASS oauth:{token}")
    await _send(w, f"NICK {CHANNEL}")
    await _send(w, f"JOIN #{CHANNEL}")
    welcomed = False
    end = time.time() + 15
    while time.time() < end and not welcomed:
        data = await asyncio.wait_for(r.readuntil(b"\r\n"), timeout=15)
        line = data.decode(errors="replace")
        if " 001 " in line:
            welcomed = True
        if "authentication failed" in line.lower():
            raise SystemExit("AUTH FAILED")
    await asyncio.sleep(1.0)
    return w


async def say(text):
    w = await open_sender()
    await _send(w, f"PRIVMSG #{CHANNEL} :{text}")
    print(time.strftime("%H:%M:%S"), "SENT as second connection:", text)
    await asyncio.sleep(1.0)
    w.close()


async def seq(steps):
    w = await open_sender()
    t0 = time.time()
    for delay, text in steps:
        await asyncio.sleep(max(0.0, t0 + delay - time.time()))
        await _send(w, f"PRIVMSG #{CHANNEL} :{text}")
        print(time.strftime("%H:%M:%S"), "SENT as second connection:", text)
    await asyncio.sleep(1.0)
    w.close()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["say", "read", "seq"])
    ap.add_argument("text", nargs="?", default="")
    ap.add_argument("--wait", type=int, default=25)
    ap.add_argument("--step", action="append", default=[], help="DELAY:TEXT (seq mode)")
    a = ap.parse_args()
    seen = []
    rt = asyncio.create_task(reader_task(a.wait, seen))
    await asyncio.sleep(3)  # reader joined
    if a.mode == "say":
        await say(a.text)
    elif a.mode == "seq":
        await seq([(float(x.split(":", 1)[0]), x.split(":", 1)[1]) for x in a.step])
    await rt
    print("--- anonymous reader captured %d PRIVMSG line(s) ---" % len(seen))
    for ts, line in seen:
        m = re.search(r"display-name=([^;]*);.*? PRIVMSG #\S+ :(.*)$", line)
        who, msg = (m.group(1), m.group(2)) if m else ("?", line)
        print(ts, f"[{who}]", msg.encode("ascii", "backslashreplace").decode())


asyncio.run(main())
