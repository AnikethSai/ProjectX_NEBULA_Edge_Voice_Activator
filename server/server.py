"""
Nebula Host — UDP audio receiver + faster-whisper transcription + WebSocket server.

Receives 16kHz/16-bit mono PCM audio from the ESP32-S3 over UDP, transcribes it
incrementally using faster-whisper, and pushes live caption events to a browser
frontend over WebSocket.

Usage:
    pip install faster-whisper websockets aiohttp
    python server.py

Configuration:
    HOST_PORT       — UDP port to listen on (must match ESP32 firmware)
    WS_PORT         — WebSocket port for the frontend
    HTTP_PORT       — HTTP port for serving the frontend
    WHISPER_MODEL   — faster-whisper model size ("tiny", "base", "small", etc.)
"""

import asyncio
import json
import struct
import time
import threading
import os
import logging
import socket
from pathlib import Path

import numpy as np
from aiohttp import web
import websockets
from faster_whisper import WhisperModel

# ===================================================================
# Configuration
# ===================================================================
HOST_PORT     = 12345          # UDP port — must match ESP32's HOST_PORT
ESP_CMD_PORT  = 12346          # UDP command port on ESP32
WS_PORT       = 8765           # WebSocket port for frontend
HTTP_PORT     = 8080           # HTTP port to serve the frontend
WHISPER_MODEL = "base"         # "tiny", "base", "small", etc.
COMPUTE_TYPE  = "int8"         # "int8" for fast CPU inference
SAMPLE_RATE   = 16000          # Must match ESP32 firmware
FRAME_SAMPLES = 320            # 20ms at 16kHz
SEQ_HEADER_SIZE = 4            # 4-byte uint32 LE sequence number

last_esp_ip = None             # Learned automatically from incoming UDP packets

# Transcription timing
TRANSCRIBE_INTERVAL_S = 0.7    # Run transcription every ~700ms
TRANSCRIBE_WINDOW_S   = 3.0    # Process last N seconds of audio each run
LANGUAGE              = "en"   # Force English for speed

# ===================================================================
# Logging
# ===================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("nebula")

# ===================================================================
# Globals
# ===================================================================
ws_clients: set = set()
audio_buffer: bytearray = bytearray()
audio_lock = threading.Lock()
is_receiving = False
last_packet_time = 0.0
expected_seq = 0
session_active = False
dropped_packets = 0

esp_online = False
last_esp_heartbeat_time = 0.0
current_stream_duration = 8

# Model loaded once at startup
whisper_model: WhisperModel = None


# ===================================================================
# WebSocket broadcast
# ===================================================================
async def ws_broadcast(message: dict):
    """Send a JSON message to all connected WebSocket clients."""
    global ws_clients
    if not ws_clients:
        return
    text = json.dumps(message)
    disconnected = set()
    for ws in list(ws_clients):
        try:
            await ws.send(text)
        except Exception:
            disconnected.add(ws)
    ws_clients.difference_update(disconnected)


eos_event: asyncio.Event = None

def trigger_eos():
    """Signal end-of-stream from UDP or WebSocket test mic."""
    global eos_event
    if eos_event:
        eos_event.set()


def send_esp_command(cmd: str):
    """Send command to ESP32 via UDP broadcast and direct IP if known."""
    global last_esp_ip
    targets = []
    if last_esp_ip:
        targets.append(last_esp_ip)
    # Try direct IP, subnet broadcast, and standard broadcast
    targets.extend(["192.168.1.255", "<broadcast>", "255.255.255.255"])

    sent_any = False
    for target in dict.fromkeys(targets):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(cmd.encode('utf-8'), (target, ESP_CMD_PORT))
            sock.close()
            sent_any = True
            log.info(f"Sent UDP command '{cmd}' to {target}:{ESP_CMD_PORT}")
        except Exception:
            pass
    if not sent_any:
        log.warning(f"Could not send UDP command '{cmd}' to any target")


async def ws_handler(websocket):
    """Handle a new WebSocket connection from the frontend."""
    global ws_clients, is_receiving, session_active, current_stream_duration
    ws_clients.add(websocket)
    log.info(f"WebSocket client connected ({len(ws_clients)} total)")

    # Send current status and device status immediately
    state = "receiving" if is_receiving else "idle"
    try:
        await websocket.send(json.dumps({
            "type": "status",
            "state": state,
            "esp_online": esp_online,
            "duration": current_stream_duration
        }))
    except Exception:
        pass

    try:
        async for message in websocket:
            if isinstance(message, bytes):
                # Binary 16kHz 16-bit mono PCM chunk from browser test mic
                with audio_lock:
                    audio_buffer.extend(message)
                is_receiving = True
            elif isinstance(message, str):
                try:
                    data = json.loads(message)
                    mtype = data.get("type")
                    if mtype == "test_start":
                        with audio_lock:
                            audio_buffer.clear()
                        session_active = True
                        is_receiving = True
                        log.info("Frontend direct laptop mic test started")
                        asyncio.create_task(ws_broadcast({"type": "status", "state": "receiving", "source": "laptop"}))
                    elif mtype == "test_stop":
                        log.info("Frontend direct laptop mic test stopped")
                        trigger_eos()
                    elif mtype == "trigger_esp_start":
                        log.info("Frontend requested ESP32 mic stream START")
                        send_esp_command("START")
                    elif mtype == "trigger_esp_stop":
                        log.info("Frontend requested ESP32 mic stream STOP")
                        send_esp_command("STOP")
                    elif mtype == "set_duration":
                        current_stream_duration = int(data.get("duration", 8))
                        log.info(f"Setting stream duration to {current_stream_duration}s")
                        send_esp_command(f"DURATION:{current_stream_duration}")
                except Exception as e:
                    log.error(f"Error handling WebSocket message: {e}")
    except Exception:
        pass
    finally:
        ws_clients.discard(websocket)
        log.info(f"WebSocket client disconnected ({len(ws_clients)} total)")


async def esp_liveness_checker():
    """Periodically check if ESP32 heartbeat has timed out."""
    global esp_online, last_esp_heartbeat_time
    while True:
        await asyncio.sleep(1.0)
        now = time.monotonic()
        if esp_online and (now - last_esp_heartbeat_time > 5.0):
            esp_online = False
            log.info("ESP32 heartbeat timed out -> device offline")
            await ws_broadcast({"type": "esp_status", "online": False})


# ===================================================================
# UDP Protocol
# ===================================================================
class UDPAudioProtocol(asyncio.DatagramProtocol):
    """Receives framed PCM audio packets from the ESP32-S3."""

    def __init__(self, event_loop, on_eos_callback):
        self.loop = event_loop
        self.on_eos = on_eos_callback

    def datagram_received(self, data, addr):
        global expected_seq, dropped_packets, last_packet_time
        global is_receiving, session_active, last_esp_ip
        global esp_online, last_esp_heartbeat_time

        if len(data) < SEQ_HEADER_SIZE:
            return  # Malformed

        # Parse sequence number (uint32 LE)
        seq = struct.unpack('<I', data[:SEQ_HEADER_SIZE])[0]
        payload = data[SEQ_HEADER_SIZE:]

        # Heartbeat packet from ESP32 (magic sequence 0xFFFFFFFE)
        if seq == 0xFFFFFFFE:
            last_esp_ip = addr[0]
            last_esp_heartbeat_time = time.monotonic()
            if not esp_online:
                esp_online = True
                log.info(f"ESP32 is online ({addr[0]})")
                self.loop.create_task(ws_broadcast({"type": "esp_status", "online": True}))
                send_esp_command(f"DURATION:{current_stream_duration}")
            return

        last_esp_ip = addr[0]
        last_esp_heartbeat_time = time.monotonic()
        if not esp_online:
            esp_online = True
            self.loop.create_task(ws_broadcast({"type": "esp_status", "online": True}))

        # End-of-stream marker: seq header with zero-length payload
        if len(payload) == 0:
            log.info(f"End-of-stream received (seq {seq}) from {addr}")
            self.on_eos()
            return

        # Start of new session detection
        if not session_active:
            session_active = True
            expected_seq = seq
            log.info(f"New audio session from {addr} (starting seq {seq})")
            self.loop.create_task(
                ws_broadcast({"type": "status", "state": "receiving", "source": "esp32"})
            )

        # Sequence check — drop stale/out-of-order packets
        if seq < expected_seq:
            dropped_packets += 1
            return
        if seq > expected_seq:
            gap = seq - expected_seq
            dropped_packets += gap
            log.warning(f"Dropped {gap} packet(s) (expected {expected_seq}, got {seq})")

        expected_seq = seq + 1
        last_packet_time = time.monotonic()

        # Store PCM data
        with audio_lock:
            audio_buffer.extend(payload)

        is_receiving = True


# ===================================================================
# Transcription engine
# ===================================================================
def transcribe_audio(audio_bytes: bytes, is_final: bool = False) -> str:
    """Run faster-whisper on raw PCM bytes. Returns transcribed text."""
    if len(audio_bytes) < 3200:  # Need at least 100ms of audio (1600 samples)
        return ""

    # Convert bytes to float32 numpy array
    samples = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0

    try:
        segments, info = whisper_model.transcribe(
            samples,
            language=LANGUAGE,
            beam_size=1 if not is_final else 3,
            vad_filter=False,        # We handle VAD on the ESP32 side
            without_timestamps=True,
        )
        text_parts = []
        for segment in segments:
            text_parts.append(segment.text.strip())
        return " ".join(text_parts).strip()
    except Exception as e:
        log.error(f"Transcription error: {e}")
        return ""


# ===================================================================
# Main streaming/transcription loop
# ===================================================================
async def audio_session_manager():
    """
    Background task: periodically transcribes buffered audio and broadcasts
    partial results. On end-of-stream, runs a final pass and resets.
    """
    global is_receiving, session_active, audio_buffer, dropped_packets, expected_seq, eos_event

    eos_event = asyncio.Event()

    def on_eos():
        trigger_eos()

    # Start UDP server
    loop = asyncio.get_event_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: UDPAudioProtocol(loop, on_eos),
        local_addr=('0.0.0.0', HOST_PORT)
    )
    log.info(f"UDP server listening on port {HOST_PORT}")

    try:
        while True:
            # Wait for a session to begin
            eos_event.clear()
            while not session_active:
                await asyncio.sleep(0.1)

            log.info("Audio session active — starting transcription loop")
            last_transcribe = time.monotonic()

            while session_active:
                # Check for end-of-stream
                if eos_event.is_set():
                    break

                # Periodic partial transcription
                now = time.monotonic()
                if now - last_transcribe >= TRANSCRIBE_INTERVAL_S:
                    last_transcribe = now

                    with audio_lock:
                        # Get the last N seconds for rolling transcription
                        window_bytes = int(TRANSCRIBE_WINDOW_S * SAMPLE_RATE * 2)
                        if len(audio_buffer) > window_bytes:
                            chunk = bytes(audio_buffer[-window_bytes:])
                        else:
                            chunk = bytes(audio_buffer)

                    if len(chunk) > 0:
                        # Run transcription in a thread to not block the event loop
                        text = await loop.run_in_executor(
                            None, transcribe_audio, chunk, False
                        )
                        if text:
                            log.info(f"Partial: {text}")
                            await ws_broadcast({
                                "type": "partial",
                                "text": text
                            })

                await asyncio.sleep(0.05)  # Small sleep to avoid busy-waiting

            # End of session — run final transcription on full buffer
            log.info(f"Session ended. Total audio: {len(audio_buffer)} bytes, "
                     f"dropped packets: {dropped_packets}")

            with audio_lock:
                full_audio = bytes(audio_buffer)

            if len(full_audio) > 0:
                text = await loop.run_in_executor(
                    None, transcribe_audio, full_audio, True
                )
                if text:
                    log.info(f"Final: {text}")
                    await ws_broadcast({
                        "type": "final",
                        "text": text
                    })

            # Reset for next session
            with audio_lock:
                audio_buffer.clear()
            is_receiving = False
            session_active = False
            dropped_packets = 0
            expected_seq = 0
            eos_event.clear()

            await ws_broadcast({"type": "status", "state": "idle"})
            log.info("Ready for next session")

    finally:
        transport.close()


# ===================================================================
# HTTP server for frontend
# ===================================================================
async def serve_frontend(request):
    """Serve the static frontend HTML."""
    static_dir = Path(__file__).parent / "static"
    index_path = static_dir / "index.html"
    if index_path.exists():
        return web.FileResponse(index_path)
    return web.Response(text="Frontend not found", status=404)


async def start_http_server():
    """Start the aiohttp server for the frontend."""
    app = web.Application()
    static_dir = Path(__file__).parent / "static"
    app.router.add_get('/', serve_frontend)
    # Also serve any other static files
    if static_dir.exists():
        app.router.add_static('/static/', static_dir)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', HTTP_PORT)
    await site.start()
    log.info(f"HTTP server at http://localhost:{HTTP_PORT}")


# ===================================================================
# Main
# ===================================================================
async def main():
    global whisper_model

    log.info("=" * 60)
    log.info("  Nebula Host — Live Captioning Backend")
    log.info("=" * 60)

    # Load Whisper model
    log.info(f"Loading faster-whisper model '{WHISPER_MODEL}' ({COMPUTE_TYPE})...")
    whisper_model = WhisperModel(WHISPER_MODEL, compute_type=COMPUTE_TYPE)
    log.info("Model loaded successfully")

    # Start HTTP server for frontend
    await start_http_server()

    # Start WebSocket server
    ws_server = await websockets.serve(ws_handler, "0.0.0.0", WS_PORT)
    log.info(f"WebSocket server at ws://localhost:{WS_PORT}")

    # Start UDP audio session manager and ESP liveness checker
    audio_task = asyncio.create_task(audio_session_manager())
    liveness_task = asyncio.create_task(esp_liveness_checker())

    log.info("")
    log.info(f"Frontend:  http://localhost:{HTTP_PORT}")
    log.info(f"WebSocket: ws://localhost:{WS_PORT}")
    log.info(f"UDP port:  {HOST_PORT}")
    log.info("")
    log.info("Waiting for ESP32-S3 audio stream...")

    # Run forever
    await asyncio.gather(audio_task, liveness_task)


if __name__ == "__main__":
    asyncio.run(main())
