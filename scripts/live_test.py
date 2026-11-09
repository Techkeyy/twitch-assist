"""Live Twitch test helper (dev only). Never prints the token.

  python live_test.py say "<text>" [--wait N]   # post as the bot account over a SECOND connection (same token)
                                                # and capture what an anonymous justinfan reader sees
  python live_test.py read --wait N             # only capture with an anonymous reader
"""
import argparse
import asyncio
import random
import re
import ssl
import time

TOKEN_FILE = r"C:\Users\HomePC\Desktop\Twitch Access.txt"
CHANNEL = "iszee24"


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


async def say(text):
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
    await _send(w, f"PRIVMSG #{CHANNEL} :{text}")
    print(time.strftime("%H:%M:%S"), "SENT as second connection:", text)
    await asyncio.sleep(1.0)
    w.close()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["say", "read"])
    ap.add_argument("text", nargs="?", default="")
    ap.add_argument("--wait", type=int, default=25)
    a = ap.parse_args()
    seen = []
    rt = asyncio.create_task(reader_task(a.wait, seen))
    await asyncio.sleep(3)  # reader joined
    if a.mode == "say":
        await say(a.text)
    await rt
    print("--- anonymous reader captured %d PRIVMSG line(s) ---" % len(seen))
    for ts, line in seen:
        m = re.search(r"display-name=([^;]*);.*? PRIVMSG #\S+ :(.*)$", line)
        print(ts, "RAW:", line.encode("ascii", "backslashreplace").decode())


asyncio.run(main())
