"""Video stream receiver for DJI Pocket 3.

Receives H.264 video from type 0x02 UDP packets and pipes to ffplay/ffmpeg.
"""

import subprocess
import threading
import logging
import os
import signal
import queue

logger = logging.getLogger("pocket3.video")


class VideoReceiver:
    """Receives H.264 video data and pipes to a viewer or file."""

    def __init__(self):
        self._ffplay_proc: subprocess.Popen | None = None
        self._file = None
        self._file_path: str | None = None
        self._lock = threading.Lock()
        self._frame_count = 0
        self._byte_count = 0
        self._queue: queue.Queue = queue.Queue(maxsize=500)
        self._writer_thread: threading.Thread | None = None
        self._running = False

    def on_video_data(self, data: bytes):
        """Callback for video data from UDP protocol. Non-blocking."""
        self._frame_count += 1
        self._byte_count += len(data)
        # File write is fast (OS-buffered), do it directly
        if self._file:
            try:
                self._file.write(data)
            except OSError:
                pass
        # Queue for ffplay (slow pipe, may drop if full)
        if self._ffplay_proc:
            try:
                self._queue.put_nowait(data)
            except queue.Full:
                pass  # drop frame rather than block rx_loop

    def _writer_loop(self):
        """Drain queue and write to ffplay pipe. Runs in own thread."""
        while self._running:
            try:
                data = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            with self._lock:
                if self._ffplay_proc and self._ffplay_proc.stdin:
                    try:
                        self._ffplay_proc.stdin.write(data)
                        self._ffplay_proc.stdin.flush()
                    except (BrokenPipeError, OSError):
                        logger.warning("ffplay pipe broken")
                        self._ffplay_proc = None

    def start_viewer(self, width: int = 1920, height: int = 1080, low_latency: bool = True):
        """Start ffplay to display the video stream."""
        cmd = [
            "ffplay",
            "-f", "h264",
            "-framerate", "30",
            "-i", "pipe:0",
            "-window_title", "DJI Pocket 3",
        ]
        if low_latency:
            cmd.extend([
                "-fflags", "nobuffer",
                "-flags", "low_delay",
                "-framedrop",
                "-probesize", "4096",
                "-analyzeduration", "1000000",
            ])

        logger.info(f"Starting viewer: {' '.join(cmd)}")
        try:
            self._ffplay_proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            logger.info(f"ffplay started (PID {self._ffplay_proc.pid})")
            self._ensure_writer()
        except FileNotFoundError:
            logger.error("ffplay not found! Install ffmpeg to view video.")
            logger.info("Video data is still being received, just not displayed.")

    def start_recording(self, filename: str = "pocket3_video.h264"):
        """Start recording raw H.264 to file."""
        self._file_path = filename
        self._file = open(filename, "wb")
        logger.info(f"Recording to {filename}")
        self._ensure_writer()

    def stop_viewer(self):
        """Stop ffplay."""
        if self._ffplay_proc:
            try:
                self._ffplay_proc.stdin.close()
            except Exception:
                pass
            try:
                self._ffplay_proc.terminate()
                self._ffplay_proc.wait(timeout=3)
            except Exception:
                try:
                    self._ffplay_proc.kill()
                except Exception:
                    pass
            self._ffplay_proc = None
            logger.info("Viewer stopped")

    def stop_recording(self):
        """Stop recording."""
        if self._file:
            self._file.close()
            self._file = None
            logger.info(f"Recording saved to {self._file_path}")

    def _ensure_writer(self):
        """Start writer thread if not running."""
        if not self._running:
            self._running = True
            self._writer_thread = threading.Thread(
                target=self._writer_loop, daemon=True, name="video-writer")
            self._writer_thread.start()

    def stop(self):
        """Stop everything."""
        self._running = False
        if self._writer_thread:
            self._writer_thread.join(timeout=2)
            self._writer_thread = None
        self.stop_viewer()
        self.stop_recording()

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def byte_count(self) -> int:
        return self._byte_count
