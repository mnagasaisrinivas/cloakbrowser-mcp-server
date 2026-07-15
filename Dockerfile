# syntax=docker/dockerfile:1.7
#
# cloakbrowser-mcp-server — headful wrapper around swimmwatch/cloakbrowser-mcp.
# Brings up Xvfb + a window manager + x11vnc, then execs the upstream
# MCP bridge in Streamable HTTP mode. Connect MCP clients to :3000 and
# a VNC viewer to :5900 to drive/watch the same browser.
#
# Build:  docker build -t cloakbrowser-mcp-server .
# Run:    docker run --rm -p 3000:3000 -p 5900:5900 \
#             -e VNC_PASSWORD=changeme \
#             -e DISPLAY_WIDTH=1024 -e DISPLAY_HEIGHT=768 \
#             cloakbrowser-mcp-server

ARG CLOAKBROWSER_MCP_IMAGE=swimmwatch/cloakbrowser-mcp:latest

FROM ${CLOAKBROWSER_MCP_IMAGE} AS base

USER root

ENV DEBIAN_FRONTEND=noninteractive

# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends \
        # Headed X stack for Chromium.
        xvfb xdotool openbox \
        # x11vnc — VNC mirror of :99 on :5900. Password gated by VNC_PASSWORD.
        x11vnc \
        # tini — proper signal handling, zombie reaping.
        tini \
        # socat — kept around in case a future variant wants to forward
        # Chromium's loopback CDP/control socket. Not used today.
        socat \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/cloakbrowser-mcp-server
COPY --chown=node:node scripts/launcher.py /opt/cloakbrowser-mcp-server/launcher.py
RUN chmod 0755 /opt/cloakbrowser-mcp-server/launcher.py

USER node

ENV DISPLAY=:99 \
    DISPLAY_WIDTH=1024 \
    DISPLAY_HEIGHT=768

EXPOSE 3000 5900

# Liveness: cloakbrowser-mcp exposes /healthz once the Streamable HTTP
# bridge is up. The upstream image already documents this endpoint.
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=3 \
    CMD python3 -c "import sys,urllib.request; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:3000/healthz',timeout=3).status==200 else 1)" \
    || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "python3", "/opt/cloakbrowser-mcp-server/launcher.py"]