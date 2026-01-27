# Audio Processing Module

Real-time noise suppression and echo cancellation for self-hosted LiveKit.

## Features
- Noise Suppression (NS)
- Acoustic Echo Cancellation (AEC)
- Automatic Gain Control (AGC)

## Usage

```python
from audio_processing import ProcessedAudioInput, WebRTCAudioProcessor

# Create processor
processor = WebRTCAudioProcessor(
    noise_suppression=True,
    echo_cancellation=True,
    auto_gain_control=True,
)

# Wrap existing audio input
session.input.audio = ProcessedAudioInput(session.input.audio, processor)
```

## Echo Cancellation

Feed remote audio for AEC reference:
```python
processor.process_playout(remote_audio_frame)
```

## Install Dependency

```bash
pip install aec-audio-processing
```

## Fail-Safe Behavior
- If processor fails to initialize: raw audio passes through
- If per-frame processing fails: raw frame passes through
- No call interruption on errors
