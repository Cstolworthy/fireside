FROM python:3.12-slim
WORKDIR /app
COPY server.py index.html ./
# bind on all interfaces so it is reachable from the LAN; --out keeps books on a volume
EXPOSE 8765
CMD ["python3", "server.py", "--host", "0.0.0.0", "--port", "8765", "--out", "/data"]
