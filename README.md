# ac4fix 📡🔊

> **In-line ATSC 3.0 Dolby AC-4 to AC-3 Live TV Audio Transcoding Proxy for HDHomeRun & Jellyfin.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Docker Image](https://img.shields.io/badge/docker-ghcr.io-blue.svg)](https://github.com/wronglebowsk/ac4fix/pkgs/container/ac4fix)
[![Architecture Docs](https://img.shields.io/badge/Architecture-Design%20Decisions-orange.svg)](ARCHITECTURE.md)

---

## The Problem & Origin Story

NextGen TV (**ATSC 3.0**) broadcasts transmit video in **HEVC (H.265)** and audio in **Dolby AC-4**. 

However, in many U.S. markets, the majority of ATSC 3.0 broadcasts have unfortunately been locked down with **A3SA DRM encryption**, making them completely unplayable on network tuners like the HDHomeRun when paired with third-party software like Jellyfin. This industry-wide encryption controversy and its impact on consumer hardware have been extensively documented:
* [**SiliconDust Official Forum: ATSC 3.0 and DRM FAQ**](https://forum.silicondust.com/forum/viewtopic.php?t=77598) — SiliconDust's technical breakdown explaining why network gateway tuners cannot stream DRM-encrypted NextGen channels to third-party clients, along with their regulatory challenges.
* [**Lon.TV Coverage & Analysis**](https://lon.tv) (and [YouTube Playlist](https://www.youtube.com/results?search_query=lon.tv+atsc+3.0+drm)) — In-depth reporting and consumer advocacy covering how DRM locks antenna viewers out of free public airwaves and breaks whole-home DVR systems.

### The Philadelphia Use Case
In the Philadelphia broadcast market, nearly all major ATSC 3.0 broadcasts (CBS, NBC, FOX, CW, Univision) have turned on DRM encryption. However, **WPVI-HD (ABC 6)** remains broadcast in the clear without DRM.

It was on this unencrypted broadcast where the author encountered the core issue:
* Most consumer streaming clients (Android TV, Apple TV, Fire TV, Chromecast, Web browsers) **lack hardware Dolby AC-4 audio decoders**.
* In apps like **Jellyfin Android TV (ExoPlayer)**, the client device profile erroneously advertises container/AC-4 acceptance. The Jellyfin server chooses **Direct Stream** (no transcoding).
* When the client player receives the stream, the physical SoC cannot decode the AC-4 track, resulting in **crystal-clear 1080p/4K video with complete silence**.
* Attempting to force server-side Live TV transcoding causes Jellyfin's HLS segmentation to introduce **8–15 seconds of channel-changing lag** and frequently triggers unnecessary video re-encoding.

> [!IMPORTANT]
> **Scope Limitation:** `ac4fix` **only works with unencrypted ATSC 3.0 broadcasts**. It does not and cannot bypass A3SA DRM encryption. Its sole purpose is solving the audio codec incompatibility on clear, unencrypted NextGen TV signals.

---

## The Solution

`ac4fix` is a lightweight, zero-dependency pre-ingest proxy that sits between your HDHomeRun tuner and Jellyfin:

```
┌─────────────────────────┐
│ HDHomeRun FLEX 4K Tuner │ (192.168.1.x:5004)
└────────────┬────────────┘
             │  HEVC Video + Dolby AC-4 Audio (Raw ATSC 3.0 TS)
             ▼
┌─────────────────────────┐
│         ac4fix          │ (localhost:5010)
│  ┌───────────────────┐  │
│  │  jellyfin-ffmpeg   │  │  -c:v copy (0% CPU load, untouched video)
│  │                   │  │  -c:a ac3 -b:a 384k -ac 6 (AC-4 -> AC-3 5.1)
│  └───────────────────┘  │
└────────────┬────────────┘
             │  HEVC Video + Dolby Digital (AC-3) 5.1 Audio (MPEG-TS)
             ▼
┌─────────────────────────┐
│  Jellyfin Media Server  │ (Imported via /channels.m3u)
└────────────┬────────────┘
             │  Direct Stream (Universal client compatibility)
             ▼
┌─────────────────────────────────────────────────────────┐
│ Clients: Android TV, Apple TV, Web, Roku, iOS, Fire TV  │
│              (Instant start, full 5.1 sound)            │
└─────────────────────────────────────────────────────────┘
```

1. **Zero Video Penalty (`-c:v copy`):** Video is stream-copied untouched with zero transcoding loss and negligible CPU usage (< 1%).
2. **Dolby Digital 5.1 Surround (`-c:a ac3 -b:a 384k -ac 6`):** Audio is re-encoded to universal AC-3, supported natively by 100% of televisions and receivers.
3. **Instant Channel Tuning:** Emits raw MPEG-TS over chunked HTTP—no HLS disk segmentation or buffering latency.
4. **Automatic Lineup Discovery:** Auto-queries the HDHomeRun `/lineup.json` to find all non-DRM ATSC 3.0 channels with AC-4 audio.
5. **Seamless EPG Guide Sharing:** Generates an M3U playlist with `tvg-id` and `tvg-chno` headers that link directly to your existing XMLTV or HDHomeRun guide data.

> [!NOTE]
> For a deep dive into the engineering reasons why Jellyfin doesn't automatically convert this audio and why client profiles fail, read [**ARCHITECTURE.md**](ARCHITECTURE.md).

---

## Quickstart

### Option A: Docker Compose (Recommended)

Save the following as `docker-compose.yml`:

```yaml
services:
  ac4fix:
    image: ghcr.io/wronglebowsk/ac4fix:latest
    container_name: ac4fix
    restart: unless-stopped
    ports:
      - "5010:5010"
    environment:
      - TUNER_IP=192.168.1.134     # Your HDHomeRun IP address
      - PORT=5010
      - AUDIO_CODEC=ac3
      - AUDIO_BITRATE=384k
      - AUDIO_CHANNELS=6
```

Run:
```bash
docker compose up -d
```

### Option B: Bare Metal / Linux Systemd

1. Ensure `jellyfin-ffmpeg` or a modern `ffmpeg` with the AC-4 decoder is installed:
   ```bash
   /usr/lib/jellyfin-ffmpeg/ffmpeg -decoders | grep ac4
   ```
2. Clone the repository:
   ```bash
   git clone https://github.com/wronglebowsk/ac4fix.git /opt/ac4fix
   ```
3. Copy and configure the systemd unit:
   ```bash
   sudo cp /opt/ac4fix/systemd/ac4fix.service /etc/systemd/system/
   sudo cp /opt/ac4fix/systemd/ac4fix.env.example /etc/default/ac4fix
   # Edit /etc/default/ac4fix with your HDHomeRun IP
   sudo systemctl daemon-reload
   sudo systemctl enable --now ac4fix
   ```

---

## Configuring Jellyfin

Once `ac4fix` is running:

1. Open **Jellyfin Dashboard** $\rightarrow$ **Live TV**.
2. Under **Tuner Devices**, click **Add (+)**.
3. Choose **Tuner Type**: `M3U Tuner`.
4. Enter the **File or URL**:
   ```text
   http://<ac4fix-host-ip>:5010/channels.m3u
   ```
5. Click **Save**.
6. Under **TV Guide Data Providers**, click your existing **XMLTV** or **HDHomeRun Guide** provider and map the new AC-3 channels (e.g. `106.1`) to the existing broadcast guide data.

---

## Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/channels.m3u` | `GET` | Dynamic M3U playlist for Jellyfin/Plex/VLC with auto-detected client `Host` URLs. |
| `/auto/v<channel>` | `GET` | Live MPEG-TS stream with HEVC video + converted AC-3 audio. |
| `/health` | `GET` | JSON status returning tuner connection, active transcode streams, and channel inventory. |

---

## Configuration Reference

Parameters can be passed as CLI arguments or environment variables:

| CLI Option | Environment Variable | Default | Description |
|---|---|---|---|
| `--tuner` | `TUNER_URL` or `TUNER_IP` | `http://192.168.1.134:5004` | HDHomeRun IP or base URL |
| `--port` | `PORT` | `5010` | HTTP port to listen on |
| `--bind` | `BIND_ADDR` | `0.0.0.0` | Network interface to bind |
| `--ffmpeg` | `FFMPEG_PATH` | `/usr/lib/jellyfin-ffmpeg/ffmpeg` | Path to FFmpeg executable |
| `--audio-codec` | `AUDIO_CODEC` | `ac3` | Target audio codec (`ac3`, `eac3`, `aac`) |
| `--audio-bitrate` | `AUDIO_BITRATE` | `384k` | Output audio bitrate |
| `--audio-channels` | `AUDIO_CHANNELS` | `6` | Audio output channels (`6` for 5.1 surround, `2` for stereo) |
| `--channels` | `CHANNELS` | `""` (Auto-discover) | Manual channel definitions (`chan=Name:ChNo:XmltvId`) |
| `--no-auto-discover` | `AUTO_DISCOVER=0` | `False` | Disable automatic lineup discovery |

---

## DRM Note

NextGen TV channels encrypted with **A3SA DRM** (`DRM: 1` in `/lineup.json`) cannot be decrypted or played by any third-party software, including Jellyfin, Plex, or FFmpeg. `ac4fix` automatically skips DRM-encrypted channels during lineup discovery.

---

## Acknowledgments

This project was created in collaboration with **Gemini Flash 3.8** and **Antigravity**.

---

## License

MIT License. See [LICENSE](LICENSE) for details.
