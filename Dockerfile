FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl tar && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared \
 && chmod +x /usr/local/bin/cloudflared
COPY requirements.txt pyquotex-1.0.3.tar.gz ./
RUN pip install --upgrade pip setuptools wheel \
 && pip install -r requirements.txt \
 && mkdir -p /opt/pyquotex-src \
 && tar -xzf pyquotex-1.0.3.tar.gz -C /opt/pyquotex-src \
 && cp -a /opt/pyquotex-src/pyquotex /app/pyquotex \
 && python -c "from pyquotex.stable_api import Quotex; print('pyquotex import OK')"
COPY app.py ./
EXPOSE 5000
CMD ["python", "app.py"]
