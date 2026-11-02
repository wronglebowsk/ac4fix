#!/usr/bin/env python3
"""
ac4fix - In-line ATSC 3.0 Dolby AC-4 to AC-3 audio transcoding proxy.

Designed for HDHomeRun tuners and Jellyfin/Emby/Plex media servers.
Bypasses client-side AC-4 decoding limitations by remuxing HEVC video untouched
and re-encoding only the AC-4 audio to universally compatible Dolby Digital (AC-3).
"""

import argparse
import http.server
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Dict, Optional, Tuple

# Defaults
DEFAULT_PORT = 5010
DEFAULT_BIND = "0.0.0.0"
DEFAULT_AUDIO_CODEC = "ac3"
DEFAULT_AUDIO_BITRATE = "384k"
DEFAULT_AUDIO_CHANNELS = 6
DEFAULT_SAMPLE_RATE = 48000

# Channel definition: guide_number -> (display_name, virtual_channel_no, xmltv_id)
ChannelMap = Dict[str, Tuple[str, str, str]]


def find_default_ffmpeg() -> str:
    """Locate the best available FFmpeg binary with AC-4 decoding support."""
    # Priority: Environment variable -> jellyfin-ffmpeg -> system ffmpeg
    if os.environ.get("FFMPEG_PATH"):
        return os.environ["FFMPEG_PATH"]
    jellyfin_path = "/usr/lib/jellyfin-ffmpeg/ffmpeg"
    if os.path.isfile(jellyfin_path) and os.access(jellyfin_path, os.X_OK):
        return jellyfin_path
    sys_path = shutil.which("ffmpeg")
    if sys_path:
        return sys_path
    return "ffmpeg"


class ProxyConfig:
    def __init__(
        self,
        tuner_url: str,
        port: int = DEFAULT_PORT,
        bind_addr: str = DEFAULT_BIND,
        ffmpeg_bin: str = "ffmpeg",
        audio_codec: str = DEFAULT_AUDIO_CODEC,
        audio_bitrate: str = DEFAULT_AUDIO_BITRATE,
        audio_channels: int = DEFAULT_AUDIO_CHANNELS,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        channels: Optional[ChannelMap] = None,
        auto_discover: bool = True,
    ):
        self.tuner_url = tuner_url.rstrip("/")
        # Extract base HTTP control URL (e.g. port 80) and stream port (usually 5004)
        if ":5004" in self.tuner_url:
            self.tuner_control_url = self.tuner_url.replace(":5004", "")
            self.tuner_stream_url = self.tuner_url
        else:
            self.tuner_control_url = self.tuner_url
            self.tuner_stream_url = f"{self.tuner_url}:5004"

        self.port = port
        self.bind_addr = bind_addr
        self.ffmpeg_bin = ffmpeg_bin
        self.audio_codec = audio_codec
        self.audio_bitrate = audio_bitrate
        self.audio_channels = audio_channels
        self.sample_rate = sample_rate
        self.channels: ChannelMap = channels or {}
        self.auto_discover = auto_discover


class StreamManager:
    def __init__(self, config: ProxyConfig):
        self.config = config
        self.lock = threading.Lock()
        self.active_streams: Dict[str, float] = {}

    def fetch_lineup(self) -> None:
        """Query HDHomeRun lineup.json to automatically discover non-DRM AC-4 channels."""
        lineup_url = f"{self.config.tuner_control_url}/lineup.json"
        sys.stdout.write(f"Probing tuner lineup at {lineup_url}...\n")
        sys.stdout.flush()

        try:
            req = urllib.request.Request(lineup_url, headers={"User-Agent": "ac4fix/1.0"})
            with urllib.request.urlopen(req, timeout=5) as response:
                if response.status != 200:
                    sys.stdout.write(f"Warning: Tuner returned HTTP {response.status}\n")
                    return
                data = json.loads(response.read().decode("utf-8"))
        except Exception as e:
            sys.stdout.write(f"Notice: Lineup auto-discovery failed: {e}\n")
            return

        discovered: ChannelMap = {}
        for item in data:
            guide_num = item.get("GuideNumber")
            name = item.get("GuideName", f"Channel {guide_num}")
            audio_codec = item.get("AudioCodec", "").upper()
            is_drm = bool(item.get("DRM", 0))

            # Target ATSC 3.0 channels with AC-4 audio and no DRM
            if audio_codec == "AC4":
                if is_drm:
                    sys.stdout.write(f"Skipping channel {guide_num} ({name}): DRM encrypted\n")
                    continue
                display_name = f"{name} ({self.config.audio_codec.upper()})"
                # Derive virtual channel number from guide number (e.g. 106.1 -> 6.1)
                virtual_num = guide_num
                if guide_num.startswith("1") and "." in guide_num:
                    virtual_num = guide_num[1:]  # e.g. 106.1 -> 06.1 or 6.1
                    if virtual_num.startswith("0"):
                        virtual_num = virtual_num[1:]
                xmltv_id = f"ac4fix-{guide_num}"
                discovered[guide_num] = (display_name, virtual_num, xmltv_id)

        if discovered:
            with self.lock:
                for k, v in discovered.items():
                    if k not in self.config.channels:
                        self.config.channels[k] = v
            sys.stdout.write(f"Auto-discovered {len(discovered)} AC-4 channel(s): {list(discovered.keys())}\n")
            sys.stdout.flush()

    def build_ffmpeg_cmd(self, channel: str) -> list:
        tuner_stream = f"{self.config.tuner_stream_url}/auto/v{channel}"
        return [
            self.config.ffmpeg_bin,
            "-hide_banner",
            "-loglevel", "warning",
            "-fflags", "+genpts+igndts",
            "-analyzeduration", "8M",
            "-probesize", "8M",
            "-i", tuner_stream,
            "-map", "0:v:0",
            "-map", "0:a:0",
            "-c:v", "copy",
            "-c:a", self.config.audio_codec,
            "-b:a", self.config.audio_bitrate,
            "-ac", str(self.config.audio_channels),
            "-ar", str(self.config.sample_rate),
            "-f", "mpegts",
            "-mpegts_flags", "+resend_headers",
            "-flush_packets", "1",
            "-",
        ]


class RequestHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ac4fix/2.0"

    @property
    def manager(self) -> StreamManager:
        return self.server.manager  # type: ignore

    def log_message(self, fmt, *args):
        sys.stdout.write(f"[{time.strftime('%H:%M:%S')}] {fmt % args}\n")
        sys.stdout.flush()

    def _send_response_data(self, code: int, content_type: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET(head_only=True)

    def do_GET(self, head_only: bool = False):
        path = self.path.split("?", 1)[0]

        # 1. M3U Playlist endpoint
        if path in ("/channels.m3u", "/playlist.m3u", "/m3u"):
            host_header = self.headers.get("Host", f"localhost:{self.manager.config.port}")
            lines = ["#EXTM3U"]
            with self.manager.lock:
                channels = dict(self.manager.config.channels)

            for chan, (name, chno, tvg_id) in channels.items():
                lines.append(
                    f'#EXTINF:-1 tvg-chno="{chno}" tvg-id="{tvg_id}" tvg-name="{name}" '
                    f'channel-id="ac4fix-{chan}",{name}'
                )
                lines.append(f"http://{host_header}/auto/v{chan}")

            body = ("\n".join(lines) + "\n").encode("utf-8")
            return self._send_response_data(200, "text/plain; charset=utf-8", body)

        # 2. Health & Status endpoint
        if path == "/health":
            with self.manager.lock:
                status = {
                    "status": "healthy",
                    "tuner_url": self.manager.config.tuner_url,
                    "ffmpeg_bin": self.manager.config.ffmpeg_bin,
                    "audio_transcode": {
                        "codec": self.manager.config.audio_codec,
                        "bitrate": self.manager.config.audio_bitrate,
                        "channels": self.manager.config.audio_channels,
                    },
                    "active_streams": {
                        k: round(time.time() - v, 1) for k, v in self.manager.active_streams.items()
                    },
                    "channels": self.manager.config.channels,
                }
            return self._send_response_data(200, "application/json", json.dumps(status, indent=2).encode())

        # 3. Live Stream endpoint: /auto/v<channel>
        if path.startswith("/auto/v"):
            chan = path.rsplit("/", 1)[-1][1:]
            return self._stream_channel(chan, head_only)

        return self._send_response_data(404, "text/plain", b"Not Found\n")

    def _stream_channel(self, chan: str, head_only: bool):
        with self.manager.lock:
            channel_valid = chan in self.manager.config.channels

        if not channel_valid:
            return self._send_response_data(404, "text/plain", f"Channel {chan} is not configured\n".encode())

        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "close")
        self.end_headers()

        if head_only:
            return

        cmd = self.manager.build_ffmpeg_cmd(chan)
        self.log_message("Starting stream for channel %s: %s", chan, " ".join(cmd))

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1048576,  # 1MB buffer
        )

        with self.manager.lock:
            self.manager.active_streams[chan] = time.time()

        try:
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self.log_message("Client disconnected from channel %s", chan)
        except Exception as e:
            self.log_message("Stream read error on channel %s: %s", chan, e)
        finally:
            with self.manager.lock:
                self.manager.active_streams.pop(chan, None)

            # Cleanup FFmpeg process
            try:
                if proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass

            try:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=3)
            except Exception:
                proc.kill()

            # Prevent zombie processes
            try:
                proc.wait(timeout=2)
                err = proc.stderr.read().decode(errors="replace").strip() if proc.stderr else ""
                if err:
                    self.log_message("FFmpeg [%s]: %s", chan, err[-300:])
            except Exception:
                pass

            self.log_message("Stream ended for channel %s (code %s)", chan, proc.returncode)


class Ac4FixServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: ProxyConfig):
        super().__init__((config.bind_addr, config.port), RequestHandler)
        self.manager = StreamManager(config)


def parse_args() -> ProxyConfig:
    parser = argparse.ArgumentParser(
        description="ac4fix: In-line ATSC 3.0 AC-4 to AC-3 audio transcoding proxy for Jellyfin."
    )
    parser.add_argument(
        "--tuner",
        default=os.environ.get("TUNER_URL") or os.environ.get("TUNER_IP", "http://192.168.1.134:5004"),
        help="HDHomeRun base URL or IP address (e.g. http://192.168.1.134:5004 or 192.168.1.134)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", DEFAULT_PORT)),
        help=f"Port to bind HTTP server (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--bind",
        default=os.environ.get("BIND_ADDR", DEFAULT_BIND),
        help=f"IP address to bind HTTP server (default: {DEFAULT_BIND})",
    )
    parser.add_argument(
        "--ffmpeg",
        default=find_default_ffmpeg(),
        help="Path to FFmpeg binary with AC-4 decoder support",
    )
    parser.add_argument(
        "--audio-codec",
        default=os.environ.get("AUDIO_CODEC", DEFAULT_AUDIO_CODEC),
        choices=["ac3", "eac3", "aac"],
        help=f"Target transcoded audio codec (default: {DEFAULT_AUDIO_CODEC})",
    )
    parser.add_argument(
        "--audio-bitrate",
        default=os.environ.get("AUDIO_BITRATE", DEFAULT_AUDIO_BITRATE),
        help=f"Audio bitrate (default: {DEFAULT_AUDIO_BITRATE})",
    )
    parser.add_argument(
        "--audio-channels",
        type=int,
        default=int(os.environ.get("AUDIO_CHANNELS", DEFAULT_AUDIO_CHANNELS)),
        choices=[2, 6],
        help=f"Audio output channels: 2 for stereo, 6 for 5.1 surround (default: {DEFAULT_AUDIO_CHANNELS})",
    )
    parser.add_argument(
        "--channels",
        default=os.environ.get("CHANNELS", ""),
        help="Manual channels specification: comma-separated list of chan=Name:ChNo:XmltvId (e.g. 106.1=WPVI-HD:6.1:US19612.hdhomerun.com)",
    )
    parser.add_argument(
        "--no-auto-discover",
        action="store_true",
        default=os.environ.get("AUTO_DISCOVER", "1") in ("0", "false", "False"),
        help="Disable automatic lineup discovery from HDHomeRun",
    )

    args = parser.parse_args()

    tuner_url = args.tuner
    if not tuner_url.startswith("http://") and not tuner_url.startswith("https://"):
        tuner_url = f"http://{tuner_url}"

    channels: ChannelMap = {}
    if args.channels:
        # Parse format: 106.1=WPVI-HD:6.1:US19612.hdhomerun.com,110.1=...
        for item in args.channels.split(","):
            if "=" in item:
                chan_id, rest = item.split("=", 1)
                parts = rest.split(":")
                name = parts[0] if len(parts) > 0 else chan_id
                chno = parts[1] if len(parts) > 1 else chan_id
                xmltv = parts[2] if len(parts) > 2 else f"ac4fix-{chan_id}"
                channels[chan_id.strip()] = (name.strip(), chno.strip(), xmltv.strip())

    return ProxyConfig(
        tuner_url=tuner_url,
        port=args.port,
        bind_addr=args.bind,
        ffmpeg_bin=args.ffmpeg,
        audio_codec=args.audio_codec,
        audio_bitrate=args.audio_bitrate,
        audio_channels=args.audio_channels,
        channels=channels,
        auto_discover=not args.no_auto_discover,
    )


def main():
    config = parse_args()

    sys.stdout.write("====================================================\n")
    sys.stdout.write(" ac4fix: ATSC 3.0 AC-4 to AC-3 Live TV Audio Proxy \n")
    sys.stdout.write("====================================================\n")
    sys.stdout.write(f"Tuner Target  : {config.tuner_url}\n")
    sys.stdout.write(f"Listening on  : {config.bind_addr}:{config.port}\n")
    sys.stdout.write(f"FFmpeg Binary : {config.ffmpeg_bin}\n")
    sys.stdout.write(f"Audio Output  : {config.audio_codec} ({config.audio_channels}ch @ {config.audio_bitrate})\n")

    server = Ac4FixServer(config)

    # Perform auto-discovery if enabled
    if config.auto_discover:
        server.manager.fetch_lineup()

    # Graceful shutdown handler
    def handle_signal(sig, frame):
        sys.stdout.write("\nShutting down ac4fix...\n")
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    sys.stdout.write(f"Ready. M3U playlist available at: http://<server-ip>:{config.port}/channels.m3u\n")
    sys.stdout.flush()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
