#!/usr/bin/env python3
from __future__ import annotations
import argparse, asyncio, json, random, re, sys, time, unicodedata, urllib.error, urllib.request, uuid
from urllib.parse import urlparse
import websockets

REALTIME_BASE = "https://web.kick.com/api/v1/realtime"
CHANNEL_API = "https://kick.com/api/v2/channels/{slug}"
FALLBACK_PUSHER_KEY = "34bf7a0ff419a2a775b9"
FALLBACK_PUSHER_CLUSTER = "us2"
CHAT_EVENT = "App\\Events\\ChatMessageEvent"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
HDRS = {"User-Agent": UA, "Accept": "application/json",
        "Origin": "https://kick.com", "x-app-platform": "web"}

SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_FRAME = 256 * 1024
MAX_HTTP = 2 * 1024 * 1024
MAX_CONTENT = 500

def clean(text: object, limit: int = MAX_CONTENT) -> str:
    if not isinstance(text, str): return ""
    out = "".join(ch for ch in text if ch in (" ",) or unicodedata.category(ch)[0] not in ("C",))
    return out[:limit]

def http_json(url: str, body: dict | None = None, timeout: float = 15.0) -> dict:
    if not url.startswith("https://"): raise ValueError("Yalnizca https izinli")
    data = json.dumps(body).encode() if body is not None else None
    headers = dict(HDRS)
    if data is not None: headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(MAX_HTTP + 1)
    if len(raw) > MAX_HTTP: raise ValueError("HTTP yaniti cok buyuk")
    obj = json.loads(raw)
    if not isinstance(obj, dict): raise ValueError("Beklenmeyen JSON")
    return obj

def resolve_channel(slug: str) -> tuple[int, int]:
    d = http_json(CHANNEL_API.format(slug=slug))
    cid, rid = d.get("id"), (d.get("chatroom") or {}).get("id")
    if not (isinstance(cid, int) and isinstance(rid, int)): raise ValueError("Kanal yanitinda id bulunamadi")
    return cid, rid

def negotiate(client_id: str, providers: list[str], channel_id: int | None = None) -> dict:
    path = f"/channels/{int(channel_id)}/chat/connection" if channel_id else "/connection"
    resp = http_json(REALTIME_BASE + path, {
        "client": {"id": client_id, "type": "web"},
        "capabilities": {"accepted_providers": [{"provider": p} for p in providers]},
    })
    for c in (resp.get("data") or {}).get("connections") or []:
        if not isinstance(c, dict): continue
        cred = c.get("credentials") or {}
        if c.get("provider") == "pusher":
            k, cl = str(cred.get("app_key", "")), str(cred.get("cluster", ""))
            if k.isascii() and k.isalnum() and cl.isascii() and cl.isalnum() and len(k) <= 64 and len(cl) <= 16:
                return {"provider": "pusher", "key": k, "cluster": cl}
        if c.get("provider") == "centrifugo":
            u = urlparse(str(cred.get("url", "")))
            host = u.hostname or ""
            if u.scheme == "wss" and host.startswith("realtime.") and host.endswith(".platform.kick.com") and u.path == "/connection/websocket" and not u.query:
                return {"provider": "centrifugo", "url": u.geturl()}
    raise ValueError("Desteklenen saglayici donmedi")

class Game:
    def __init__(self) -> None:
        self.secret = random.randint(1, 100)
        self.points: dict[str, int] = {}
        self.cooldown: dict[str, float] = {}
    def handle(self, user: str, msg: str) -> str | None:
        if not msg.startswith("!"): return None
        now = time.monotonic()
        if now - self.cooldown.get(user, 0) < 3: return None
        self.cooldown[user] = now
        if len(self.cooldown) > 50_000: self.cooldown.clear()
        parts = msg.split()
        cmd = parts[0].lower()
        if cmd == "!zar": return f"{user} zar atti: {random.randint(1, 6)}"
        if cmd == "!tahmin" and len(parts) > 1 and parts[1].isdigit() and len(parts[1]) <= 3:
            n = int(parts[1])
            if n == self.secret:
                self.points[user] = self.points.get(user, 0) + 10
                self.secret = random.randint(1, 100)
                return f"*** {user} BILDI ({n})! +10 puan. Yeni sayi secildi."
            return f"{user}: {n} -> {'daha BUYUK' if n < self.secret else 'daha KUCUK'}"
        if cmd == "!puan": return f"{user} puani: {self.points.get(user, 0)}"
        if cmd == "!top":
            top = sorted(self.points.items(), key=lambda x: -x[1])[:5]
            return "Liderler: " + (", ".join(f"{u}={p}" for u, p in top) or "henuz yok")
        return None

def on_chat(data: object, game: Game) -> None:
    if isinstance(data, str):
        try: data = json.loads(data)
        except ValueError: return
    if not isinstance(data, dict): return
    sender = data.get("sender")
    user = clean(sender.get("username") if isinstance(sender, dict) else None, 40) or "?"
    text = clean(data.get("content"))
    print(f"[{time.strftime('%H:%M:%S')}] {user}: {text}", flush=True)
    reply = game.handle(user, text)
    if reply: print(f"    >> OYUN: {reply}", flush=True)

class KeyRejected(Exception): pass

async def run_pusher(key: str, cluster: str, chatroom_id: int, game: Game) -> None:
    url = (f"wss://ws-{cluster}.pusher.com/app/{key}?protocol=7&client=js&version=8.4.0&flash=false")
    async with websockets.connect(url, max_size=MAX_FRAME, open_timeout=15, ping_interval=None) as ws:
        first = json.loads(await asyncio.wait_for(ws.recv(), 15))
        if first.get("event") == "pusher:error": raise KeyRejected(str(first.get("data"))[:200])
        timeout = 120
        try: timeout = int(json.loads(first["data"]).get("activity_timeout", 120))
        except (KeyError, ValueError, TypeError): pass
        await ws.send(json.dumps({"event": "pusher:subscribe", "data": {"auth": "", "channel": f"chatrooms.{chatroom_id}.v2"}}))
        print(f"[+] Pusher baglandi (key={key}, cluster={cluster})", flush=True)
        while True:
            try: raw = await asyncio.wait_for(ws.recv(), timeout)
            except asyncio.TimeoutError:
                await ws.send(json.dumps({"event": "pusher:ping", "data": {}}))
                continue
            m = json.loads(raw)
            ev = m.get("event")
            if ev == "pusher:ping": await ws.send(json.dumps({"event": "pusher:pong", "data": {}}))
            elif ev == "pusher:error":
                code = (m.get("data") or {}).get("code")
                if code in (4001, 4003, 4007): raise KeyRejected(str(m.get("data"))[:200])
                raise ConnectionError(str(m.get("data"))[:200])
            elif ev == "pusher_internal:subscription_succeeded": print(f"[+] chatrooms.{chatroom_id}.v2 dinleniyor...\n", flush=True)
            elif ev == CHAT_EVENT: on_chat(m.get("data"), game)

async def run_centrifugo(url: str, client_id: str, chatroom_id: int, game: Game) -> None:
    token = http_json(REALTIME_BASE + "/auth/connection", {"client_id": client_id})
    token = str((token.get("data") or {}).get("token", ""))
    if not token or len(token) > 4096: raise ConnectionError("Anonim token alinamadi")
    async with websockets.connect(url, max_size=MAX_FRAME, open_timeout=15, origin="https://kick.com", user_agent_header=UA, ping_interval=None) as ws:
        await ws.send(json.dumps({"id": 1, "connect": {"token": token, "name": "js", "version": "1"}}))
        await ws.send(json.dumps({"id": 2, "subscribe": {"channel": f"chatrooms.{chatroom_id}.v2"}}))
        refresh_at, next_id = float("inf"), 3
        while True:
            raw = await asyncio.wait_for(ws.recv(), 90)
            for line in str(raw).splitlines():
                it = json.loads(line)
                if not isinstance(it, dict): continue
                if not it:
                    await ws.send("{}")
                    continue
                if "error" in it: raise ConnectionError(f"Centrifugo hatasi: {str(it['error'])[:200]}")
                if it.get("id") == 1:
                    ttl = int((it.get("connect") or {}).get("ttl", 0) or 0)
                    refresh_at = time.monotonic() + max(ttl - 60, 30) if ttl else float("inf")
                    print("[+] Centrifugo baglandi", flush=True)
                elif it.get("id") == 2: print(f"[+] chatrooms.{chatroom_id}.v2 dinleniyor...\n", flush=True)
                pub = (it.get("push") or {}).get("pub")
                if pub:
                    d = pub.get("data")
                    d = json.loads(d) if isinstance(d, str) else d
                    if isinstance(d, dict) and d.get("event") == CHAT_EVENT: on_chat(d.get("data"), game)
            if time.monotonic() > refresh_at:
                t = http_json(REALTIME_BASE + "/auth/connection", {"client_id": client_id})
                await ws.send(json.dumps({"id": next_id, "refresh": {"token": str((t.get("data") or {}).get("token", ""))}}))
                next_id += 1
                refresh_at = float("inf")

async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("channel")
    ap.add_argument("--chatroom-id", type=int)
    ap.add_argument("--channel-id", type=int)
    ap.add_argument("--transport", choices=["auto", "pusher", "centrifugo"], default="auto")
    a = ap.parse_args()

    if not SLUG_RE.match(a.channel): sys.exit("Gecersiz kanal adi")
    slug = a.channel.lower()
    channel_id, chatroom_id = a.channel_id, a.chatroom_id
    if chatroom_id is None:
        try: channel_id, chatroom_id = resolve_channel(slug)
        except (urllib.error.URLError, ValueError) as e:
            sys.exit(f"Kanal bilgisi alinamadi ({e}). Tarayicida https://kick.com/api/v2/channels/{slug}/chatroom acip 'id' degerini --chatroom-id ile verin.")
    print(f"[i] kanal={slug} channel_id={channel_id} chatroom_id={chatroom_id}", flush=True)

    game, client_id, backoff = Game(), str(uuid.uuid4()), 1
    mode = a.transport
    while True:
        try:
            if mode in ("auto", "pusher"):
                try: cfg = negotiate(client_id, ["pusher"])
                except (urllib.error.URLError, ValueError) as e:
                    print(f"[!] Muzakere basarisiz ({e}); yedek anahtar kullaniliyor", flush=True)
                    cfg = {"key": FALLBACK_PUSHER_KEY, "cluster": FALLBACK_PUSHER_CLUSTER}
                try: await run_pusher(cfg["key"], cfg["cluster"], chatroom_id, game)
                except KeyRejected as e:
                    print(f"[!] Pusher anahtari reddedildi: {e}", flush=True)
                    if mode == "pusher": raise ConnectionError("pusher reddedildi")
                    mode = "centrifugo"
                    continue
            else:
                cfg = negotiate(client_id, ["centrifugo"], channel_id)
                await run_centrifugo(cfg["url"], client_id, chatroom_id, game)
            backoff = 1
        except (websockets.WebSocketException, OSError, ConnectionError, asyncio.TimeoutError, ValueError) as e:
            print(f"[!] Baglanti koptu: {type(e).__name__}: {str(e)[:150]} -> {backoff}s sonra yeniden", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try: _s.reconfigure(errors="replace")
        except (AttributeError, ValueError): pass
    try: asyncio.run(main())
    except KeyboardInterrupt: print("\nCikis.")
