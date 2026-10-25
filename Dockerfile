FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

COPY vendor/open-webui-weixin/pyproject.toml vendor/open-webui-weixin/README.md ./vendor/open-webui-weixin/
COPY vendor/open-webui-weixin/open_webui_weixin ./vendor/open-webui-weixin/open_webui_weixin/
COPY pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY oui_wechat ./oui_wechat
RUN pip install --no-cache-dir --no-deps ./vendor/open-webui-weixin . \
    && groupadd --gid 1000 oui \
    && useradd --uid 1000 --gid oui --no-create-home oui \
    && mkdir /data \
    && chown oui:oui /data

RUN chmod -R a+rX /app

USER oui
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=3)"
ENTRYPOINT ["oui-wechat", "--host", "0.0.0.0", "--port", "8787", "--data-dir", "/data"]
