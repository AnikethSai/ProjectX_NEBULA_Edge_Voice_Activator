# NEBULA — Edge Voice Activator

Low-latency, resource-efficient TinyML voice activation system
for edge devices.

## Overview

NEBULA is an edge-first voice activation system designed for
resource-constrained devices. It performs custom wake-word
detection locally and streams audio to a remote ASR service
only after the wake word is detected.

Wake Word: "NEBULA"

## Key Features

- Custom wake-word detection
- On-device TinyML inference
- INT8 quantized model
- VAD and audio preprocessing
- Pre-roll audio buffering
- Event-triggered audio streaming
- WebSocket communication
- Remote speech recognition using faster-whisper
- Robust training with real and synthetic speech

## System Architecture

Microphone
    ↓
Ring Buffer / Pre-roll
    ↓
VAD
    ↓
TFLM Microfrontend
    ↓
microWakeWord
    ↓
"NEBULA" detected?
    ↓ YES
Pre-roll + Live Audio
    ↓
WebSocket
    ↓
ASR Gateway
    ↓
faster-whisper

## Technology Stack

- TensorFlow Lite Micro
- microWakeWord
- ESP-NN
- Kokoro-82M
- faster-whisper
- WebSocket
- ESP32-S3-class edge hardware

## Training Pipeline

Real Voices + Synthetic Voices
        ↓
Data Augmentation
        ↓
Feature Extraction
        ↓
KWS Training
        ↓
INT8 Quantization
        ↓
.tflite Model
        ↓
Edge Deployment

## Resource Constraints

The prototype was tested against the target resource constraints:

- RAM: < 256 KB
- Idle CPU: < 10%
- INT8 inference
- Low-latency streaming operation

## Repository Structure

```text
/
├── training/
├── model/
├── deployment/
├── edge/
├── server/
├── data/
└── README.md
