#!/usr/bin/env python3
"""
YAYINCI VS OYUNU  -  Kick sohbetinden okuyan, SALT-OKUNUR, hafif PyGame oyunu.

Kurulum :  pip install pygame websockets
Calistir:  python kickvs.py
EXE     :  pyinstaller --onefile --collect-submodules websockets --name YayinciVS kickvs.py

Dosya bolumleri (yukaridan asagiya):
  1) AYARLAR
  2) KICK BAGLANTISI   (senin calisan chat kodun, kuyruga yazacak sekilde uyarlandi)
  3) CHAT PARSER       (komutlari ayirir)
  4) OYUNLAR           (Game tabani + Matematik Ringi)
  5) GAMEMANAGER       (davet / !kabul eslestirme)
  6) ARAYUZ + ANA DONGU
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import random
import re
import sys
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass
from urllib.parse import urlparse

import pygame
import websockets

# =====================================================================
# 1) AYARLAR
# =====================================================================
W, H = 1024, 576          # kucuk pencere = dusuk CPU; OBS'de buyutulebilir
FPS = 30                  # 30'u gecmez
GREEN = (0, 255, 0)       # chroma key zemini
PANEL = (24, 24, 36)      # yazi arkasi duz (saydam degil) panel -> yesil halo olmaz
WHITE = (255, 255, 255)
RED, DARKRED = (225, 40, 40), (90, 20, 20)
BLUE, ORANGE = (70, 130, 235), (240, 150, 30)
YELLOW = (255, 220, 60)
GRAY = (130, 130, 145)
SKIN = (250, 220, 180)

INVITE_TTL = 90           # davet kac sn gecerli
RESULT_SECS = 8           # maç sonu ekranda kac sn kalsin
LOG_LINES = 4             # alttaki sohbet logu satir sayisi

# =====================================================================
# 2) KICK BAGLANTISI  (calisan kodundan; degisen tek sey: Game yerine kuyruga yazar)
# =====================================================================
REALTIME_BASE = "https://web.kick.com/api/v1/realtime"
CHANNEL_API = "https://kick.com/api/v2/channels/{slug}"
FALLBACK_PUSHER_KEY = "34bf7a0ff419a2a775b9"      # sadece muzakere basarisiz olursa
FALLBACK_PUSHER_CLUSTER = "us2"
CHAT_EVENT = "App\\Events\\ChatMessageEvent"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
HDRS = {"User-Agent": UA, "Accept": "application/json",
        "Origin": "https://kick.com", "x-app-platform": "web"}
SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
EMOTE_RE = re.compile(r"\[emote:\d+:[^\]]*\]")
MAX_FRAME = 256 * 1024
MAX_HTTP = 2 * 1024 * 1024
MAX_CONTENT = 500


def clean(text: object, limit: int = MAX_CONTENT) -> str:
    """Kontrol karakterlerini atar, uzunlugu sinirlar."""
    if not isinstance(text, str):
        return ""
    out = "".join(ch for ch in text if ch == " " or unicodedata.category(ch)[0] != "C")
    return out[:limit]


def http_json(url: str, body: dict | None = None, timeout: float = 15.0) -> dict:
    if not url.startswith("https://"):
        raise ValueError("Yalnizca https izinli")
    data = json.dumps(body).encode() if body is not None else None
    headers = dict(HDRS)
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(MAX_HTTP + 1)
    if len(raw) > MAX_HTTP:
        raise ValueError("HTTP yaniti cok buyuk")
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise ValueError("Beklenmeyen JSON")
    return obj


def resolve_channel(slug: str) -> tuple[int, int]:
    d = http_json(CHANNEL_API.format(slug=slug))
    cid, rid = d.get("id"), (d.get("chatroom") or {}).get("id")
    if not (isinstance(cid, int) and isinstance(rid, int)):
        raise ValueError("Kanal yanitinda id bulunamadi")
    return cid, rid


def negotiate(client_id: str, providers: list[str], channel_id: int | None = None) -> dict:
    """Kick'ten ANLIK baglanti anahtarini ister (sabit anahtara bagli kalmamak icin)."""
    path = f"/channels/{int(channel_id)}/chat/connection" if channel_id else "/connection"
    resp = http_json(REALTIME_BASE + path, {
        "client": {"id": client_id, "type": "web"},
        "capabilities": {"accepted_providers": [{"provider": p} for p in providers]},
    })
    for c in (resp.get("data") or {}).get("connections") or []:
        if not isinstance(c, dict):
            continue
        cred = c.get("credentials") or {}
        if c.get("provider") == "pusher":
            k, cl = str(cred.get("app_key", "")), str(cred.get("cluster", ""))
            if k.isascii() and k.isalnum() and cl.isascii() and cl.isalnum() and len(k) <= 64 and len(cl) <= 16:
                return {"provider": "pusher", "key": k, "cluster": cl}
        if c.get("provider") == "centrifugo":
            u = urlparse(str(cred.get("url", "")))
            host = u.hostname or ""
            if (u.scheme == "wss" and host.startswith("realtime.") and host.endswith(".platform.kick.com")
                    and u.path == "/connection/websocket" and not u.query):
                return {"provider": "centrifugo", "url": u.geturl()}
    raise ValueError("Desteklenen saglayici donmedi")


class KeyRejected(Exception):
    pass


class Sink:
    """Ag thread'inden ana thread'e giden kuyruk. ("chat", user, text) / ("status", msg, "") / ("fatal", msg, "")"""

    def __init__(self, q: "queue.Queue[tuple[str, str, str]]") -> None:
        self.q = q

    def chat(self, user: str, text: str) -> None:
        self.q.put(("chat", user, text))

    def status(self, msg: str) -> None:
        print(f"[i] {msg}", flush=True)
        self.q.put(("status", msg, ""))

    def fatal(self, msg: str) -> None:
        print(f"[HATA] {msg}", flush=True)
        self.q.put(("fatal", msg, ""))


def on_chat(data: object, sink: Sink) -> None:
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return
    if not isinstance(data, dict):
        return
    sender = data.get("sender")
    user = clean(sender.get("username") if isinstance(sender, dict) else None, 40) or "?"
    text = clean(data.get("content"))
    text = EMOTE_RE.sub("", text).strip()
    if text:
        sink.chat(user, text)


async def run_pusher(key: str, cluster: str, chatroom_id: int, sink: Sink) -> None:
    url = f"wss://ws-{cluster}.pusher.com/app/{key}?protocol=7&client=js&version=8.4.0&flash=false"
    async with websockets.connect(url, max_size=MAX_FRAME, open_timeout=15, ping_interval=None) as ws:
        first = json.loads(await asyncio.wait_for(ws.recv(), 15))
        if first.get("event") == "pusher:error":
            raise KeyRejected(str(first.get("data"))[:200])
        timeout = 120
        try:
            timeout = int(json.loads(first["data"]).get("activity_timeout", 120))
        except (KeyError, ValueError, TypeError):
            pass
        await ws.send(json.dumps({"event": "pusher:subscribe",
                                  "data": {"auth": "", "channel": f"chatrooms.{chatroom_id}.v2"}}))
        print(f"[+] Pusher baglandi (key={key}, cluster={cluster})", flush=True)
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout)
            except asyncio.TimeoutError:
                await ws.send(json.dumps({"event": "pusher:ping", "data": {}}))
                continue
            m = json.loads(raw)
            ev = m.get("event")
            if ev == "pusher:ping":
                await ws.send(json.dumps({"event": "pusher:pong", "data": {}}))
            elif ev == "pusher:error":
                code = (m.get("data") or {}).get("code")
                if code in (4001, 4003, 4007):
                    raise KeyRejected(str(m.get("data"))[:200])
                raise ConnectionError(str(m.get("data"))[:200])
            elif ev == "pusher_internal:subscription_succeeded":
                sink.status(f"Sohbet bagli (chatroom {chatroom_id})")
            elif ev == CHAT_EVENT:
                on_chat(m.get("data"), sink)


async def run_centrifugo(url: str, client_id: str, chatroom_id: int, sink: Sink) -> None:
    token = http_json(REALTIME_BASE + "/auth/connection", {"client_id": client_id})
    token = str((token.get("data") or {}).get("token", ""))
    if not token or len(token) > 4096:
        raise ConnectionError("Anonim token alinamadi")
    async with websockets.connect(url, max_size=MAX_FRAME, open_timeout=15, origin="https://kick.com",
                                  user_agent_header=UA, ping_interval=None) as ws:
        await ws.send(json.dumps({"id": 1, "connect": {"token": token, "name": "js", "version": "1"}}))
        await ws.send(json.dumps({"id": 2, "subscribe": {"channel": f"chatrooms.{chatroom_id}.v2"}}))
        refresh_at, next_id = float("inf"), 3
        while True:
            raw = await asyncio.wait_for(ws.recv(), 90)
            for line in str(raw).splitlines():
                it = json.loads(line)
                if not isinstance(it, dict):
                    continue
                if not it:
                    await ws.send("{}")
                    continue
                if "error" in it:
                    raise ConnectionError(f"Centrifugo hatasi: {str(it['error'])[:200]}")
                if it.get("id") == 1:
                    ttl = int((it.get("connect") or {}).get("ttl", 0) or 0)
                    refresh_at = time.monotonic() + max(ttl - 60, 30) if ttl else float("inf")
                    print("[+] Centrifugo baglandi", flush=True)
                elif it.get("id") == 2:
                    sink.status(f"Sohbet bagli (chatroom {chatroom_id})")
                pub = (it.get("push") or {}).get("pub")
                if pub:
                    d = pub.get("data")
                    d = json.loads(d) if isinstance(d, str) else d
                    if isinstance(d, dict) and d.get("event") == CHAT_EVENT:
                        on_chat(d.get("data"), sink)
            if time.monotonic() > refresh_at:
                t = http_json(REALTIME_BASE + "/auth/connection", {"client_id": client_id})
                await ws.send(json.dumps({"id": next_id,
                                          "refresh": {"token": str((t.get("data") or {}).get("token", ""))}}))
                next_id += 1
                refresh_at = float("inf")


class ChatReader(threading.Thread):
    """Ayri thread'de asyncio calistirir; oyun dongusunu bloklamaz."""

    def __init__(self, slug: str, chatroom_id: int | None, q: "queue.Queue[tuple[str, str, str]]") -> None:
        super().__init__(daemon=True)
        self.slug = slug.lower()
        self.chatroom_id = chatroom_id
        self.channel_id: int | None = None
        self.sink = Sink(q)

    def run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as e:  # beklenmeyen her sey konsola + ekrana
            traceback.print_exc()
            self.sink.fatal(f"Beklenmeyen hata: {type(e).__name__}: {str(e)[:120]}")

    async def _main(self) -> None:
        sink = self.sink
        chatroom_id = self.chatroom_id
        if chatroom_id is None:
            sink.status("Kanal bilgisi aliniyor...")
            try:
                self.channel_id, chatroom_id = resolve_channel(self.slug)
            except (urllib.error.URLError, ValueError) as e:
                sink.fatal(f"Kanal bulunamadi ({e}). Chatroom ID'yi elle gir "
                           f"(kick.com/api/v2/channels/{self.slug}/chatroom -> 'id').")
                return
        print(f"[i] kanal={self.slug} channel_id={self.channel_id} chatroom_id={chatroom_id}", flush=True)

        client_id, backoff, mode = str(uuid.uuid4()), 1, "auto"
        sink.status("Baglaniliyor...")
        while True:
            try:
                if mode in ("auto", "pusher"):
                    try:
                        cfg = negotiate(client_id, ["pusher"])
                    except (urllib.error.URLError, ValueError) as e:
                        print(f"[!] Muzakere basarisiz ({e}); yedek anahtar kullaniliyor", flush=True)
                        cfg = {"key": FALLBACK_PUSHER_KEY, "cluster": FALLBACK_PUSHER_CLUSTER}
                    try:
                        await run_pusher(cfg["key"], cfg["cluster"], chatroom_id, sink)
                    except KeyRejected as e:
                        print(f"[!] Pusher anahtari reddedildi: {e}", flush=True)
                        mode = "centrifugo"
                        continue
                else:
                    cfg = negotiate(client_id, ["centrifugo"], self.channel_id)
                    await run_centrifugo(cfg["url"], client_id, chatroom_id, sink)
                backoff = 1
            except (websockets.WebSocketException, OSError, ConnectionError,
                    asyncio.TimeoutError, ValueError) as e:
                print(f"[!] Baglanti koptu: {type(e).__name__}: {str(e)[:150]} -> {backoff}s sonra yeniden", flush=True)
                sink.status(f"Baglanti koptu, {backoff} sn sonra tekrar denenecek")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)


# =====================================================================
# 3) CHAT PARSER
# =====================================================================
@dataclass
class Command:
    name: str            # kucuk harf, "!" dahil  (orn "!maç")
    arg: str | None      # "@" ve buyuk/kucuk harf temizlenmis kullanici adi


def parse_command(text: str) -> Command | None:
    """'!kabul @Ali' -> Command('!kabul','ali');  '!maç' -> Command('!maç',None); komut degilse None."""
    text = text.strip()
    if not text.startswith("!") or len(text) < 2:
        return None
    parts = text.split()
    name = parts[0].lower()
    arg = parts[1].lstrip("@").lower() if len(parts) > 1 else None
    if arg is not None and not re.fullmatch(r"[a-z0-9_\-]{1,40}", arg):
        arg = None
    return Command(name, arg)


# =====================================================================
# 4) OYUNLAR
# =====================================================================
class Game:
    """Her yeni oyun bu siniftan turetilir ve GAMES sozlugune eklenir."""
    TITLE = "Oyun"

    def __init__(self, p1: str, p2: str) -> None:
        self.p1, self.p2 = p1, p2
        self.finished = False
        self.finished_for = 0.0      # bittikten sonra gecen sn

    def on_message(self, user: str, text: str) -> None: ...
    def update(self, dt: float) -> None: ...
    def draw(self, screen: pygame.Surface, ui: "UI") -> None: ...


def make_question(d: int) -> tuple[str, int]:
    """d = basamak seviyesi (1..3). Islem rastgele; bolmede tam bolunen sayilar secilir."""
    op = random.choice("+-*/")
    lo, hi = (1, 9) if d == 1 else (10 ** (d - 1), 10 ** d - 1)
    if op == "+":
        a, b = random.randint(lo, hi), random.randint(lo, hi)
        return f"{a} + {b}", a + b
    if op == "-":
        a, b = sorted((random.randint(lo, hi), random.randint(lo, hi)), reverse=True)
        return f"{a} - {b}", a - b
    if op == "*":
        if d == 1:
            a, b = random.randint(2, 9), random.randint(2, 9)
        elif d == 2:
            a, b = random.randint(10, 99), random.randint(2, 9)
        else:
            a, b = random.randint(11, 99), random.randint(11, 19)
        return f"{a} \u00d7 {b}", a * b
    # bolme: once bolen ve sonucu sec, bolunen = bolen * sonuc  -> hep tam bolunur
    if d == 1:
        b, q = random.randint(2, 9), random.randint(2, 9)
    elif d == 2:
        b, q = random.randint(2, 9), random.randint(10, 99)
    else:
        b, q = random.randint(6, 19), random.randint(10, 99)
    return f"{b * q} \u00f7 {b}", q


class MathRing(Game):
    TITLE = "Matematik Ringi"
    MATCH_SECS = 60
    INTRO_SECS = 3
    WRONG_DMG = 6            # yanlis cevap -> kendine
    STALE_SECS = 20          # kimse cozmezse soru degisir

    def __init__(self, p1: str, p2: str) -> None:
        super().__init__(p1, p2)
        self.keys = (p1.lower(), p2.lower())
        self.hp = [100, 100]
        self.time_left = float(self.MATCH_SECS)
        self.intro = float(self.INTRO_SECS)
        self.q_text, self.answer, self.q_age = "", 0, 0.0
        self.lock = [0.0, 0.0]               # yanlis cevap spam korumasi
        self.flash = [0.0, 0.0]              # vurulma animasyonu suresi
        self.last_dmg = [0, 0]
        self.event = ""
        self.winner: str | None = None       # None + finished => berabere
        self.t = 0.0

    # ---- zorluk: gecen sureye VE dusuk cana gore basamak artar ----
    def digits(self) -> int:
        elapsed = self.MATCH_SECS - self.time_left
        lvl = int(elapsed // 20)             # 0-20sn:0  20-40sn:1  40-60sn:2
        low = min(self.hp)
        if low <= 60:
            lvl += 1
        if low <= 30:
            lvl += 1
        return min(1 + lvl, 3)

    def new_question(self) -> None:
        self.q_text, self.answer = make_question(self.digits())
        self.q_age = 0.0

    # ---- girdi ----
    def on_message(self, user: str, text: str) -> None:
        if self.finished or self.intro > 0:
            return
        k = user.lower()
        if k not in self.keys:
            return
        if not re.fullmatch(r"-?\d{1,7}", text.strip()):
            return                           # sayi degilse sohbettir, yok say
        i = self.keys.index(k)
        if self.lock[i] > 0:
            return
        val = int(text.strip())
        names = (self.p1, self.p2)
        if val == self.answer:
            dmg = 15 + (5 if self.q_age < 4 else 3 if self.q_age < 8 else 0)   # hizli = 20, yavas = 15
            self.hit(1 - i, dmg)
            self.event = f"{names[i]} dogru bildi!  -{dmg}"
            if not self.finished:
                self.new_question()
        else:
            self.lock[i] = 1.0
            self.hit(i, self.WRONG_DMG)
            self.event = f"{names[i]} yanlis cevap!  -{self.WRONG_DMG} (kendine)"

    def hit(self, i: int, dmg: int) -> None:
        self.hp[i] = max(0, self.hp[i] - dmg)
        self.flash[i], self.last_dmg[i] = 0.8, dmg
        if self.hp[i] <= 0:
            self.end(winner_idx=1 - i)

    def end(self, winner_idx: int | None = None) -> None:
        if self.finished:
            return
        if winner_idx is None:               # sure bitti -> cani cok olan kazanir
            if self.hp[0] != self.hp[1]:
                winner_idx = 0 if self.hp[0] > self.hp[1] else 1
        self.winner = None if winner_idx is None else (self.p1, self.p2)[winner_idx]
        self.finished = True

    # ---- zaman ----
    def update(self, dt: float) -> None:
        self.t += dt
        for i in (0, 1):
            self.flash[i] = max(0.0, self.flash[i] - dt)
            self.lock[i] = max(0.0, self.lock[i] - dt)
        if self.finished:
            self.finished_for += dt
            return
        if self.intro > 0:
            self.intro -= dt
            if self.intro <= 0:
                self.new_question()
            return
        self.time_left -= dt
        self.q_age += dt
        if self.q_age > self.STALE_SECS:
            self.new_question()
        if self.time_left <= 0:
            self.time_left = 0
            self.end()

    # ---- cizim ----
    def draw(self, screen: pygame.Surface, ui: "UI") -> None:
        # ring zemini + halatlar (basit cizgiler)
        pygame.draw.rect(screen, (125, 125, 140), (50, 360, W - 100, 40))
        pygame.draw.rect(screen, (70, 70, 85), (50, 400, W - 100, 8))
        for x in (70, W - 70):
            pygame.draw.rect(screen, (200, 200, 210), (x - 6, 250, 12, 110))
        for y, c in ((275, RED), (308, WHITE), (341, RED)):
            pygame.draw.line(screen, c, (70, y), (W - 70, y), 4)

        ui.label(screen, f"S\u00fcre: {int(self.time_left + 0.99)} sn", "md", (W // 2, 34))

        for i, (cx, color, name) in enumerate(((190, BLUE, self.p1), (W - 190, ORANGE, self.p2))):
            alive = self.hp[i] > 0
            self.draw_fighter(screen, ui, cx, color, name, i, alive)

        if self.finished:
            txt = f"KAZANAN: {self.winner}" if self.winner else "BERABERE!"
            ui.label(screen, txt, "lg", (W // 2, 170), fg=YELLOW, pad=24)
            ui.label(screen, f"{self.p1} {self.hp[0]} HP   -   {self.hp[1]} HP {self.p2}", "sm", (W // 2, 235))
        elif self.intro > 0:
            ui.label(screen, f"{self.p1}  VS  {self.p2}", "lg", (W // 2, 150), pad=20)
            ui.label(screen, f"{int(self.intro) + 1}", "xl", (W // 2, 240), fg=YELLOW, pad=20)
        else:
            ui.label(screen, f"{self.q_text} = ?", "xl", (W // 2, 170), pad=26)
            if self.event:
                ui.label(screen, self.event, "sm", (W // 2, 450))

    def draw_fighter(self, screen, ui, cx, color, name, i, alive) -> None:
        off = (7 if int(self.t * 30) % 2 else -7) if self.flash[i] > 0.55 else 0   # kisa titreme
        x = cx + off
        body = color if alive else (110, 110, 118)
        pygame.draw.rect(screen, body, (x - 30, 260, 60, 85), border_radius=6)          # govde
        pygame.draw.circle(screen, SKIN if alive else (165, 165, 170), (x, 232), 24)    # kafa
        pygame.draw.circle(screen, (20, 20, 20), (x - 8, 228), 3)                       # gozler
        pygame.draw.circle(screen, (20, 20, 20), (x + 8, 228), 3)
        pygame.draw.rect(screen, body, (x - 52, 275, 22, 14), border_radius=4)          # kollar
        pygame.draw.rect(screen, body, (x + 30, 275, 22, 14), border_radius=4)
        ui.label(screen, ui.fit("md", name, 220), "md", (cx, 190))                       # isim ustte
        # HP bari altta
        bx, by, bw, bh = cx - 100, 415, 200, 22
        pygame.draw.rect(screen, DARKRED, (bx, by, bw, bh))
        pygame.draw.rect(screen, RED, (bx, by, bw * self.hp[i] // 100, bh))
        pygame.draw.rect(screen, WHITE, (bx, by, bw, bh), 2)
        ui.label(screen, f"HP {self.hp[i]}", "xs", (cx, by + bh + 14), pad=6)
        if self.flash[i] > 0:
            ui.label(screen, f"-{self.last_dmg[i]}", "md", (cx, 140), fg=YELLOW, pad=6)


# Yeni oyun eklemek: sinifi yaz, buraya komutunu ekle. Baska hicbir yere dokunma.
GAMES: dict[str, type[Game]] = {
    "!maç": MathRing,
    "!mac": MathRing,       # turkce karakter yazmayanlar icin
}


# =====================================================================
# 5) GAMEMANAGER  (davet + !kabul eslestirme)
# =====================================================================
@dataclass
class Invite:
    game_cls: type[Game]
    inviter: str             # ekranda gorunen ad
    inviter_key: str         # kucuk harf
    target_key: str | None   # "!maç @ali" ise sadece ali kabul edebilir
    ttl: float = INVITE_TTL


class GameManager:
    def __init__(self) -> None:
        self.invites: dict[str, Invite] = {}      # inviter_key -> Invite (kisi basina tek davet)
        self.active: Game | None = None
        self.notice, self.notice_ttl = "", 0.0

    def say(self, msg: str) -> None:
        self.notice, self.notice_ttl = msg, 6.0

    def handle_chat(self, user: str, text: str) -> None:
        if self.active:
            self.active.on_message(user, text)    # mac sirasinda sadece cevaplar islenir
            return
        cmd = parse_command(text)
        if cmd is None:
            return
        key = user.lower()

        if cmd.name in GAMES:                      # !maç, !bilmece ... -> davet
            target = cmd.arg if cmd.arg and cmd.arg != key else None
            self.invites[key] = Invite(GAMES[cmd.name], user, key, target)
            who = f" ({target} icin)" if target else ""
            self.say(f"{user} {GAMES[cmd.name].TITLE} daveti acti{who}")
            return

        if cmd.name == "!kabul":
            if not cmd.arg:
                self.say(f"{user}: '!kabul @kullaniciadi' yazmalisin")
                return
            inv = self.invites.get(cmd.arg)
            if inv is None:
                self.say(f"{user}: {cmd.arg} adli kisinin bekleyen daveti yok")
            elif inv.inviter_key == key:
                self.say(f"{user}: kendi davetini kabul edemezsin")
            elif inv.target_key and inv.target_key != key:
                self.say(f"{user}: bu davet {inv.target_key} icin, sen giremezsin")
            else:
                self.start(inv, user)

    def start(self, inv: Invite, acceptor: str) -> None:
        self.invites.pop(inv.inviter_key, None)
        self.invites.pop(acceptor.lower(), None)   # kabul eden kisinin kendi acik daveti de silinir
        self.active = inv.game_cls(inv.inviter, acceptor)
        self.notice = ""

    def update(self, dt: float) -> None:
        for k in [k for k, v in self.invites.items() if (setattr(v, "ttl", v.ttl - dt) or v.ttl <= 0)]:
            del self.invites[k]
        self.notice_ttl = max(0.0, self.notice_ttl - dt)
        if self.active:
            self.active.update(dt)
            if self.active.finished and self.active.finished_for > RESULT_SECS:
                self.active = None

    def draw(self, screen: pygame.Surface, ui: "UI", status: str) -> None:
        if self.active:
            self.active.draw(screen, ui)
            return
        ui.label(screen, "YAYINCI VS OYUNU", "lg", (W // 2, 60), pad=20)
        ui.label(screen, "Sohbete  !ma\u00e7  yaz, rakip bul   |   Kabul:  !kabul @davet_eden", "sm", (W // 2, 125))
        y = 175
        if not self.invites:
            ui.label(screen, "Bekleyen davet yok", "sm", (W // 2, y))
        for inv in list(self.invites.values())[:5]:
            tgt = f" -> {inv.target_key}" if inv.target_key else ""
            ui.label(screen, f"{inv.inviter}{tgt}  |  {inv.game_cls.TITLE}  |  {int(inv.ttl)} sn", "sm",
                     (W // 2, y))
            y += 42
        if self.notice_ttl > 0:
            ui.label(screen, ui.fit("sm", self.notice, W - 80), "sm", (W // 2, 420), fg=YELLOW)
        if status:
            ui.label(screen, status, "xs", (12, 12), anchor="topleft", pad=6)


# =====================================================================
# 6) ARAYUZ + ANA DONGU
# =====================================================================
def app_dir() -> str:
    return os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))


CONFIG_PATH = os.path.join(app_dir(), "kickvs_config.json")


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(d: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except OSError as e:
        print(f"[!] Ayar kaydedilemedi: {e}", flush=True)


def printable(text: str) -> str:
    """Fontun cizemeyecegi karakterleri (emoji vb.) ekran icin eler."""
    return "".join(ch for ch in text if ord(ch) < 0x250 or 0x2010 <= ord(ch) <= 0x206F)


class UI:
    def __init__(self) -> None:
        names = "arial,segoeui,dejavusans,liberationsans"
        sizes = {"xs": 16, "sm": 21, "md": 28, "lg": 46, "xl": 66}
        self.fonts = {k: pygame.font.SysFont(names, s, bold=True) for k, s in sizes.items()}
        self.cache: dict[tuple, pygame.Surface] = {}

    def render(self, key: str, text: str, color: tuple) -> pygame.Surface:
        k = (key, text, color)
        s = self.cache.get(k)
        if s is None:
            if len(self.cache) > 400:
                self.cache.clear()
            s = self.fonts[key].render(text, True, color)
            self.cache[k] = s
        return s

    def fit(self, key: str, text: str, maxw: int) -> str:
        f = self.fonts[key]
        if f.size(text)[0] <= maxw:
            return text
        while text and f.size(text + "...")[0] > maxw:
            text = text[:-1]
        return text + "..."

    def label(self, screen, text: str, key: str, pos, anchor: str = "center",
              fg: tuple = WHITE, bg: tuple | None = PANEL, pad: int = 10) -> pygame.Rect:
        surf = self.render(key, text, fg)
        box = pygame.Rect(0, 0, surf.get_width() + pad * 2, surf.get_height() + pad)
        setattr(box, anchor, pos)
        if bg:
            pygame.draw.rect(screen, bg, box, border_radius=8)
        screen.blit(surf, surf.get_rect(center=box.center))
        return box


class Login:
    """Acilis ekrani: Kick kanal adi (+ istege bagli chatroom ID)."""

    def __init__(self, cfg: dict, error: str = "") -> None:
        self.vals = [str(cfg.get("slug", "")), str(cfg.get("chatroom_id", "") or "")]
        self.idx, self.error = 0, error

    def handle(self, ev: pygame.event.Event) -> tuple[str, int | None] | None:
        if ev.type != pygame.KEYDOWN:
            return None
        if ev.key == pygame.K_TAB:
            self.idx = 1 - self.idx
        elif ev.key == pygame.K_BACKSPACE:
            self.vals[self.idx] = self.vals[self.idx][:-1]
        elif ev.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            slug, room = self.vals[0].strip(), self.vals[1].strip()
            if not SLUG_RE.match(slug):
                self.error = "Gecersiz kanal adi (harf, rakam, _ ve - olabilir)"
            elif room and not room.isdigit():
                self.error = "Chatroom ID sadece rakam olmali"
            else:
                return slug, (int(room) if room else None)
        elif ev.unicode and len(self.vals[self.idx]) < 40:
            ok = r"[A-Za-z0-9_\-]" if self.idx == 0 else r"[0-9]"
            if re.fullmatch(ok, ev.unicode):
                self.vals[self.idx] += ev.unicode
        return None

    def draw(self, screen, ui: UI, t: float) -> None:
        ui.label(screen, "YAYINCI VS OYUNU", "lg", (W // 2, 90), pad=20)
        ui.label(screen, "Kick kanal adini gir (kick.com/KANAL)", "md", (W // 2, 190))
        titles = ("Kanal adi:", "Chatroom ID (bos birakilabilir):")
        for i in (0, 1):
            y = 260 + i * 90
            ui.label(screen, titles[i], "sm", (W // 2 - 260, y - 34), anchor="midleft", pad=6)
            cursor = "_" if (i == self.idx and int(t * 2) % 2 == 0) else " "
            border = YELLOW if i == self.idx else GRAY
            box = pygame.Rect(W // 2 - 260, y - 14, 520, 44)
            pygame.draw.rect(screen, PANEL, box, border_radius=8)
            pygame.draw.rect(screen, border, box, 2, border_radius=8)
            screen.blit(ui.render("md", self.vals[i] + cursor, WHITE), (box.x + 12, box.y + 6))
        ui.label(screen, "Enter: basla    Tab: alan degistir", "sm", (W // 2, 460))
        if self.error:
            ui.label(screen, ui.fit("sm", self.error, W - 60), "sm", (W // 2, 510), fg=(255, 120, 120))


def main() -> None:
    pygame.init()
    pygame.display.set_caption("Yayinci VS Oyunu")
    screen = pygame.display.set_mode((W, H))
    clock = pygame.time.Clock()
    ui = UI()
    cfg = load_config()

    login: Login | None = Login(cfg)
    q: "queue.Queue[tuple[str, str, str]]" = queue.Queue()
    manager = GameManager()
    chat_log: deque[tuple[str, str]] = deque(maxlen=LOG_LINES)
    status, t = "", 0.0
    running = True

    while running:
        dt = clock.tick(FPS) / 1000.0
        t += dt
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE and login is None:
                running = False
            elif login is not None:
                res = login.handle(ev)
                if res:
                    slug, room = res
                    save_config({"slug": slug, "chatroom_id": room})
                    q = queue.Queue()
                    ChatReader(slug, room, q).start()
                    manager, chat_log, status = GameManager(), deque(maxlen=LOG_LINES), "Baglaniliyor..."
                    login = None

        screen.fill(GREEN)

        if login is not None:
            login.draw(screen, ui, t)
        else:
            for _ in range(100):                       # kare basina en fazla 100 olay
                try:
                    kind, a, b = q.get_nowait()
                except queue.Empty:
                    break
                if kind == "chat":
                    chat_log.append((printable(a), printable(b)))
                    try:
                        manager.handle_chat(a, b)
                    except Exception:                  # tek bir mesaj oyunu cokertmesin
                        traceback.print_exc()
                elif kind == "status":
                    status = a
                elif kind == "fatal":
                    login = Login(cfg=load_config(), error=a)
            manager.update(dt)
            if login is None:
                manager.draw(screen, ui, status)
                # alttaki sohbet logu: duz panel, son LOG_LINES mesaj
                pygame.draw.rect(screen, PANEL, (0, H - 92, W, 92))
                for i, (u, m) in enumerate(chat_log):
                    line = ui.fit("xs", f"{u}: {m}", W - 24)
                    screen.blit(ui.render("xs", line, WHITE), (12, H - 86 + i * 21))
            else:
                screen.fill(GREEN)
                login.draw(screen, ui, t)

        pygame.display.flip()

    pygame.quit()


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        main()
    except Exception:
        traceback.print_exc()
        if getattr(sys, "frozen", False):
            input("\nHata olustu. Kapatmak icin Enter...")
