# talkAI

ブラウザで音声雑談ができるローカル AI です。マイク音声を音声認識し、LLM の返答を音声合成して読み上げます。
音声認識・LLM・音声合成はすべてローカルで動きます。

## 前提条件

talkAI 本体は Docker コンテナで動きますが、**LLM と音声処理はホスト側で別途起動しておく必要があります。**

| 必要なもの | 用途 | 接続先（コンテナから） |
| --- | --- | --- |
| Docker（Docker Compose 付き。Windows では Docker Desktop） | talkAI 本体の実行 | — |
| [Ollama](https://ollama.com/) | 返答の生成（LLM） | `http://host.docker.internal:11434` |
| speech.cpp の `speech serve`（WSL 上で起動） | 音声認識（ASR）・発話区間検出（VAD）・音声合成（TTS） | `host.docker.internal:8080` |
| マイク付きのPCとブラウザ（Chrome / Edge など） | 会話の操作 | — |

### Ollama

ホスト（Windows）に Ollama をインストールし、使うモデルを取得しておきます。

```sh
ollama pull qwen3:latest
ollama pull gemma4:e2b   # 画面で切り替えるモデル。不要なら LLM_MODELS から外す
```

- Ollama はポート `11434` で待ち受けている必要があります。
- 思考モード（`think`）を持つモデルは思考なしで呼び出します。思考モードがないモデルもそのまま使えます。

### speech serve（音声認識・音声合成）

WSL 上で、次のモデルを読み込んで `speech serve` を起動しておきます。

```sh
speech serve haas-ja-v1.0 qwen3-tts-0.6b silero-vad
```

| モデル | 用途 |
| --- | --- |
| `haas-ja-v1.0` | 日本語の音声認識 |
| `qwen3-tts-0.6b` | 音声合成（声は `ono_anna` など、サーバが提供するものから選択） |
| `silero-vad` | 発話の始まりと終わりの検出 |

- ポート `8080` で待ち受け、Docker コンテナから `host.docker.internal:8080` で接続できる状態にしてください。
- `speech serve` は `Host: localhost:<port>` のリクエストしか `/v1/realtime` で受け付けないため、talkAI は接続時に Host ヘッダを `localhost:8080` にして送ります（設定は不要です）。

## 起動

Ollama と `speech serve` を起動した状態で、次のコマンドを実行します。

```sh
docker compose up -d --build
```

ブラウザで **http://localhost:8765** を開きます。

> マイクはブラウザの制限により `localhost` か HTTPS でしか使えません。ポートは `127.0.0.1` にだけ公開しているため、同じPCのブラウザから開いてください。

停止するときは次のコマンドを実行します。

```sh
docker compose down
```

## 使い方

1. マイクボタンを押して話しかけます。話し終えて少し黙ると、AI が返答を読み上げます。
2. 入力欄に文字を入れて送ることもできます。
3. 画面上部で、使う LLM モデルと声を切り替えられます。

| 操作 | 動作 |
| --- | --- |
| マイクボタン | マイクの開始・停止 |
| 止める | AI の返答を途中で止める |
| リセット | 会話の履歴を消す |
| 話しかけて割り込む | オンにすると、AI が話している途中でも話しかけると返答を止めて聞き取りに移る |

- 「話しかけて割り込む」はヘッドホンを使うときだけオンにしてください。スピーカーだと AI 自身の声をマイクが拾って止まることがあります。オフの間は、AI が返答している間マイクを無音にします。
- AI の返答が途中で止まった場合、会話の履歴には実際に読み上げられた部分だけが残ります。

## 設定

`compose.yml` と同じフォルダに `.env` を置くと、次の値を変更できます。

| 変数 | 既定値 | 内容 |
| --- | --- | --- |
| `APP_PORT` | `8765` | ブラウザで開くポート |
| `LLM_MODEL` | `qwen3:latest` | 最初に使う LLM モデル |
| `LLM_MODELS` | `qwen3:latest,gemma4:e2b` | 画面で選べるモデル（カンマ区切り） |
| `OLLAMA_KEEP_ALIVE` | `60m` | Ollama がモデルをメモリに保持する時間 |
| `SPEECH_PORT` | `8080` | `speech serve` のポート |
| `TTS_VOICE` | `ono_anna` | 最初に使う声 |
| `VAD_SILENCE_MS` | `600` | 無音がこのミリ秒続いたら発話の終わりとみなす |
| `MAX_HISTORY` | `40` | 保持する発言数（ユーザーと AI の合計。40 で 20 往復分） |

`.env` の例:

```sh
APP_PORT=8765
LLM_MODEL=qwen3:latest
LLM_MODELS=qwen3:latest,gemma4:e2b
VAD_SILENCE_MS=800
```

AI の話し方を変えたいときは、`compose.yml` の `SYSTEM_PROMPT` を編集して `docker compose up -d` で反映します。

## 仕組み

```
ブラウザ ──(WebSocket: マイク音声 24kHz PCM16)──▶ talkAI (FastAPI)
                                                   │
                                                   ├─▶ speech serve /v1/realtime   音声認識・VAD
                                                   ├─▶ Ollama /api/chat            返答をストリーミング生成
                                                   └─▶ speech serve /v1/audio/speech  文ごとに音声合成
ブラウザ ◀──(WebSocket: 文字起こし・返答テキスト・WAV 音声)──┘
```

- 返答は文ごとに区切って音声合成するため、LLM が全文を生成し終える前に読み上げが始まります。
- 起動時とモデル切り替え時に、Ollama へモデルを事前に読み込ませます。

## ファイル構成

| パス | 内容 |
| --- | --- |
| `compose.yml` | コンテナ設定と環境変数 |
| `app/main.py` | 中継サーバ（ブラウザ・speech serve・Ollama をつなぐ） |
| `app/static/index.html` | 画面 |
| `app/static/mic-worklet.js` | マイク音声を 24kHz PCM16 に変換して送る AudioWorklet |
| `app/Dockerfile`, `app/requirements.txt` | コンテナイメージの定義 |

## うまく動かないとき

- **「読み込みに失敗」と表示される**: Ollama が起動しているか、`ollama list` にそのモデルがあるかを確認してください。
- **「セッション終了」と表示される／音声認識されない**: `speech serve` が起動しているか、Windows 側からポート `8080` に接続できるかを確認してください。
- **マイクを使えない**: `http://localhost:8765` で開いているか、ブラウザのマイク許可を確認してください。
- ログは `docker compose logs -f talkai` で確認できます。
