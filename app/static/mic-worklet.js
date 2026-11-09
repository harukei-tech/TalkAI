// マイク入力を 24kHz mono PCM16 に変換し、100ms ごとにメインスレッドへ送る
const TARGET_RATE = 24000;
const CHUNK = TARGET_RATE / 10;

class MicCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.step = sampleRate / TARGET_RATE;
    this.pos = 0;     // 現在のブロック先頭からの読み出し位置（-1 は前ブロック末尾）
    this.prev = 0;
    this.muted = false;
    this.out = new Int16Array(CHUNK);
    this.n = 0;
    this.port.onmessage = (e) => { this.muted = e.data.muted; };
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    const at = (k) => (k < 0 ? this.prev : ch[k]);
    while (this.pos < ch.length - 1) {
      const k = Math.floor(this.pos);
      const f = this.pos - k;
      // AI の発話中は無音を送り、VAD の時間軸を保ったまま自分の声を拾わないようにする
      let v = this.muted ? 0 : at(k) * (1 - f) + at(k + 1) * f;
      v = Math.max(-1, Math.min(1, v));
      this.out[this.n++] = v < 0 ? v * 0x8000 : v * 0x7fff;
      if (this.n === CHUNK) {
        this.port.postMessage(this.out.buffer, [this.out.buffer]);
        this.out = new Int16Array(CHUNK);
        this.n = 0;
      }
      this.pos += this.step;
    }
    this.pos -= ch.length;
    this.prev = ch[ch.length - 1];
    return true;
  }
}

registerProcessor("mic-capture", MicCapture);
