"""GeoHazard GMPE servisi: OpenQuake hazardlib'i ayrı bir süreçte HTTP üzerinden sunar.

Bu servis AGPL-3.0'dır (OpenQuake hazardlib'i değiştirilmeden kullanır). Kaynak kodu ``/source``
ucunun verdiği adreste yayımlanır. Yalnız GeoHazard API'si çağırır: her istek ``X-Service-Key``
başlığını ``GMPE_SERVICE_KEY`` ortam değişkeniyle sabit zamanlı karşılaştırarak doğrular.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import os
from typing import Annotated, List, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from openquake.baselib import __version__ as ENGINE_VERSION
from pydantic import BaseModel, ConfigDict, Field

from gmpe_service import compute

SOURCE_URL = os.environ.get("GMPE_SOURCE_URL", "")
MAX_RUPTURE_BYTES = 2_000_000
MAX_BODY_BYTES = 3_500_000  # base64 (~2,7 MB) + saha listesi (2500 saha ≈ 250 kB)
PROTECTED_PREFIX = "/v1/"
Name = Annotated[str, Field(min_length=1, max_length=100)]

app = FastAPI(title="GeoHazard GMPE", docs_url=None, redoc_url=None, openapi_url=None)


def _expected_key() -> str:
    return os.environ.get("GMPE_SERVICE_KEY", "")


def _key_ok(given: str) -> bool:
    expected = _expected_key()
    return bool(expected) and bool(given) and hmac.compare_digest(given.encode(), expected.encode())


class GuardMiddleware:
    """Gövde OKUNMADAN önce /v1/ isteklerinde anahtar ve boyut denetimi (anahtarsız istemci RAM'i
    dolduramasın). Content-Length zorunlu ve en çok MAX_BODY_BYTES; akış da aynı sınırla kesilir."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith(PROTECTED_PREFIX):
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        if not _expected_key():
            return await _plain(send, 503, "service_key_missing")
        if not _key_ok(headers.get("x-service-key", "")):
            return await _plain(send, 401, "unauthorized")
        length = headers.get("content-length", "")
        if not length.isdigit():
            return await _plain(send, 411, "length_required")
        if int(length) > MAX_BODY_BYTES:
            return await _plain(send, 413, "body_too_large")
        seen = 0

        async def limited():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > MAX_BODY_BYTES:
                    raise ValueError("body_too_large")
            return message

        return await self.app(scope, limited, send)


async def _plain(send, status: int, code: str) -> None:
    body = json.dumps({"error": {"code": code}}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


app.add_middleware(GuardMiddleware)


class SiteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lon: float
    lat: float
    vs30: float
    vs30measured: bool
    z1pt0: Optional[float] = None
    z2pt5: Optional[float] = None


class ScenarioIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rupture_xml_b64: str = Field(max_length=MAX_RUPTURE_BYTES * 2)
    sites: List[SiteIn] = Field(min_length=1, max_length=compute.MAX_SITES)
    gsims: List[Name] = Field(min_length=1, max_length=compute.MAX_GSIMS)
    imts: List[Name] = Field(min_length=1, max_length=compute.MAX_IMTS)
    rupture_mesh_spacing: float = Field(default=2.0, ge=compute.MIN_MESH_KM, le=compute.MAX_MESH_KM)
    maximum_distance: float = Field(default=300.0, gt=0, le=1000)


def _authorize(key: Optional[str]) -> None:
    """Middleware'in arkasında ikinci kat (middleware kaldırılırsa da uç açık kalmasın)."""
    if not _expected_key():
        raise HTTPException(503, "GMPE_SERVICE_KEY tanımlı değil.")
    if not _key_ok(key or ""):
        raise HTTPException(401, "Yetkisiz.")


@app.get("/health")
def health() -> dict:
    return {"ok": True, "engine": ENGINE_VERSION}


@app.get("/source")
def source() -> dict:
    return {"license": "AGPL-3.0-or-later", "source": SOURCE_URL, "engine": ENGINE_VERSION}


@app.post("/v1/scenario")
def scenario(body: ScenarioIn, x_service_key: Optional[str] = Header(default=None)):
    _authorize(x_service_key)
    try:
        xml = base64.b64decode(body.rupture_xml_b64, validate=True)
    except (binascii.Error, ValueError):
        return _error("rupture_encoding", "rupture_xml_b64 geçerli base64 değil.")
    if len(xml) > MAX_RUPTURE_BYTES:
        return _error("rupture_too_large", "Kırılma XML'i çok büyük.")
    sites = [compute.Site(**s.model_dump()) for s in body.sites]
    try:
        result = compute.scenario(xml, sites, body.gsims, body.imts,
                                  body.rupture_mesh_spacing, body.maximum_distance)
    except compute.ComputeError as exc:
        return _error(exc.code, str(exc))
    return {"engine": ENGINE_VERSION, **result}


def _error(code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=422, content={"error": {"code": code, "message": message}})
