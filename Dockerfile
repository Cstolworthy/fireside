FROM python:3.12-slim
WORKDIR /app
COPY . .
ENV PYTHONUNBUFFERED=1 FIRESIDE_DATA=/data
EXPOSE 8788
# the v2 host: plugins discovered from ./plugins, state on the /data volume
CMD ["python3", "-m", "host.app", "--host", "0.0.0.0", "--port", "8788"]
