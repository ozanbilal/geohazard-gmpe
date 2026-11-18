# GeoHazard GMPE servisi: değiştirilmemiş OpenQuake hazardlib + ince FastAPI sarmalayıcı (AGPL-3.0).
# Bağımlılıklar GEM'in sabitlenmiş tekerlekleri (requirements-oq-linux64.txt = oq-engine v3.23.2
# requirements-py311-linux64.txt); motor sürümü GeoHazard'ın GSIM kataloğuyla (3.23.2) aynı tutulur.
FROM python:3.11.6-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 OQ_DISTRIBUTE=no
# Taban imajın sistem paketleri (libexpat dahil) derleme anında güncellenir.
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/* \n && useradd -m -u 1000 -s /bin/bash gmpe
WORKDIR /app
COPY requirements-oq-linux64.txt requirements-service.txt ./
RUN pip install --no-cache-dir -r requirements-oq-linux64.txt \
 && pip install --no-cache-dir --no-deps "openquake.engine==3.23.2" \
 && pip install --no-cache-dir -r requirements-service.txt
COPY gmpe_service ./gmpe_service
USER gmpe
EXPOSE 8080
CMD ["sh", "-c", "uvicorn gmpe_service.app:app --host :: --port ${PORT:-8080} --workers ${WEB_CONCURRENCY:-2}"]
