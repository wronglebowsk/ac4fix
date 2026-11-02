# Architecture & Design Decisions: ac4fix

This document details the technical problem, system failure modes, and architectural decisions behind `ac4fix`—an in-line audio transcoding proxy designed to bridge NextGen TV (ATSC 3.0) broadcasts from HDHomeRun network tuners into Jellyfin and other open-source media centers.

---

## 1. The Core Problem: The ATSC 3.0 & Dolby AC-4 Dilemma

In North America, television broadcasting is transitioning from **ATSC 1.0** (standard digital TV) to **ATSC 3.0** ("NextGen TV").

| Standard | Video Codec | Audio Codec | Client Ecosystem Support |
|---|---|---|---|
| **ATSC 1.0** | MPEG-2 (or H.264) | **AC-3 (Dolby Digital)** / E-AC-3 | Universally supported across virtually all TVs, streaming boxes, browsers, and mobile devices. |
| **ATSC 3.0** | **HEVC (H.265)** | **Dolby AC-4** | Extremely limited. Supported natively only on select high-end Smart TVs with built-in ATSC 3.0 hardware tuners. |

### The Licensing Barrier
Unlike legacy AC-3 (whose patents have largely expired) and AAC, **Dolby AC-4** is a modern, proprietary, object-based audio codec protected by active patents and licensing agreements.
* **Consumer streaming hardware**—including the Nvidia Shield, Chromecast with Google TV, Amazon Fire TV, Apple TV, Roku streaming sticks, and generic Android TV boxes—do **not** include a licensed hardware Dolby AC-4 DSP decoder.
* **Web Browsers** (Chrome, Firefox, Safari, Edge) do not decode AC-4 bitstreams.
* **Upstream FFmpeg** historically contained no AC-4 decoder due to patent risks and the lack of an open standard specification.

When network tuners such as the **SiliconDust HDHomeRun FLEX 4K** receive ATSC 3.0 OTA signals, they demux the broadcast packets and expose raw transport streams over HTTP (`http://<tuner>:5004/auto/v<channel>`). These streams contain pristine 1080p/4K HDR HEVC video paired with Dolby AC-4 audio.

---

## 2. Why Jellyfin Fails Automatically: The 4 System Failure Modes

A frequent question is: *Why can't Jellyfin simply detect the AC-4 track and transcode only the audio to AC-3 or AAC on the fly?*

The failure is not a simple configuration oversight; it is rooted in how open-source media servers negotiate playback capabilities with client apps.

```
                  ┌──────────────────────────────────────────────┐
                  │              Incoming Broadcast              │
                  │        HEVC Video + Dolby AC-4 Audio         │
                  └──────────────────────┬───────────────────────┘
                                         │
                                         ▼
                               ┌───────────────────┐
                               │  Jellyfin Server  │
                               └─────────┬─────────┘
                                         │
            ┌────────────────────────────┴────────────────────────────┐
            ▼                                                         ▼
┌───────────────────────┐                                 ┌───────────────────────┐
│     Scenario A:       │                                 │     Scenario B:       │
│ Client Profile Match  │                                 │ Force Server Transcode│
│   (The Direct Stream  │                                 │   (Live HLS Pipeline  │
│         Trap)         │                                 │       Failure)        │
└───────────┬───────────┘                                 └───────────┬───────────┘
            │                                                         │
            ▼                                                         ▼
• Client claims container/AC-4 support                     • Spawns HLS segmenter
• Jellyfin selects Direct Stream (no transcode)           • Probing timeouts on live TS
• Client SoC lacks AC-4 decoder chip                       • Re-encodes video (high CPU load)
• ExoPlayer fails silently                                • 8-15 second channel flip latency
            │                                                         │
            ▼                                                         ▼
 [Video Plays, Audio is Silent]                            [Buffering Spinners / Crashes]
```

### Failure Mode 1: The Client Profile "False-Positive" (The Direct Stream Trap)
When a Jellyfin client app (e.g. Jellyfin Android TV) requests a stream, it sends a `DeviceProfile` detailing its supported containers and codecs:
1. Android TV clients rely on Google's **ExoPlayer** media engine. On many Android TV devices, the operating system's media framework enumerates `audio/ac4` as a supported MIME type or permits container passthrough of MPEG-TS containing AC-4 tracks.
2. The Jellyfin server evaluates the stream against the profile:
   * Container: `mpegts` $\rightarrow$ Allowed
   * Video: `hevc` $\rightarrow$ Allowed
   * Audio: `ac4` $\rightarrow$ Claimed allowed (or allowed in container profile)
3. **The Result:** Jellyfin decides that transcoding is unnecessary and selects **Direct Stream** (copying the raw stream without modification).
4. **The Failure:** When ExoPlayer passes the AC-4 bitstream to the device's hardware audio sink, the hardware decoder does not exist. ExoPlayer drops the audio frames silently or fails to instantiate the audio track. The user gets **smooth video, but total silence**.

### Failure Mode 2: Live Broadcast Probing & Stream Complexity
In standard Video-on-Demand (VOD) playback, media servers have static files on disk that can be deeply probed with `ffprobe` prior to playback. 
In Live TV:
* The server reads from an unbounded HTTP socket (`/auto/v<channel>`).
* ATSC 3.0 broadcasts feature complex transport streams with multiple sub-streams, presentation descriptors, emergency alert layers, and multiple audio tracks (e.g., secondary audio programming or dialogue enhancement).
* Jellyfin's live TV media source provider must determine stream parameters within a tight timeout (typically 3–5 seconds). If packets are delayed or descriptors cannot be parsed immediately, stream negotiation defaults to passthrough or errors out entirely.

### Failure Mode 3: The HLS Transcoding Latency Penalty
When Jellyfin is forced to transcode a Live TV stream (for example, if a client explicitly rejects AC-4):
* Jellyfin wraps the stream in an **HLS (`.m3u8`) pipeline**, creating small segmented transport chunks on disk.
* HLS requires buffering multiple segments before client playback can begin.
* This introduces **8 to 15 seconds of latency** every time the user changes the channel.
* Furthermore, unless hardware acceleration is perfectly tuned, live transcoding frequently converts the video stream as well, pegging the host CPU/GPU needlessly for a stream whose HEVC video was already 100% client-compatible.

### Failure Mode 4: Lack of Granular Tuner Rules in Jellyfin
Jellyfin does not currently support per-channel, per-codec transform policies on tuner hosts (e.g., *"For Tuner Host A on channel 106.1, always copy video untouched but convert audio to AC-3"*). Tuner settings are global per host, leaving no native configuration hook to solve the AC-4 asymmetry.

---

## 3. The Solution: Pre-Ingest Audio-Only Remuxing Proxy

Rather than fighting client profiles or modifying server internals, `ac4fix` acts as an **in-line pre-ingest proxy** positioned between the physical tuner and Jellyfin:

```
┌─────────────────────────┐
│ HDHomeRun FLEX 4K Tuner │ (192.168.1.x:5004)
└────────────┬────────────┘
             │  HEVC Video + Dolby AC-4 Audio (Raw ATSC 3.0 TS)
             ▼
┌─────────────────────────┐
│         ac4fix          │ (localhost:5010)
│  ┌───────────────────┐  │
│  │  jellyfin-ffmpeg   │  │  -c:v copy (0% CPU impact, untouched video)
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

### Why This Architecture Works

1. **Zero Video Transcoding Overhead:**
   Using `-c:v copy`, video packets are demuxed and remuxed without decoding. On an entry-level processor (such as an Intel N100), running an active stream consumes negligible CPU resources (< 1%).
2. **Sub-second Channel Tune Times:**
   Because `ac4fix` streams raw MPEG-TS over standard chunked HTTP directly into Jellyfin (without HLS file-segmenting layers), playback initiates almost instantaneously.
3. **Universal Client Direct Play:**
   Dolby Digital (AC-3) is supported by virtually 100% of televisions, AVRs, soundbars, and streaming platforms. Jellyfin sees an incoming `HEVC + AC-3` stream and can safely Direct Stream to all clients.
4. **Seamless Guide (EPG) Sharing:**
   By matching the channel's `tvg-id` in the generated M3U playlist with the official XMLTV channel ID from the HDHomeRun Guide, the restreamed channel automatically inherits the exact broadcast schedule and artwork without duplicate EPG sources.

---

## 4. FFmpeg Pipeline Design

`ac4fix` executes `ffmpeg` with specific stream-mapping and buffering flags optimized for live transport feeds:

```bash
ffmpeg -hide_banner -loglevel warning \
  -fflags +genpts+igndts \
  -analyzeduration 8M -probesize 8M \
  -i http://<TUNER_IP>:5004/auto/v<CHANNEL> \
  -map 0:v:0 -map 0:a:0 \
  -c:v copy \
  -c:a ac3 -b:a 384k -ac 6 -ar 48000 \
  -f mpegts -mpegts_flags +resend_headers -flush_packets 1 \
  -
```

### Flag Rationale:
* `-fflags +genpts+igndts`: Live broadcast streams occasionally have missing or out-of-order Presentation Time Stamps (PTS) and Decode Time Stamps (DTS). This forces FFmpeg to regenerate smooth timestamps, preventing audio/video desync.
* `-analyzeduration 8M -probesize 8M`: Allocates sufficient buffer to reliably parse the AC-4 stream descriptors inside ATSC 3.0 MPEG-TS without timing out.
* `-map 0:v:0 -map 0:a:0`: Explicitly binds the primary video and primary audio tracks. ATSC 3.0 broadcasts often carry secondary descriptive audio, emergency metadata, or alternate language tracks; explicit mapping prevents FFmpeg from dropping the stream or selecting an auxiliary track.
* `-c:v copy`: Stream-copies video untouched (zero generational quality loss, zero GPU/CPU encoding load).
* `-c:a ac3 -b:a 384k -ac 6 -ar 48000`: Transcodes AC-4 to standard 5.1 Dolby Digital at 48kHz / 384kbps, maintaining surround sound spatialization.
* `-mpegts_flags +resend_headers`: Re-emits Program Association Table (PAT) and Program Map Table (PMT) headers frequently, allowing media players to lock onto the live feed mid-stream without waiting for an initial header block.
* `-flush_packets 1`: Flushes muxed packets immediately into stdout, eliminating pipeline buffering lag.

---

## 5. DRM Considerations (A3SA)

NextGen TV broadcasts can be flagged with **A3SA Digital Rights Management (DRM)**.
* **Unencrypted ATSC 3.0 channels** (`DRM: 0`) stream open MPEG-TS and work seamlessly with `ac4fix`.
* **Encrypted ATSC 3.0 channels** (`DRM: 1` in the HDHomeRun `lineup.json`) require proprietary decryption keys and certificates issued by the A3SA consortium. Network tuners cannot decrypt these feeds for third-party consumers, and no open-source software (including Jellyfin or FFmpeg) can play them.
* `ac4fix` queries the HDHomeRun tuner's `/lineup.json` and automatically ignores DRM-flagged channels during auto-discovery.
