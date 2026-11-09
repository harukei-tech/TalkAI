"""音声雑談AI: ブラウザ <-> (speech.cpp ASR/VAD -> ollama -> speech.cpp TTS) の中継サーバ"""

import asyncio
import base64
import json
import logging
import os
import re
import struct
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
import websockets
from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

env = os.environ.get
OLLAMA_URL = env("OLLAMA_URL", "http://host.docker.internal:11434")
LLM_MODEL = env("LLM_MODEL", "qwen3:latest")
LLM_MODELS = [m.strip() for m in env("LLM_MODELS", LLM_MODEL).split(",") if m.strip()]
OLLAMA_KEEP_ALIVE = env("OLLAMA_KEEP_ALIVE", "60m")
SPEECH_HOST = env("SPEECH_HOST", "host.docker.internal")
SPEECH_PORT = int(env("SPEECH_PORT", "8080"))
ASR_MODEL = env("ASR_MODEL", "Haas-ja-v1.0")
TTS_MODEL = env("TTS_MODEL", "Qwen3-TTS-12Hz-0.6B-CustomVoice")
TTS_VOICE = env("TTS_VOICE", "ono_anna")
VAD_SILENCE_MS = int(env("VAD_SILENCE_MS", "600"))
MAX_HISTORY = int(env("MAX_HISTORY", "20"))
SYSTEM_PROMPT = env("SYSTEM_PROMPT", "あなたは気さくな雑談相手です。話し言葉で短く返答してください。").strip()

# speech serve は Host が localhost:PORT のリクエストしか /v1/realtime で受け付けないため、
# 接続先は SPEECH_HOST にしつつ Host ヘッダは localhost を名乗る
SPEECH_HOST_HEADER = f"localhost:{SPEECH_PORT}"

log = logging.getLogger("talkai")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

speech = httpx.AsyncClient(
    base_url=f"http://{SPEECH_HOST}:{SPEECH_PORT}",
    headers={"Host": SPEECH_HOST_HEADER},
    timeout=httpx.Timeout(120, connect=5),
)
ollama = httpx.AsyncClient(base_url=OLLAMA_URL, timeout=httpx.Timeout(600, connect=5))


async def warmup(model: str) -> None:
    """messages を空で送ると ollama はモデルをメモリに載せるだけで返る"""
    r = await ollama.post("/api/chat", json={"model": model, "messages": [], "keep_alive": OLLAMA_KEEP_ALIVE})
    r.raise_for_status()


@asynccontextmanager
async def lifespan(_: FastAPI):
    async def initial_warmup():
        try:
            await warmup(LLM_MODEL)
        except Exception as e:
            log.warning("warmup failed: %r", e)

    task = asyncio.create_task(initial_warmup())
    yield
    task.cancel()
    await speech.aclose()
    await ollama.aclose()


app = FastAPI(lifespan=lifespan)
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/config")
async def config():
    voices = [TTS_VOICE]
    with suppress(Exception):
        r = await speech.get("/v1/models")
        for m in r.json()["data"]:
            if m["id"] == TTS_MODEL:
                voices = [v["name"] for v in m["speech"]["voices"]]
    return {"models": LLM_MODELS, "model": LLM_MODEL, "voices": voices, "voice": TTS_VOICE}


# ---- 文分割・読み上げ用の整形 ----

SENTENCE_END = re.compile(r"[。！？!?\n]+")
THINK = re.compile(r"<think>.*?</think>", re.S)
UNSPEAKABLE = re.compile(r"[*#`>_~|\[\]\U0001F000-\U0001FAFF☀-➿️]")


def pop_sentences(buf: str, final: bool = False) -> tuple[list[str], str]:
    out = []
    while True:
        if m := SENTENCE_END.search(buf):
            out.append(buf[: m.end()])
            buf = buf[m.end():]
            continue
        # 句点が来ないまま長くなったら読点で区切って先に合成する
        if len(buf) > 60 and (i := max(buf.rfind("、"), buf.rfind(","))) > 10:
            out.append(buf[: i + 1])
            buf = buf[i + 1:]
            continue
        break
    if final and buf:
        out.append(buf)
        buf = ""
    return [s for s in map(speakable, out) if s], buf


def speakable(text: str) -> str:
    return UNSPEAKABLE.sub("", THINK.sub("", text)).strip()


# ---- 1 接続 = 1 会話 ----

class Session:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.send_lock = asyncio.Lock()
        self.model = LLM_MODEL
        self.voice = TTS_VOICE
        self.history: list[dict] = []
        self.turn = 0
        self.response: asyncio.Task | None = None
        self.upstream = None
        self.loading: asyncio.Task | None = None

    async def send_json(self, data: dict) -> None:
        async with self.send_lock:
            await self.ws.send_text(json.dumps(data, ensure_ascii=False))

    async def send_bytes(self, data: bytes) -> None:
        async with self.send_lock:
            await self.ws.send_bytes(data)

    async def run(self) -> None:
        self.upstream = await websockets.connect(
            f"ws://{SPEECH_HOST_HEADER}/v1/realtime?model={ASR_MODEL}",
            host=SPEECH_HOST, port=SPEECH_PORT, max_size=None,
        )
        await self.upstream.send(json.dumps({"type": "session.update", "session": {
            "type": "transcription",
            "audio": {"input": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "transcription": {"model": ASR_MODEL, "language": "ja"},
                "turn_detection": {"type": "server_vad", "silence_duration_ms": VAD_SILENCE_MS},
            }},
        }}))
        self.loading = asyncio.create_task(self.load_model())

        tasks = [asyncio.create_task(self.from_client()), asyncio.create_task(self.from_speech())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                t.result()
        finally:
            for t in tasks:
                t.cancel()
            self.cancel_response()
            await self.upstream.close()

    async def from_client(self) -> None:
        while True:
            msg = await self.ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if msg.get("bytes"):
                # 24kHz mono PCM16 のマイク音声
                await self.upstream.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(msg["bytes"]).decode(),
                }))
            elif msg.get("text"):
                self.on_client_message(json.loads(msg["text"]))

    def on_client_message(self, data: dict) -> None:
        match data.get("type"):
            case "config":
                self.voice = data.get("voice") or self.voice
                if (model := data.get("model")) and model != self.model:
                    self.model = model
                    self.loading = asyncio.create_task(self.load_model())
            case "text":
                if text := data.get("text", "").strip():
                    self.start_response(text)
            case "interrupt":
                self.cancel_response()
            case "reset":
                self.cancel_response()
                self.history.clear()

    async def from_speech(self) -> None:
        async for raw in self.upstream:
            ev = json.loads(raw)
            t = ev["type"]
            if t == "input_audio_buffer.speech_started":
                await self.send_json({"type": "speech_started", "item": ev["item_id"]})
            elif t == "conversation.item.input_audio_transcription.delta":
                await self.send_json({"type": "user_delta", "item": ev["item_id"], "text": ev["delta"]})
            elif t == "conversation.item.input_audio_transcription.completed":
                text = ev["transcript"].strip()
                await self.send_json({"type": "user_final", "item": ev["item_id"], "text": text})
                if text:
                    self.start_response(text, echoed=True)
            elif t == "error":
                log.warning("speech.cpp: %s", ev)
                await self.send_json({"type": "error", "text": ev.get("error", {}).get("message", str(ev))})

    async def load_model(self) -> None:
        model = self.model
        await self.send_json({"type": "status", "text": f"{model} を読み込み中…"})
        try:
            await warmup(model)
            await self.send_json({"type": "status", "text": "準備完了"})
        except Exception as e:
            await self.send_json({"type": "error", "text": f"{model} の読み込みに失敗: {e}"})

    def cancel_response(self) -> None:
        if self.response and not self.response.done():
            self.response.cancel()

    def start_response(self, text: str, echoed: bool = False) -> None:
        # 新しい発話が来たら前の返答は打ち切る
        self.cancel_response()
        self.turn += 1
        self.response = asyncio.create_task(self.respond(self.turn, text, echoed))

    async def respond(self, turn: int, text: str, echoed: bool) -> None:
        if self.history and self.history[-1]["role"] == "user":
            # 前の発話への返答がひと言も届かずに打ち切られた（VAD が発話を区切った等）ので、続きとして連結する
            self.history[-1]["content"] += "\n" + text
        else:
            self.history.append({"role": "user", "content": text})
        log.info("user[%d]: %s", turn, text)
        await self.send_json({"type": "turn_start", "turn": turn, "user": None if echoed else text})
        sentences: asyncio.Queue[str | None] = asyncio.Queue()
        spoken: list[str] = []
        tts = asyncio.create_task(self.speak(turn, sentences, spoken))
        reply = ""
        completed = False
        try:
            buf = ""
            async for delta in self.chat_stream():
                reply += delta
                buf += delta
                await self.send_json({"type": "assistant_delta", "turn": turn, "text": delta})
                done, buf = pop_sentences(buf)
                for s in done:
                    sentences.put_nowait(s)
            done, _ = pop_sentences(buf, final=True)
            for s in done:
                sentences.put_nowait(s)
            sentences.put_nowait(None)
            await tts
            completed = True
            await self.send_json({"type": "turn_end", "turn": turn})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("response failed")
            await self.send_json({"type": "error", "turn": turn, "text": str(e)})
        finally:
            tts.cancel()
            # 打ち切られた返答は、ユーザーに音声で届いた分だけを履歴に残す。
            # 生成済みでも聞かれていない文を「言ったこと」にすると会話の流れが食い違う
            said = THINK.sub("", reply).strip() if completed else "".join(spoken)
            if said:
                self.history.append({"role": "assistant", "content": said})
            log.info("assistant[%d]%s: %s", turn, "" if completed else "(中断)", said)
            del self.history[:-MAX_HISTORY]

    async def chat_stream(self):
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *self.history],
            "stream": True,
            "think": False,
            "keep_alive": OLLAMA_KEEP_ALIVE,
            "options": {"temperature": 0.8, "num_predict": 256},
        }
        for attempt in range(2):
            async with ollama.stream("POST", "/api/chat", json=body) as r:
                if r.status_code == 400 and attempt == 0:
                    # 思考モードを持たないモデルは think を付けると 400 になる
                    body.pop("think")
                    continue
                if r.status_code != 200:
                    raise RuntimeError(f"ollama {r.status_code}: {(await r.aread()).decode(errors='replace')}")
                async for line in r.aiter_lines():
                    if not line:
                        continue
                    chunk = json.loads(line)
                    if err := chunk.get("error"):
                        raise RuntimeError(err)
                    if delta := chunk.get("message", {}).get("content"):
                        yield delta
                return

    async def speak(self, turn: int, sentences: asyncio.Queue, spoken: list[str]) -> None:
        """文ごとに合成して送る。ブラウザが 1 文目を再生している間に 2 文目を合成する"""
        while (text := await sentences.get()) is not None:
            r = await speech.post("/v1/audio/speech", json={
                "model": TTS_MODEL, "input": text, "voice": self.voice,
                "language": "ja", "response_format": "wav",
            })
            if r.status_code != 200:
                raise RuntimeError(f"TTS {r.status_code}: {r.text}")
            # 先頭 4 バイトにターン番号を付け、打ち切られた返答の音声をブラウザ側で捨てられるようにする
            await self.send_bytes(struct.pack("<I", turn) + r.content)
            spoken.append(text)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    try:
        await Session(ws).run()
    except Exception as e:
        log.exception("session ended")
        with suppress(Exception):
            await ws.send_text(json.dumps({"type": "error", "text": f"セッション終了: {e}"}, ensure_ascii=False))
    with suppress(Exception):
        await ws.close()
