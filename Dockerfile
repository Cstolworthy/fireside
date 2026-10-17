FROM python:3.12-slim
WORKDIR /app
# MySQL backend support; SQLite is the default and needs no extra package
RUN pip install --no-cache-dir pymysql==1.1.1
COPY . .
ENV PYTHONUNBUFFERED=1 \
    FIRESIDE_DATA=/data \
    FIRESIDE_PLUGINS_ROOT=plugins
# run as a non-root user; the app only ever writes under /data
RUN useradd -r -u 10001 -m fireside \
 && mkdir -p /data && chown -R fireside:fireside /app /data
USER fireside
EXPOSE 8788
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8788/health').status==200 else 1)"
# the v2 host: plugins discovered from ./plugins, state on the /data volume
CMD ["python3", "-m", "host.app", "--host", "0.0.0.0", "--port", "8788"]
