#!/usr/bin/env python3
"""
obx_bot.py — VibeCoder always-on Discord gateway + voice daemon (py-cord).

The bot is the box's always-on Discord presence in Bociarnia:

  • stays online via the Discord gateway (presence "watching obx channels")
  • text routing:
      - channels named  obx-<agent>   (or exactly `obx`) → fully autonomous:
        EVERY message is watched and answered by that <agent>.
      - generic channels              → only replies when the bot is
        mentioned, replied-to, or via the /obx slash command.
      - /obx [agent] [message]        → start a conversation with an agent
        in any channel of the server.
  • voice routing:
      - the bot auto-joins obx-<agent> voice channels when a human is in
        them, listens (faster-whisper persistent server), thinks through
        the <agent> session, and speaks back with the agent's kokoro voice.

Brains = per-agent sessions on the OpenCodeBox shared opencode service
(127.0.0.1:49374, Basic auth opencode:<token>). Reply endpoint used:
POST /api/session/{id}/generate {"prompt": ...} -> {"data":{"text":...}}.

Usage:  python3 obx_bot.py            → run (foreground, logs to stdout)
        python3 obx_bot.py doctor     → environment/config diagnostics
Env:    BOX (box root) overrides config.json;
        DISCORD_AGENTS overrides the agents file;
        WHISPER_MODEL overrides the whisper model.
"""

import asyncio
import base64
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import wave
from pathlib import Path

BASE = Path(__file__).resolve().parent
BOX = Path(os.environ.get("BOX", "/run/media/hubertg/SONIC/OpenCodeBox"))


def load_json(path: Path, label: str) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[obx] ERR cannot load {label} ({path}): {e}", flush=True)
        return {}


CONFIG = load_json(BASE / "config.json", "config")
AGENTS = load_json(Path(os.environ.get("DISCORD_AGENTS", BASE / "agents.json")), "agents")

GUILD_ID = int(CONFIG.get("guild_id", "1521889213987946707"))
SVC_URL = CONFIG.get("service", {}).get("url", "http://127.0.0.1:49374")
WHISPER_SOCK = CONFIG.get("whisper", {}).get("socket", "/tmp/opencode/whisper-server.sock")
WHISPER_SERVER = CONFIG.get("whisper", {}).get("server", str(BOX / "tools/whisper-server.sh"))
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", CONFIG.get("whisper", {}).get("model", "medium"))
WHISPER_LANG = os.environ.get("WHISPER_LANG", CONFIG.get("whisper", {}).get("lang", "en"))
KOKORO_SOCK = CONFIG.get("kokoro", {}).get("socket", "/tmp/opencode/kokoro-server.sock")
KOKORO_SERVER = CONFIG.get("kokoro", {}).get("server", str(BOX / "tools/kokoro-server.sh"))
KEYS_ENV = CONFIG.get("token_keys_env", str(BOX / "box/TokenKeysMCP.env"))
TMPDIR = Path(CONFIG.get("tmp", "/tmp/opencode"))
MIN_GAP = float(CONFIG.get("min_text_gap_s", 1.5))
VOICE_CFG = CONFIG.get("voice", {})
V_THR = float(VOICE_CFG.get("thr", 0.0035))
V_SILENCE = float(VOICE_CFG.get("silence_s", 2.0))  # 2s: fewer mid-sentence cuts
V_MAX = float(VOICE_CFG.get("max_s", 25))
MIRROR_VOICE = bool(CONFIG.get("mirror_voice_to_text", True))
ASK_REPEAT = bool(CONFIG.get("voice", {}).get("ask_repeat", True))  # ask to repeat when unheard
MIC_LISTEN = bool(CONFIG.get("mic_listen", False))  # local-mic bridge (default OFF — direct channel listening is the goal)

# --- box voice layers (best-effort, fall back to agents.json voices) ---
def _read_layered(path, default=""):
    try:
        return Path(path).read_text(encoding="utf-8").strip() or default
    except Exception:
        return default


BOX_VOICE_ACTIVE = _read_layered(str(BOX / "box/agents/voice/ACTIVE"), "")
BOX_VOICEMODE_ACTIVE = _read_layered(str(BOX / "box/agents/voicemode/ACTIVE"), "5-full")
BOX_VOICEENGINE_ACTIVE = _read_layered(str(BOX / "box/agents/voiceengine/ACTIVE"), "1-kokoro")
BOX_VOICES = load_json(BOX / "box/agents/voice/voices.json", "voices.json")


def resolve_agent_voice(name: str, cfg: dict) -> tuple[str, str]:
    """(voice_slug, lang) for an agent: explicit cfg -> box voice layer -> default."""
    voice = (cfg or {}).get("voice")
    lang = (cfg or {}).get("lang", "a")
    if not voice and BOX_VOICE_ACTIVE and BOX_VOICE_ACTIVE in BOX_VOICES:
        entry = BOX_VOICES.get(BOX_VOICE_ACTIVE, {})
        if entry.get("type") == "teto":
            voice, lang = "jf_alpha", "j"
        else:
            voice, lang = entry.get("voice", "af_nicole"), "a"
    return voice or "af_nicole", lang

STATE_FILE = BASE / "state.json"


# ──────────────────────────────────────────────────────────────────────
# Box plumbing: discord token + opencode shared service
# ──────────────────────────────────────────────────────────────────────
def read_token() -> str:
    """[Discord:Client] token from TokenKeysMCP.env (same resolution as tools/discord)."""
    text = ""
    for f in (KEYS_ENV, str(BOX / "box/TokenKeysMCP.env"), str(BOX / "TokenKeysMCP.env")):
        try:
            text = Path(f).read_text(encoding="utf-8")
            if text:
                break
        except Exception:
            continue
    m = re.search(r"^\[Discord:Client\][^\n]*\n(.*?)(?=^\[|\Z)", text, re.M | re.S)
    if m:
        mm = re.search(r"^token\s*=\s*['\"]?([^'\r\n\"]+)", m.group(1), re.M)
        if mm:
            return mm.group(1)
    return os.environ.get("BOX_DISCORD_CLIENT_TOKEN", "")


def svc_auth() -> tuple[str, str]:
    tok = ""
    try:
        m = re.search(r"^OPENCODE_SVC_PASS\s*=\s*(\S+)", Path(KEYS_ENV).read_text(encoding="utf-8"), re.M)
        if m:
            tok = m.group(1)
    except Exception:
        pass
    tok = tok or os.environ.get("OPENCODE_SVC_PASS", "")
    return "opencode", tok


def svc_request(method: str, path: str, body: dict | None = None, timeout: float = 180) -> dict:
    """Call the shared opencode service; returns parsed JSON ({} on failure)."""
    user, pw = svc_auth()
    data = json.dumps(body).encode("utf-8") if body is not None else None
    basic = base64.b64encode(f"{user}:{pw}".encode()).decode()
    req = urllib.request.Request(
        SVC_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Authorization": "Basic " + basic},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        print(f"[obx] svc HTTP {e.code} {method} {path}", flush=True)
        return {}
    except Exception as e:
        print(f"[obx] svc error {method} {path}: {e}", flush=True)
        return {}


# ──────────────────────────────────────────────────────────────────────
# Per-agent brains (sessions on the shared opencode service)
# ──────────────────────────────────────────────────────────────────────
class AgentRuntime:
    """One brain per obx-<agent>: session id + serialized generate calls."""

    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.cfg = cfg
        self.session_id: str | None = None

    def _seed_prompt(self, prompt: str) -> str:
        persona = (self.cfg.get("persona") or "").strip()
        if persona:
            return f"{persona}\n\nUser: {prompt}"
        return prompt

    async def ensure_session(self) -> str | None:
        if self.session_id:
            return self.session_id
        title = self.cfg.get("title") or f"obx-{self.name}"
        sess = svc_request("GET", "/api/session", timeout=30)
        for s in sess.get("data", []):
            if s.get("title") == title:
                self.session_id = s.get("id")
                break
        if not self.session_id:
            # real OpenCodeBox brain: create the session WITH the box agent
            # ("agent" param — agentID is ignored). Bare sessions answer
            # generically; agent sessions think with the box's setup/layers.
            agent = self.cfg.get("agent", "Skebob")
            created = svc_request(
                "POST", "/api/session", {"title": title, "agent": agent}, timeout=30
            )
            self.session_id = created.get("data", {}).get("id")
            if self.session_id:
                print(f"[obx] created session for '{self.name}' (agent={agent})", flush=True)
        if not self.session_id:
            print(f"[obx] WARN no session reachable for agent '{self.name}'", flush=True)
            return None
        state = {}
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
        state.setdefault("sessions", {})[self.name] = self.session_id
        try:
            STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except Exception:
            pass
        return self.session_id

    async def ask(self, prompt: str, timeout: float = 180) -> str:
        sid = await self.ensure_session()
        if not sid:
            return "*brain offline*"
        r = await asyncio.to_thread(
            svc_request, "POST", f"/api/session/{sid}/generate",
            {"prompt": self._seed_prompt(prompt)}, timeout,
        )
        text = (r.get("data") or {}).get("text", "").strip()
        return text or "*brain returned nothing*"


class Brain:
    """Holds all AgentRuntimes and the serialized text worker."""

    def __init__(self):
        self.agents: dict[str, AgentRuntime] = {}
        for name, cfg in AGENTS.items():
            if isinstance(cfg, dict) and cfg.get("enabled", True):
                self.agents[name.lower()] = AgentRuntime(name.lower(), cfg)
        if "default" not in self.agents:
            self.agents["default"] = AgentRuntime("default", {})
        self.pending: asyncio.Queue = asyncio.Queue()
        self.last_post: dict[int, float] = {}
        self.worker_started = False

    def agent(self, name: str) -> AgentRuntime | None:
        return self.agents.get((name or "").lower())

    def can_post(self, channel_id: int) -> bool:
        now = time.monotonic()
        if now - self.last_post.get(channel_id, 0) < MIN_GAP:
            return False
        self.last_post[channel_id] = now
        return True

    def submit_text(self, agent_name: str, kind: str, channel, prompt: str, ctx=None) -> None:
        rt = self.agent(agent_name) or self.agent("default")
        if rt is None:
            return
        self.pending.put_nowait((rt, kind, channel, prompt, ctx))

    async def text_worker(self):
        while True:
            rt, kind, channel, prompt, ctx = await self.pending.get()
            typing = None
            try:
                if kind == "slash" and ctx is not None:
                    try:
                        await ctx.defer(ephemeral=False)
                    except Exception:
                        pass
                if kind == "message" and channel is not None and hasattr(channel, "typing"):
                    try:
                        typing = channel.typing()
                        await typing.__aenter__()
                    except Exception:
                        typing = None
                answer = await rt.ask(prompt)
                await asyncio.sleep(0.4)
                if kind == "slash" and ctx is not None:
                    await ctx.respond(answer[:2000] or "*…*")
                elif channel is not None and self.can_post(channel.id):
                    await channel.send(answer[:2000] or "*…*")
            except Exception as e:
                print(f"[obx] text_worker error: {e}", flush=True)
                traceback.print_exc()
            finally:
                if typing is not None:
                    try:
                        await typing.__aexit__(None, None, None)
                    except Exception:
                        pass


# ──────────────────────────────────────────────────────────────────────
# Whisper (STT) — persistent server over unix socket
# ──────────────────────────────────────────────────────────────────────
def socket_in_use(path: str) -> bool:
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.3)
        s.connect(path)
        s.close()
        return True
    except Exception:
        return False


def ensure_whisper() -> bool:
    if Path(WHISPER_SOCK).exists() and socket_in_use(WHISPER_SOCK):
        return True
    print("[obx] starting whisper server…", flush=True)
    try:
        env = dict(os.environ)
        env["WHISPER_MODEL"] = WHISPER_MODEL
        if WHISPER_LANG:
            env["WHISPER_LANG"] = WHISPER_LANG
        subprocess.Popen(
            [WHISPER_SERVER], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        for _ in range(90):  # GPU model load can take a while
            if Path(WHISPER_SOCK).exists() and socket_in_use(WHISPER_SOCK):
                return True
            time.sleep(0.5)
    except Exception as e:
        print(f"[obx] whisper start failed: {e}", flush=True)
    return False


def whisper_transcribe(wav_path: str) -> str:
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(30)
        s.connect(WHISPER_SOCK)
        s.sendall((wav_path + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            c = s.recv(4096)
            if not c:
                break
            buf += c
        s.close()
        text = buf.split(b"\n", 1)[0].decode("utf-8", "replace").strip()
        if text.startswith("ERR"):
            print(f"[obx] whisper: {text}", flush=True)
            return ""
        return text
    except Exception as e:
        print(f"[obx] whisper error: {e}", flush=True)
        return ""


# ──────────────────────────────────────────────────────────────────────
# Kokoro (TTS) — persistent server over unix socket
# ──────────────────────────────────────────────────────────────────────
def ensure_kokoro() -> bool:
    if Path(KOKORO_SOCK).exists() and socket_in_use(KOKORO_SOCK):
        return True
    print("[obx] starting kokoro server…", flush=True)
    try:
        subprocess.Popen(
            [KOKORO_SERVER],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        for _ in range(60):
            if Path(KOKORO_SOCK).exists() and socket_in_use(KOKORO_SOCK):
                return True
            time.sleep(0.5)
    except Exception as e:
        print(f"[obx] kokoro start failed: {e}", flush=True)
    return False


def kokoro_synth(text: str, out_wav: str, voice: str, lang: str = "a") -> bool:
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(60)
        s.connect(KOKORO_SOCK)
        req = json.dumps({"text": text, "lang": lang, "voice": voice, "out": out_wav}).encode() + b"\n"
        s.sendall(req)
        buf = b""
        while b"\n" not in buf:
            c = s.recv(4096)
            if not c:
                break
            buf += c
        s.close()
        return buf.decode().strip().startswith("OK") and Path(out_wav).exists()
    except Exception as e:
        print(f"[obx] kokoro error: {e}", flush=True)
        return False


# ──────────────────────────────────────────────────────────────────────
# Voice — listen (whisper), think (agent), speak (kokoro)
# ──────────────────────────────────────────────────────────────────────
import numpy as np
import discord
from discord.sinks import Sink


class RoomSink(Sink):
    """VAD-gated utterance capture. Receives decoded PCM (48 kHz) per user;
    keeps a mono 16 kHz mix and flushes whole utterances for the whisper brain."""

    # py-cord 2.8 never defines this on the base Sink; the event router iterates
    # it during registration — an empty list = no sink-side event listeners.
    __sink_listeners__: list = []

    def __init__(self, room: "VoiceRoom"):
        super().__init__()
        self.room = room
        self.pcm16 = bytearray()
        self.speaking = False
        self.last_sound = time.monotonic()
        self.buf_start = time.monotonic()
        # receive diagnostics
        self.frame_count = 0
        self.seen_users = set()
        self.last_hb = time.monotonic()

    def is_opus(self) -> bool:
        return False  # we want decoded PCM

    def walk_children(self, *, with_self: bool = True) -> list:
        return []  # no child sinks

    def write(self, data, user):
        if user is None or getattr(user, "bot", False):
            return
        pcm = getattr(data, "pcm", b"") or b""
        if not pcm:
            return
        try:
            self.frame_count += 1
            self.seen_users.add(str(getattr(user, "id", user)))
            stereo = len(pcm) >= 3840 and len(pcm) % 4 == 0
            a = np.frombuffer(pcm, dtype=np.int16)
            if stereo:
                a = a.reshape(-1, 2).mean(axis=1).astype(np.int16)
            rms = float(np.sqrt(np.mean((a.astype(np.float32) / 32768.0) ** 2)))
            was = self.speaking
            if rms > V_THR:
                self.speaking = True
                self.last_sound = time.monotonic()
                if not was:
                    print(f"[obx] VAD voice detected (rms={rms:.4f})", flush=True)
            if self.speaking:
                self.pcm16.extend(a[::3].astype(np.int16).tobytes())  # 48k -> 16k
                if time.monotonic() - self.buf_start > V_MAX:
                    self._flush("max")
            now = time.monotonic()
            if now - self.last_hb >= 5.0:
                self.last_hb = now
                print(
                    f"[obx] recv hb: frames={self.frame_count} users={sorted(self.seen_users)[:4]} "
                    f"rms={rms:.4f} speaking={self.speaking} buf={len(self.pcm16)}",
                    flush=True,
                )
        except Exception:
            pass

    def _flush(self, why: str):
        if self.pcm16:
            data = bytes(self.pcm16)
            print(f"[obx] utterance flush ({why}): {len(data)} bytes {len(data)/32000:.2f}s", flush=True)
            self.pcm16 = bytearray()
            self.speaking = False
            self.buf_start = time.monotonic()
            self.room.utterance_ready(data, why)
        else:
            self.speaking = False
            self.buf_start = time.monotonic()

    def pump(self):
        if self.speaking and time.monotonic() - self.last_sound > V_SILENCE:
            self._flush("silence")

    def cleanup(self):
        try:
            self._flush("leave")
        except Exception:
            pass


class MicListener(threading.Thread):
    """Local-mic -> VAD -> whisper (the proven OBX-Talking path).

    Captures the machine's default mic (PipeWire), waits for speech + trailing
    silence, downsamples to 16k mono, transcribes via the persistent whisper
    server and hands the text to the voice room. Bypasses Discord's voice
    receiving entirely (DAVE/E2EE-proof).
    """

    def __init__(self, room: "VoiceRoom"):
        super().__init__(daemon=True, name="obx-mic-listener")
        self.room = room
        self.stop_evt = threading.Event()

    def run(self):
        try:
            src = subprocess.check_output(["pactl", "get-default-source"], text=True).strip()
        except Exception as e:
            print(f"[obx] mic: no default source ({e})", flush=True)
            return
        if not src:
            return
        try:
            p = subprocess.Popen(
                ["pw-cat", "-r", "--target", src, "--format=s16", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            print(f"[obx] mic: pw-cat failed ({e})", flush=True)
            return
        thr = 0.005
        sil = 1.5
        max_s = 20.0
        spoke = False
        last = 0.0
        t0 = time.monotonic()
        buf = []
        leftover = b""
        DS = 12  # 192k -> 16k
        try:
            while not self.stop_evt.is_set():
                chunk = p.stdout.read(4096)
                if not chunk:
                    break
                data = leftover + chunk
                n = (len(data) // 2) * 2
                arr = np.frombuffer(data[:n], dtype=np.int16).astype(np.float32) / 32768.0
                leftover = data[n:]
                if len(arr) == 0:
                    continue
                rms = float(np.sqrt(np.mean(arr * arr)))
                if rms > thr:
                    spoke = True
                    last = time.monotonic()
                    buf.append(arr)
                elif spoke:
                    if time.monotonic() - last > sil:
                        break
                    buf.append(arr)
                if spoke and time.monotonic() - t0 > max_s:
                    break
        finally:
            try:
                p.terminate()
            except Exception:
                pass
        if not spoke or not buf:
            return
        audio = np.concatenate(buf)
        try:
            idx = np.arange(0, len(audio), DS)
            pcm16 = audio[idx].astype(np.int16)
            # drop <1s of speech (noise)
            if len(pcm16) < 16000:
                return
            print(f"[obx] mic utterance captured ({len(pcm16)/16000:.1f}s)", flush=True)
            self.room.on_mic_utterance(pcm16.tobytes())
        except Exception as e:
            print(f"[obx] mic utterance error: {e}", flush=True)


class VoiceRoom:
    """One obx voice channel the bot is connected to (single active at a time)."""

    def __init__(self, bot, voice_client, agent_name, channel):
        self.bot = bot
        self.vc = voice_client
        self.agent_name = agent_name
        self.channel = channel
        self.sink = RoomSink(self)
        self.stopped = False
        self._pump_task: asyncio.Task | None = None

    def __init__(self, bot, voice_client, agent_name, channel):
        self.bot = bot
        self.vc = voice_client
        self.agent_name = agent_name
        self.channel = channel
        self.sink = RoomSink(self)
        self.stopped = False
        self._pump_task: asyncio.Task | None = None
        self._raw_count = 0
        self._raw_last = time.monotonic()
        self._raw_probe_registered = False
        self.rawdump = str(TMPDIR / "obx-rawdump.bin")
        self._last_repeat = 0.0

    def _raw_probe(self, data: bytes):
        """Called by py-cord's socket reader for EVERY raw UDP packet (pre-decrypt)."""
        self._raw_count += 1
        if self._raw_count == 20:
            self._raw_last = time.monotonic()
            print(f"[obx] RAW packets arriving ({self._raw_count} seen, last {len(data)}B)", flush=True)
        # debug: keep a small packet dump for offline decrypt verification
        if self._raw_count <= 300:
            try:
                with open(self.rawdump, "ab") as f:
                    f.write(len(data).to_bytes(2, "big") + data)
            except Exception:
                pass

    def start(self):
        self.loop = self.bot.loop
        # py-cord 2.8.1 never calls sink.init(vc) -> decoder asserts on sink.client
        self.sink.vc = self.vc
        self.sink._client = self.vc
        try:
            self.vc.start_listening(self.sink)
            print(f"[obx] listening in voice #{self.channel.name}", flush=True)
        except Exception as e:
            print(f"[obx] voice listen failed: {e} (DAVE/E2EE may block receiving)", flush=True)
        try:
            conn = getattr(self.vc, "_connection", None)
            if conn is not None and not self._raw_probe_registered:
                conn.add_socket_listener(self._raw_probe)
                self._raw_probe_registered = True
            sk = getattr(conn, "secret_key", None) or getattr(self.vc, "secret_key", None)
            sk_hex = sk.hex() if isinstance(sk, (bytes, bytearray)) else str(sk)
            print(
                f"[obx] voice mode: {getattr(self.vc, 'mode', '?')} | "
                f"dave(privacy_code): {getattr(self.vc, 'voice_privacy_code', None)} | "
                f"key: {sk_hex[:32]}…",
                flush=True,
            )
            try:
                Path(self.rawdump).write_bytes(
                    f"mode={getattr(self.vc, 'mode', '?')}\nkey={sk_hex}\n".encode()
                )
            except Exception:
                pass
        except Exception as e:
            print(f"[obx] probe setup failed: {e}", flush=True)
        self._pump_task = asyncio.get_event_loop().create_task(self._pump_loop())
        # local-mic bridge (OBX-Talking style) — hears the box's mic regardless of DAVE
        if MIC_LISTEN and not self.stopped:
            self.mic = MicListener(self)
            self.mic.start()
            print("[obx] local-mic listener started", flush=True)
        # proactive greeting: proves the speaker path + tells the human we're here
        greet = CONFIG.get("greet_on_join", True)
        if greet and not self.stopped:
            async def _greet():
                await asyncio.sleep(1.5)
                await self.speak(
                    f"I'm here in {self.channel.name}. Say something and I will answer."
                )
            asyncio.ensure_future(_greet())

    async def _pump_loop(self):
        while not self.stopped:
            try:
                self.sink.pump()
            except Exception:
                pass
            await asyncio.sleep(0.25)

    def utterance_ready(self, pcm16k: bytes, why: str):
        # called from py-cord's sink router THREAD — hand off to the bot loop
        try:
            loop = getattr(self, "loop", None) or self.bot.loop
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._handle_utterance(pcm16k)))
        except Exception:
            pass

    async def _handle_utterance(self, pcm16k: bytes):
        if self.stopped or len(pcm16k) < 9600:  # <0.3s of 16k mono — ignore
            return
        # trim leading/trailing silence edges — whisper hallucinates on them
        a = np.frombuffer(pcm16k, dtype=np.int16).astype(np.float32) / 32768.0
        mag = np.abs(a)
        ons = np.where(mag > 0.01)[0]
        if len(ons) > 0:
            a0, a1 = max(0, int(ons[0] - 0.06 * 16000)), min(len(a), int(ons[-1] + 0.12 * 16000))
            pcm16k = (a[a0:a1] * 32767).astype(np.int16).tobytes()
        if len(pcm16k) < 9600:
            return
        wav = TMPDIR / f"obx-voice-{int(time.time() * 1000)}.wav"
        text = ""
        try:
            with wave.open(str(wav), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(pcm16k)
            text = await asyncio.to_thread(whisper_transcribe, str(wav))
            if not text or len(text) < 2:
                print(f"[obx] whisper: empty on {len(pcm16k)/16000:.2f}s audio", flush=True)
                await self._repeat_request(f"Sorry, I couldn't make that out — could you say it again?")
                return
            print(f"[obx] voice[{self.agent_name}] heard: {text}", flush=True)
            if MIRROR_VOICE:
                txt_ch = self._text_channel()
                if txt_ch:
                    try:
                        await txt_ch.send(f"🎙 *{text}*")
                    except Exception:
                        pass
            if self.vc.is_playing():
                self.vc.stop()  # interrupt: latest-wins
            rt = self.bot.brain.agent(self.agent_name) or self.bot.brain.agent("default")
            t0 = time.monotonic()
            answer = await rt.ask(
                f"A person is talking to you in the Discord voice channel {self.channel.name}. "
                f'They said: "{text}"\n'
                "Reply BRIEFLY and conversationally (max ~25 words) — as a natural spoken answer. "
                "Do NOT mention the transcription, do not speculate about anything you did not hear, "
                "and answer only what was asked."
            )
            print(f"[obx] brain {time.monotonic()-t0:.1f}s for {self.agent_name}", flush=True)
            if (not answer) or answer.startswith("*brain"):
                await self._repeat_request("Sorry, I had trouble forming an answer — could you repeat that?")
                return
            print(f"[obx] voice[{self.agent_name}] says: {answer[:120]}", flush=True)
            await self.speak(answer)
        except Exception as e:
            print(f"[obx] voice utterance error: {e}", flush=True)
            traceback.print_exc()
        finally:
            try:
                wav.unlink(missing_ok=True)
            except Exception:
                pass

    def on_mic_utterance(self, pcm16k: bytes):
        try:
            loop = getattr(self, "loop", None) or self.bot.loop
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(self._handle_mic_utterance(pcm16k))
            )
        except Exception:
            pass

    async def _handle_mic_utterance(self, pcm16k: bytes):
        if self.stopped or len(pcm16k) < 9600:
            return
        wav = TMPDIR / f"obx-mic-{int(time.time() * 1000)}.wav"
        try:
            with wave.open(str(wav), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(pcm16k)
            text = await asyncio.to_thread(whisper_transcribe, str(wav))
            if not text or len(text) < 2:
                return
            print(f"[obx] mic[{self.agent_name}] heard: {text}", flush=True)
            if MIRROR_VOICE:
                txt_ch = self._text_channel()
                if txt_ch:
                    try:
                        await txt_ch.send(f"🎙 *{text}*")
                    except Exception:
                        pass
            if self.vc.is_playing():
                self.vc.stop()  # interrupt: latest-wins
            rt = self.bot.brain.agent(self.agent_name) or self.bot.brain.agent("default")
            answer = await rt.ask(f"(Discord voice #{self.channel.name}) listener said: {text}")
            if not answer or answer.startswith("*brain"):
                return
            print(f"[obx] mic[{self.agent_name}] says: {answer[:120]}", flush=True)
            await self.speak(answer)
        except Exception as e:
            print(f"[obx] mic utterance error: {e}", flush=True)
            traceback.print_exc()
        finally:
            try:
                wav.unlink(missing_ok=True)
            except Exception:
                pass

    def _repeat_request(self, line: str):
        now = time.monotonic()
        if now - self._last_repeat < 8.0:
            return asyncio.sleep(0)  # rate-limited prompt
        self._last_repeat = now
        return asyncio.ensure_future(self.speak(line))

    def _text_channel(self):
        try:
            for ch in self.channel.guild.channels:
                if isinstance(ch, discord.TextChannel) and ch.name == self.channel.name:
                    return ch
        except Exception:
            pass
        return None

    async def speak(self, text: str):
        rt = self.bot.brain.agents.get(self.agent_name)
        cfg = rt.cfg if rt is not None else {}
        voice, lang = resolve_agent_voice(self.agent_name, cfg)
        out = TMPDIR / f"obx-tts-{int(time.time() * 1000)}.wav"
        ok = await asyncio.to_thread(kokoro_synth, text, str(out), voice, lang)
        if not ok:
            print(f"[obx] TTS failed for {self.agent_name}", flush=True)
            return
        try:
            if not self.vc.is_connected():
                print("[obx] drop speak: not connected", flush=True)
                return
            if self.vc.is_playing():
                self.vc.stop()
            print(f"[obx] voice speaking ({voice}): {text[:120]}", flush=True)
            # ffmpeg reads the file ASYNC — delete only after playback ends
            def _cleanup(_e=None):
                try:
                    out.unlink(missing_ok=True)
                except Exception:
                    pass
            self.vc.play(
                discord.FFmpegPCMAudio(str(out), executable="/usr/bin/ffmpeg"),
                after=_cleanup,
            )
        except Exception as e:
            print(f"[obx] voice play error: {e}", flush=True)
            traceback.print_exc()

    async def stop(self):
        self.stopped = True
        if getattr(self, "mic", None) is not None:
            self.mic.stop_evt.set()
            try:
                self.mic.join(timeout=2)
            except Exception:
                pass
        if self._pump_task:
            self._pump_task.cancel()
        try:
            if self.vc.is_recording():
                self.vc.stop_listening()
        except Exception:
            pass
        try:
            if self.vc.is_playing():
                self.vc.stop()
        except Exception:
            pass
        try:
            await self.vc.disconnect()
        except Exception:
            pass

    def is_listening(self) -> bool:
        try:
            return self.vc.is_recording()
        except Exception:
            return False


# ──────────────────────────────────────────────────────────────────────
# The bot
# ──────────────────────────────────────────────────────────────────────
class ObxBot(discord.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        super().__init__(intents=intents)
        self.brain = Brain()
        self.voice_room: VoiceRoom | None = None
        self._leave_task: asyncio.Task | None = None
        self._joining = False
        # register /obx explicitly (bare decorators do not add commands in py-cord 2.8)
        for name, desc, handler in (
            ("obx", "Talk with an agent (default unless named)", self.obx_cmd),
            ("privacy", "OpenCodeBox privacy policy", self.privacy_cmd),
            ("terms", "OpenCodeBox terms of service", self.terms_cmd),
            ("speak", "Make the bot say something in the voice channel it is in", self.speak_cmd),
        ):
            self.add_application_command(
                discord.SlashCommand(handler, name=name, description=desc, guild_ids=[GUILD_ID])
            )

    # ── lifecycle ──
    async def on_ready(self):
        print(f"[obx] online: {self.user} (id {self.user.id})", flush=True)
        try:
            await self.change_presence(
                activity=discord.Activity(type=discord.ActivityType.watching, name="obx channels")
            )
        except Exception as e:
            print(f"[obx] presence: {e}", flush=True)
        if not self.brain.worker_started:
            self.brain.worker_started = True
            asyncio.get_event_loop().create_task(self.brain.text_worker())
        try:
            await self.sync_commands(guild_ids=[GUILD_ID])
            print("[obx] /obx slash command synced", flush=True)
        except Exception as e:
            print(f"[obx] sync_commands: {e}", flush=True)
        g = self.get_guild(GUILD_ID)
        if g:
            for ch in g.voice_channels:
                if ch.name.startswith("obx-") and any(not m.bot for m in ch.members):
                    await self._join_voice(ch)
                    break

    # ── text ──
    async def on_message(self, message: discord.Message):
        try:
            if message.author.bot or message.author.id == self.user.id:
                return
            if not message.guild or message.guild.id != GUILD_ID:
                return
            if not message.content:
                return
            ch = message.channel
            name = getattr(ch, "name", "") or ""
            if name.startswith("obx-"):
                agent = name[4:].lower()
            elif name == "obx":
                agent = "default"
            else:
                mentioned = self.user.mentioned_in(message)
                replied_to_bot = bool(
                    message.reference
                    and message.reference.resolved
                    and getattr(message.reference.resolved, "author", None)
                    and message.reference.resolved.author.id == self.user.id
                )
                if not (mentioned or replied_to_bot):
                    return
                agent = "default"
            prompt = f"(Discord #{name}) {message.author.display_name}: {message.content}"
            self.brain.submit_text(agent, "message", ch, prompt)
        except Exception as e:
            print(f"[obx] on_message error: {e}", flush=True)

    # ── slash: /obx [agent] [message] (registered in __init__) ──
    async def obx_cmd(
        self,
        ctx: discord.ApplicationContext,
        agent: str = "",
        message: str = "",
    ):
        name = (agent or "default").strip().lower()
        if self.brain.agent(name) is None:
            known = ", ".join(sorted(self.brain.agents))
            await ctx.respond(f"no such agent `{name}` — known: {known}")
            return
        prompt = message.strip() or "(Discord) start a conversation; greet the user here."
        who = f"(Discord #{getattr(ctx.channel, 'name', '?')}) user: {prompt}"
        self.brain.submit_text(name, "slash", ctx.channel, who, ctx=ctx)

    # ── slash: /privacy and /terms ──
    async def speak_cmd(self, ctx, text: str = ""):
        try:
            await ctx.defer()
        except Exception:
            pass
        try:
            room = self.voice_room
            if room is None or not room.vc.is_connected():
                await ctx.respond("I'm not in a voice channel. Join an `obx-*` voice channel first.")
                return
            line = text.strip() or "Hello! This is a voice test."
            await room.speak(line)
            await ctx.respond(f"🔊 said: {line[:120]}")
            return line
        except Exception as e:
            print(f"[obx] /speak error: {e}", flush=True)
            traceback.print_exc()
            try:
                await ctx.respond(f"⚠ speak failed: {e}")
            except Exception:
                pass

    async def privacy_cmd(self, ctx: discord.ApplicationContext):
        await ctx.respond("Privacy Policy: <https://gyerchak.github.io/OpenCode-DiscordBot/privacy-policy.html>")

    async def terms_cmd(self, ctx: discord.ApplicationContext):
        await ctx.respond("Terms of Service: <https://gyerchak.github.io/OpenCode-DiscordBot/terms-of-service.html>")

    # ── voice: auto join obx-* channels with humans, leave when empty ──
    async def on_voice_state_update(self, member, before, after):
        try:
            if member.id == self.user.id:
                if after.channel is None and self.voice_room is not None:
                    room = self.voice_room
                    self.voice_room = None
                    await room.stop()
                return
            if member.bot or member.guild.id != GUILD_ID:
                return
            g = member.guild
            target = None
            for ch in g.voice_channels:
                if ch.name.startswith("obx-") and any(not m.bot for m in ch.members):
                    target = ch
                    break
            if target is None:
                if self.voice_room is not None and self._leave_task is None:
                    self._leave_task = asyncio.get_event_loop().create_task(self._maybe_leave())
                return
            if self.voice_room is not None:
                vc = self.voice_room.vc
                if vc.is_connected() and getattr(vc, "channel", None) == target:
                    return
                # staying put while the current room still has humans
                cur = getattr(vc, "channel", None)
                if cur is not None and any(not m.bot for m in cur.members):
                    return
                await self.voice_room.stop()
                self.voice_room = None
            await self._join_voice(target)
        except Exception as e:
            print(f"[obx] voice_state error: {e}", flush=True)

    async def _maybe_leave(self):
        try:
            await asyncio.sleep(20)
            g = self.get_guild(GUILD_ID)
            busy = False
            if g:
                busy = any(
                    ch.name.startswith("obx-") and any(not m.bot for m in ch.members)
                    for ch in g.voice_channels
                )
            if not busy and self.voice_room is not None:
                room = self.voice_room
                self.voice_room = None
                await room.stop()
                print("[obx] left empty voice channel", flush=True)
        except Exception as e:
            print(f"[obx] maybe_leave error: {e}", flush=True)
        finally:
            self._leave_task = None

    async def _join_voice(self, channel):
        if getattr(self, "_joining", False):
            return
        if self.voice_room is not None and self.voice_room.vc.is_connected():
            return
        existing = channel.guild.voice_client
        if existing is not None and existing.is_connected():
            # already connected — just ensure a room wraps it (start listening)
            if self.voice_room is None:
                agent = channel.name[4:].lower() if channel.name.startswith("obx-") else "default"
                if self.brain.agent(agent) is None:
                    agent = "default"
                print(f"[obx] reusing voice connection as agent '{agent}'", flush=True)
                self.voice_room = VoiceRoom(self, existing, agent, channel)
                self.voice_room.start()
            return
        self._joining = True
        try:
            agent = channel.name[4:].lower() if channel.name.startswith("obx-") else "default"
            if self.brain.agent(agent) is None:
                agent = "default"
            print(f"[obx] joining voice #{channel.name} as agent '{agent}'", flush=True)
            vc = await channel.connect()
            room = VoiceRoom(self, vc, agent, channel)
            self.voice_room = room
            room.start()
        except Exception as e:
            print(f"[obx] voice connect failed: {e}", flush=True)
        finally:
            self._joining = False


# ──────────────────────────────────────────────────────────────────────
# Doctor / main
# ──────────────────────────────────────────────────────────────────────
def app_intents() -> str:
    """Check Discord-side privilege flags for the bot application (LIMITED = no passive reading)."""
    tok = read_token()
    try:
        import discord.http
        # direct GET /applications/@me
        req = urllib.request.Request(
            "https://discord.com/api/v10/applications/@me",
            headers={
                "User-Agent": "OpenCodeBox-DiscordBot/1.0 (github.com/Gyerchak)",
                "Authorization": "Bot " + tok,
            },
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        flags = d.get("flags", 0)
        limited = bool(flags & (1 << 15))
        full = bool(flags & (1 << 14))
        if full:
            return "full message content (verified)"
        if limited:
            return (
                "LIMITED — message content only when mentioned/replied/DM'd. "
                "Fix: Developer Portal → Bot → Privileged Gateway Intents → toggle MESSAGE CONTENT "
                "(apps under 10k users can self-serve; then restart the daemon)."
            )
        return "no message-content flags set"
    except Exception as e:
        return f"unchecked ({e})"


def doctor() -> int:
    tok = read_token()
    sess = svc_request("GET", "/api/session", timeout=10)
    print(f"box            : {BOX}")
    print(f"guild_id       : {GUILD_ID}")
    print(f"service        : {SVC_URL}")
    print(f"discord token  : {'ok (%d chars)' % len(tok) if len(tok) > 30 else 'MISSING/INVALID'}")
    print(f"message intent : {app_intents()}")
    print(f"service up     : {bool(sess)} ({len(sess.get('data', []))} sessions)")
    for name, cfg in sorted(AGENTS.items()):
        print(
            f"  agent {name:<10} title={cfg.get('title')} voice={cfg.get('voice')} "
            f"lang={cfg.get('lang')} enabled={cfg.get('enabled', True)}"
        )
    ws = Path(WHISPER_SOCK).exists() and socket_in_use(WHISPER_SOCK)
    ks = Path(KOKORO_SOCK).exists() and socket_in_use(KOKORO_SOCK)
    print(f"whisper server : {'up' if ws else 'down (daemon starts it)'}")
    print(f"kokoro server  : {'up' if ks else 'down (daemon starts it)'}")
    return 0 if tok and sess else 1


def patch_pycord_receive():
    """Fix py-cord 2.8.1: PacketDecryptor.decrypt_rtp drops non-DAVE payloads.

    The upstream method computes `raw_payload = self._decryptor_rtp(packet)` but
    only assigns it to packet.decrypted_data inside the DAVE branch — so in a
    classic (non-DAVE) voice channel every packet returns None and the reader
    drops them. We assign the classic payload ourselves.
    """
    try:
        from discord.voice.receive.reader import PacketDecryptor
        from discord.voice.gateway import VoiceWebSocket
        from discord.voice.enums import OpCodes
        import davey
    except Exception as e:
        print(f"[obx] WARN cannot patch receive path: {e}", flush=True)
        return

    def decrypt_rtp_fixed(self, packet):
        state = self.client._connection
        dave = getattr(state, "dave_session", None)
        raw_pkt = bytes(packet.header) + bytes(packet.data)

        # Spec-correct AEAD XChaCha20-Poly1305 (rtpsize) — py-cord's version is
        # broken (wrong nonce/extension handling -> zero decrypted packets).
        # Verified offline against real captures: 110/153 packets -> valid Opus.
        if self.mode == "aead_xchacha20_poly1305_rtpsize":
            try:
                first = raw_pkt[0]
                hs = 12 + (4 if (first & 0x10) else 0)          # 12 + ext preamble
                nonce = raw_pkt[-4:] + b"\x00" * 20              # counter appended to payload
                plain = self.box.decrypt(raw_pkt[hs:-4], raw_pkt[:hs], nonce)
                # no extension strip: offline test on real captures showed raw
                # plaintext decodes as Opus more often (135 vs 111 with strip)
                packet.decrypted_data = plain
            except Exception:
                packet.decrypted_data = None
        else:
            try:
                packet.decrypted_data = self._decryptor_rtp(packet)
            except Exception:
                packet.decrypted_data = None

        # DAVE branch (upstream behaviour, rarely active here)
        if packet.decrypted_data and dave is not None and getattr(dave, "ready", False):
            uid = state.ssrc_user_map.get(packet.ssrc)
            if uid:
                try:
                    dec = dave.decrypt(uid, davey.MediaType.audio, packet.decrypted_data)
                    if packet.extended:
                        off = packet.update_extended_header(dec)
                        packet.decrypted_data = dec[off:]
                    else:
                        packet.decrypted_data = dec
                except Exception:
                    pass
        return packet.decrypted_data

    PacketDecryptor.decrypt_rtp = decrypt_rtp_fixed

    # py-cord 2.8.1 second bug: no inbound handler for OpCodes.speaking (opcode 5),
    # so ssrc_user_map stays empty and the reader drops EVERY packet (RTPPacket
    # .is_silence() is a stub returning True). Map user_id<->ssrc ourselves.
    _orig_received_message = VoiceWebSocket.received_message

    async def received_message_fixed(self, msg, /):
        r = await _orig_received_message(self, msg)
        try:
            if msg.get("op") == int(OpCodes.speaking):
                d = msg.get("d") or {}
                uid, ssrc = d.get("user_id"), d.get("ssrc")
                if uid is not None and ssrc is not None:
                    conn = getattr(self, "state", None)
                    vc = getattr(conn, "client", None)
                    if vc is not None:
                        vc._add_ssrc(int(uid), int(ssrc))
        except Exception:
            pass
        return r

    VoiceWebSocket.received_message = received_message_fixed

    # Router resilience: py-cord's router dies on the FIRST packet that fails
    # opus decode (mixed/odd frames). Skip bad packets instead of dying.
    from discord.voice.receive.router import PacketRouter as _PR

    _skip_last = {"t": 0.0}

    def _do_run_fixed(self):
        while not self._end_thread.is_set():
            self.waiter.wait()
            with self._lock:
                for decoder in list(self.waiter.items):
                    try:
                        data = decoder.pop_data()
                    except Exception as e:
                        # reset the decoder only after 2 CONSECUTIVE bad frames —
                        # a single odd frame shouldn't disrupt a clean stream
                        consec = _skip_last.setdefault("consec", {})
                        k = id(decoder)
                        consec[k] = consec.get(k, 0) + 1
                        if consec[k] >= 2:
                            try:
                                decoder.reset()
                            except Exception:
                                pass
                            consec.pop(k, None)
                            now = time.monotonic()
                            if now - _skip_last["t"] > 2.0:
                                _skip_last["t"] = now
                                print(f"[obx] skip bad frames ({type(e).__name__}) -> decoder reset", flush=True)
                        elif len(consec) > 64:
                            consec.pop(k, None)
                        continue
                    if data is not None:
                        try:
                            self.sink.write(data, data.source)
                        except Exception as e:
                            print(f"[obx] sink write error: {e}", flush=True)

    _PR._do_run = _do_run_fixed

    # Reader callback: remove the unknown-SSRC gate that dropped EVERY packet
    # whose SSRC wasn't yet mapped (RTPPacket.is_silence() is a stub = True).
    from discord.voice.receive.reader import AudioReader as _AR
    from discord.voice.packets.rtp import decode as _decode
    from discord.voice.receive.reader import is_rtcp as _is_rtcp

    def _callback_fixed(self, packet_data):
        try:
            if not _is_rtcp(packet_data):
                pkt = _decode(packet_data)
                pkt.decrypted_data = self.decryptor.decrypt_rtp(pkt)
                if not pkt.decrypted_data:
                    return
                self.speaking_timer.notify(pkt.ssrc)
                try:
                    self.packet_router.feed_rtp(pkt)
                except Exception:
                    pass
            else:
                try:
                    self.packet_router.feed_rtcp(packet_data)
                except Exception:
                    pass
        except Exception:
            pass

    _AR.callback = _callback_fixed
    print("[obx] py-cord receive path patched (decrypt + no packet gate + ssrc map)", flush=True)


def load_opus_safely() -> bool:
    if discord.opus.is_loaded():
        return True
    for cand in ("/usr/lib/libopus.so.0", "/usr/lib64/libopus.so.0", "/usr/lib/x86_64-linux-gnu/libopus.so.0"):
        try:
            discord.opus.load_opus(cand)
        except Exception:
            continue
        if discord.opus.is_loaded():
            print(f"[obx] libopus loaded ({cand})", flush=True)
            return True
    print("[obx] WARN libopus not found — voice playback will fail", flush=True)
    return False


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "doctor":
        return doctor()
    tok = read_token()
    if not tok:
        print("[obx] fatal: no Discord bot token ([Discord:Client] in TokenKeysMCP.env)", flush=True)
        return 1
    ensure_whisper()
    ensure_kokoro()
    load_opus_safely()
    patch_pycord_receive()
    bot = ObxBot()
    try:
        bot.run(tok)
    except KeyboardInterrupt:
        print("[obx] stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
