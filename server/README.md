# Nebula — Edge Voice Activator + Live Captions

A two-part system for the SIH26172 hackathon (ISRO edge voice activator):

1. **ESP32-S3 firmware** — Listens for the wake word "nebula" using an on-device TFLite model, then streams raw microphone audio over UDP to a host app.
2. **Host app** — Receives the audio, transcribes it in real time using faster-whisper, and displays live captions in a clean browser UI.

```
ESP32-S3 ──UDP 16kHz PCM──▶ Python backend ──WebSocket──▶ Browser (live captions)
```

---

## Quick Start

### 1. Configure the ESP32-S3 firmware

Open [`nebula_tester.ino`](file:///Users/rishi/Documents/Arduino/nebula_tester/nebula_tester.ino) and set these constants near the top of the file (look for the `NEW STREAMING CODE — Configuration constants` section):

```cpp
constexpr const char* WIFI_SSID     = "YOUR_WIFI_SSID";
constexpr const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";
constexpr const char* HOST_IP       = "192.168.1.100";  // IP of machine running server.py
constexpr uint16_t    HOST_PORT     = 12345;             // Must match server.py
```

To find `HOST_IP`, run on your host machine:
- **macOS**: `ipconfig getifaddr en0`
- **Linux**: `hostname -I`
- **Windows**: `ipconfig` → look for your Wi-Fi adapter's IPv4 address

Flash to the XIAO ESP32-S3 Sense via Arduino IDE.

### 2. Install Python dependencies

```bash
cd nebula_host
pip install -r requirements.txt
```

> **Note:** faster-whisper will download the Whisper model (~150 MB for "base") on first run. This requires an internet connection the first time only.

### 3. Configure & run the host app

Open [`server.py`](file:///Users/rishi/.gemini/antigravity/scratch/nebula_host/server.py) and verify these match your firmware settings:

```python
HOST_PORT     = 12345     # Must match ESP32's HOST_PORT
WS_PORT       = 8765      # WebSocket port (frontend connects here)
HTTP_PORT     = 8080      # Browser UI served here
WHISPER_MODEL = "base"    # "tiny" for faster/less accurate, "small" for slower/more accurate
```

Then run:

```bash
python server.py
```

### 4. Open the frontend

Open your browser to:

```
http://localhost:8080
```

You'll see the Nebula live captions UI. It connects to the WebSocket server automatically.

---

## How It Works

### Network flow

```
                         Wi-Fi (UDP)                WebSocket
   ┌──────────┐   16kHz/16-bit mono PCM    ┌──────────┐    ┌─────────┐
   │ ESP32-S3 │ ─── 20ms frames + seq ───▶ │  Python  │──▶ │ Browser │
   │  (mic)   │     header (640+4 bytes)    │ backend  │    │  (UI)   │
   └──────────┘                             └──────────┘    └─────────┘
```

### ESP32-S3 behavior
1. Continuously listens for "nebula" using the TFLite wake-word model
2. On detection: blinks LED twice, connects to Wi-Fi, opens UDP socket
3. Streams raw PCM audio in 20ms chunks (320 samples = 640 bytes) with a 4-byte sequence number header
4. LED pulses fast while streaming
5. Stops streaming after 8s max or 800ms of silence
6. Sends an end-of-stream marker (header only, zero payload)
7. Returns to wake-word listening

### Host app behavior
1. Receives UDP packets and reassembles audio in sequence order
2. Every ~700ms, runs faster-whisper on the last ~3s of buffered audio → pushes partial transcript to frontend
3. On end-of-stream, runs a final pass on the complete audio → pushes final transcript
4. Resets buffer, waits for next session

### Frontend
- Partial transcripts update the active line in real time
- Final transcripts move to dimmer history above
- Status indicator shows idle / transcribing / disconnected states
- Settings: font size slider, dark/light mode toggle (persisted in localStorage)

---

## Tunable Parameters

| Parameter | File | Default | Description |
|-----------|------|---------|-------------|
| `WIFI_SSID` / `WIFI_PASSWORD` | firmware | — | Your Wi-Fi credentials |
| `HOST_IP` / `HOST_PORT` | firmware | `192.168.1.100` / `12345` | Host app address |
| `STREAM_MAX_DURATION_MS` | firmware | `8000` | Max streaming time per utterance |
| `SILENCE_TRAILING_MS` | firmware | `800` | Silence duration to auto-stop |
| `SILENCE_THRESHOLD` | firmware | `300` | RMS threshold for silence detection |
| `HOST_PORT` | server.py | `12345` | UDP listen port (must match firmware) |
| `WS_PORT` | server.py | `8765` | WebSocket port for frontend |
| `HTTP_PORT` | server.py | `8080` | HTTP port for the browser UI |
| `WHISPER_MODEL` | server.py | `"base"` | faster-whisper model size |
| `TRANSCRIBE_INTERVAL_S` | server.py | `0.7` | How often to run partial transcription |
| `TRANSCRIBE_WINDOW_S` | server.py | `3.0` | Rolling window size for partial transcription |

---

## File Structure

```
nebula_tester/              ← ESP32-S3 firmware (Arduino)
├── nebula_tester.ino       ← Main firmware (original + streaming additions)
└── model_data.h            ← Quantized TFLite wake-word model

nebula_host/                ← Host app
├── server.py               ← Python backend (UDP + whisper + WebSocket)
├── requirements.txt        ← Python dependencies
├── static/
│   └── index.html          ← Live captions frontend
└── README.md               ← This file
```

---

## Troubleshooting

- **No audio received**: Check that `HOST_IP` in the firmware matches your host machine's IP, and that both use the same `HOST_PORT`. Ensure both devices are on the same Wi-Fi network.
- **Slow transcription**: Try `WHISPER_MODEL = "tiny"` for faster (but less accurate) results.
- **ESP32 won't connect to Wi-Fi**: Check credentials. The firmware waits 5 seconds; if your network is slow, increase the timeout in `ensureWiFi()`.
- **Frontend shows "Disconnected"**: Make sure `server.py` is running and port `8765` isn't blocked by a firewall.
