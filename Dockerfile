# Use official Jellyfin image which bundles jellyfin-ffmpeg with the AC-4 decoder
FROM jellyfin/jellyfin:latest

USER root

# Install minimal Python 3 runtime & curl for healthcheck
RUN apt-get update && \
    apt-get install -y --no-install-recommends python3 curl && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY ac4fix.py /app/ac4fix.py
RUN chmod +x /app/ac4fix.py

ENV FFMPEG_PATH="/usr/lib/jellyfin-ffmpeg/ffmpeg"
ENV PORT=5010
ENV BIND_ADDR="0.0.0.0"
ENV AUDIO_CODEC="ac3"
ENV AUDIO_BITRATE="384k"
ENV AUDIO_CHANNELS=6

EXPOSE 5010

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:5010/health || exit 1

ENTRYPOINT ["python3", "/app/ac4fix.py"]
