# Dockerfile is provided to satisfy the Glama listing check (the server must
# start and respond to MCP introspection in a container).
#
# NOTE: This server reads ~/Library/Messages/chat.db from a host macOS install
# and needs Full Disk Access. It cannot return real data from inside a Linux
# container — that file simply isn't there. Use the host install instructions
# in README.md for actual use; this image only exists so introspection passes.

FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir "mcp>=1.2.0"

COPY server.py /app/server.py

# stdio MCP server
CMD ["python", "/app/server.py"]
