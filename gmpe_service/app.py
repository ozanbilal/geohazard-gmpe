"""GeoHazard GMPE servisi: OpenQuake hazardlib'i ayrı bir süreçte HTTP üzerinden sunar.

Bu servis AGPL-3.0'dır (OpenQuake hazardlib'i değiştirilmeden kullanır). Kaynak kodu ``/source``
ucunun verdiği adreste yayımlanır. Yalnız GeoHazard API'si çağırır: her istek ``X-Service-Key``
başlığını ``GMPE_SERVICE_KEY`` ortam değişkeniyle sabit zamanlı karşılaştırarak doğrular.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import os
from typing import List, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from openquake.baselib import __version__ as ENGINE_VERSION
from pydantic import BaseModel, ConfigDict, Field

from gmpe_service import compute

SOURCE_URL = os.environ.get("GMPE_SOURCE_URL", "")
MAX_RUPTURE_BYTES = 2_000_000

app = FastAPI(title="GeoHazard GMPE", docs_url=None, redoc_url=None, openapi_url=None)


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
    gsims: List[str] = Field(min_length=1, max_length=compute.MAX_GSIMS)
    imts: List[str] = Field(min_length=1, max_length=compute.MAX_IMTS)
    rupture_mesh_spacing: float = 2.0
    maximum_distance: float = Field(default=300.0, gt=0, le=1000)


def _authorize(key: Optional[str]) -> None:
    expected = os.environ.get("GMPE_SERVICE_KEY", "")
    if not expected:
        raise HTTPException(503, "GMPE_SERVICE_KEY tanımlı değil.")
    if not key or not hmac.compare_digest(key.encode(), expected.encode()):
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
