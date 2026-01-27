"""
Real-time audio processing for noise suppression and echo cancellation.

This module provides pluggable audio processing without modifying core LiveKit classes.
Uses WebRTC Audio Processing Module (APM) via aec-audio-processing package.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Callable

from livekit import rtc
from livekit.agents.voice.io import AgentInput, AudioInput

logger = logging.getLogger("audio-processing")


class AudioProcessor(ABC):
    """Interface for audio processing (NS, AEC, AGC)."""

    @abstractmethod
    def process_capture(self, frame: rtc.AudioFrame) -> rtc.AudioFrame:
        """Process microphone audio (noise suppression + echo cancellation).
        
        Args:
            frame: Input audio frame from microphone
            
        Returns:
            Processed audio frame
        """
        ...

    @abstractmethod
    def process_playout(self, frame: rtc.AudioFrame) -> None:
        """Feed far-end (remote) audio for echo cancellation reference.
        
        Args:
            frame: Audio frame being played to speaker
        """
        ...

    def close(self) -> None:
        """Release resources. Override if cleanup is needed."""
        pass


class WebRTCAudioProcessor(AudioProcessor):
    """WebRTC APM-based audio processor with NS, AEC, and AGC.
    
    Uses aec-audio-processing package for WebRTC bindings.
    """

    def __init__(
        self,
        *,
        noise_suppression: bool = True,
        echo_cancellation: bool = True,
        auto_gain_control: bool = True,
        sample_rate: int = 16000,
        channels: int = 1,
    ):
        """Initialize WebRTC audio processor.
        
        Args:
            noise_suppression: Enable noise suppression
            echo_cancellation: Enable acoustic echo cancellation  
            auto_gain_control: Enable automatic gain control
            sample_rate: Expected sample rate (16000 or 48000)
            channels: Number of audio channels (typically 1)
        """
        self._ns_enabled = noise_suppression
        self._aec_enabled = echo_cancellation
        self._agc_enabled = auto_gain_control
        self._sample_rate = sample_rate
        self._channels = channels
        self._processor = None
        self._initialized = False

        self._init_processor()

    def _init_processor(self) -> None:
        """Initialize the underlying WebRTC APM."""
        try:
            from aec_audio_processing import AudioProcessor as AecProcessor

            self._processor = AecProcessor(
                enable_aec=self._aec_enabled,
                enable_ns=self._ns_enabled,
                enable_agc=self._agc_enabled,
            )
            self._processor.set_stream_format(self._sample_rate, self._channels)
            self._initialized = True
            logger.info(
                f"WebRTC APM initialized: NS={self._ns_enabled}, "
                f"AEC={self._aec_enabled}, AGC={self._agc_enabled}"
            )
        except ImportError:
            logger.warning(
                "aec-audio-processing not installed. "
                "Audio will pass through unprocessed. "
                "Install with: pip install aec-audio-processing"
            )
        except Exception as e:
            logger.warning(f"Failed to initialize WebRTC APM: {e}. Passing through raw audio.")

    def process_capture(self, frame: rtc.AudioFrame) -> rtc.AudioFrame:
        """Process microphone audio through WebRTC APM."""
        if not self._initialized or self._processor is None:
            return frame  # Pass through on failure

        try:
            # Reconfigure if sample rate changed
            if frame.sample_rate != self._sample_rate:
                self._sample_rate = frame.sample_rate
                self._processor.set_stream_format(self._sample_rate, self._channels)

            # Process the audio data
            processed_data = self._processor.process_stream(bytes(frame.data))

            # Return new frame with processed data
            return rtc.AudioFrame(
                data=processed_data,
                sample_rate=frame.sample_rate,
                num_channels=frame.num_channels,
                samples_per_channel=frame.samples_per_channel,
            )
        except Exception as e:
            logger.debug(f"Frame processing failed: {e}. Passing through raw frame.")
            return frame  # Fail open

    def process_playout(self, frame: rtc.AudioFrame) -> None:
        """Feed far-end audio to echo canceller."""
        if not self._initialized or self._processor is None:
            return

        try:
            # Feed reverse stream for AEC reference
            if hasattr(self._processor, "process_reverse_stream"):
                self._processor.process_reverse_stream(bytes(frame.data))
        except Exception as e:
            logger.debug(f"Playout processing failed: {e}")

    def close(self) -> None:
        """Release APM resources."""
        self._processor = None
        self._initialized = False


class ProcessedAudioInput(AudioInput):
    """AudioInput wrapper that applies audio processing to each frame.
    
    Wraps any existing AudioInput without modifying it.
    """

    def __init__(
        self,
        source: AudioInput,
        processor: AudioProcessor,
    ) -> None:
        """Initialize processed audio input.
        
        Args:
            source: Original audio input to wrap
            processor: Audio processor to apply to frames
        """
        super().__init__(label=f"processed:{source.label}", source=source)
        self._processor = processor

    @property
    def processor(self) -> AudioProcessor:
        """Get the audio processor."""
        return self._processor

    async def __anext__(self) -> rtc.AudioFrame:
        """Get next frame with processing applied."""
        frame = await super().__anext__()
        return self._processor.process_capture(frame)

    def on_detached(self) -> None:
        """Clean up processor on detach."""
        super().on_detached()
        self._processor.close()


class ProcessedAgentInput(AgentInput):
    """AgentInput subclass that automatically wraps audio with processing.
    
    Overrides the audio setter to wrap provided AudioInput with ProcessedAudioInput.
    This is the main integration point - users opt-in by using this class.
    """

    def __init__(
        self,
        video_changed: Callable[[], None],
        audio_changed: Callable[[], None],
        *,
        audio_processor_factory: Callable[[], AudioProcessor] | None = None,
    ) -> None:
        """Initialize processed agent input.
        
        Args:
            video_changed: Callback when video input changes
            audio_changed: Callback when audio input changes
            audio_processor_factory: Factory function to create AudioProcessor instances.
                                     If None, defaults to WebRTCAudioProcessor.
        """
        super().__init__(video_changed=video_changed, audio_changed=audio_changed)
        self._audio_processor_factory = audio_processor_factory or WebRTCAudioProcessor
        self._current_processor: AudioProcessor | None = None

    @property
    def audio(self) -> AudioInput | None:
        """Get current audio input."""
        return self._audio_stream

    @audio.setter
    def audio(self, stream: AudioInput | None) -> None:
        """Set audio input with automatic processing wrapper."""
        if stream is self._audio_stream:
            return

        # Detach old stream
        if self._audio_stream:
            self._audio_stream.on_detached()
            self._current_processor = None

        if stream is None:
            self._audio_stream = None
            self._audio_changed()
            return

        # Create processor and wrap the stream
        try:
            self._current_processor = self._audio_processor_factory()
            processed_stream = ProcessedAudioInput(stream, self._current_processor)
            self._audio_stream = processed_stream
        except Exception as e:
            logger.warning(f"Failed to create audio processor: {e}. Using raw audio.")
            self._audio_stream = stream

        self._audio_changed()

        # Attach based on enabled state
        if self._audio_stream:
            if self._audio_enabled:
                self._audio_stream.on_attached()
            else:
                self._audio_stream.on_detached()

    def feed_playout_audio(self, frame: rtc.AudioFrame) -> None:
        """Feed far-end audio for echo cancellation.
        
        Call this whenever remote audio is being played back to provide
        the echo canceller with a reference signal.
        
        Args:
            frame: Audio frame being played to speaker
        """
        if self._current_processor:
            self._current_processor.process_playout(frame)


# Convenience factory functions

def create_default_processor() -> AudioProcessor:
    """Create a WebRTC processor with default settings (all features enabled)."""
    return WebRTCAudioProcessor(
        noise_suppression=True,
        echo_cancellation=True,
        auto_gain_control=True,
    )


def create_ns_only_processor() -> AudioProcessor:
    """Create a processor with only noise suppression (no AEC/AGC)."""
    return WebRTCAudioProcessor(
        noise_suppression=True,
        echo_cancellation=False,
        auto_gain_control=False,
    )
