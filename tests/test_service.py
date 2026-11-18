import base64
from pathlib import Path

import numpy
import pytest
from fastapi.testclient import TestClient
from openquake.hazardlib import imt as imt_module
from openquake.hazardlib import valid
from openquake.hazardlib.contexts import get_mean_stds

from gmpe_service import compute
from gmpe_service.app import app

RUPTURE = (Path(__file__).parent / "data" / "rupture_model.xml").read_bytes()
GSIMS = ["AkkarEtAlRjb2014", "BooreEtAl2014", "KaleEtAl2015Turkey"]
IMTS = ["PGA", "SA(0.2)", "SA(1.0)"]
SITES = [compute.Site(29.5, 40.0, 760.0, True), compute.Site(30.5, 40.8, 400.0, False),
         compute.Site(35.0, 45.0, 760.0, True)]  # üçüncüsü 300 km'den uzak


def test_scenario_returns_finite_values_for_sites_in_range():
    out = compute.scenario(RUPTURE, SITES, GSIMS, IMTS)
    assert out["magnitude"] == 7.1 and out["rake"] == 90.0
    d = out["distances"]
    assert d["rjb"][0] is not None and d["rrup"][0] >= d["rjb"][0] >= 0
    assert d["rjb"][1] > d["rjb"][0]
    assert all(d[name][2] is None for name in compute.DISTANCES)
    # GSIM rx/ry0 istemese de menzil içindeki sahada dört mesafe dolu (None = yalnız menzil dışı)
    assert all(isinstance(d[name][k], float) for name in compute.DISTANCES for k in (0, 1))
    for g in GSIMS:
        for i in IMTS:
            block = out["results"][g][i]
            assert block["mean_ln"][2] is None
            assert all(isinstance(block[k][0], float) for k in ("mean_ln", "sigma", "tau", "phi"))
            assert block["mean_ln"][0] > block["mean_ln"][1]  # yakın kaya saha daha büyük (PGA dahil)
            assert block["sigma"][0] > 0


def test_values_equal_engine_gsim_on_same_context():
    """Servisin yolunu (kırılma → ContextMaker) motorun düz ``get_mean_stds``'iyle karşılaştır."""
    out = compute.scenario(RUPTURE, SITES[:2], ["BooreEtAl2014"], ["SA(1.0)"])
    d = out["distances"]
    ctx = numpy.recarray(2, dtype=[(n, float) for n in
                                   ("mag", "rake", "rjb", "vs30", "sids")])
    ctx.mag, ctx.rake = 7.1, 90.0
    ctx.rjb = d["rjb"][:2]
    ctx.vs30 = [760.0, 400.0]
    ctx.sids = [0, 1]
    mean, sig, tau, phi = get_mean_stds(valid.gsim("BooreEtAl2014"), ctx,
                                        [imt_module.from_string("SA(1.0)")])
    block = out["results"]["BooreEtAl2014"]["SA(1.0)"]
    numpy.testing.assert_allclose(block["mean_ln"], mean[0], rtol=1e-12)
    numpy.testing.assert_allclose(block["sigma"], sig[0], rtol=1e-12)


@pytest.mark.parametrize("gsims,imts,code", [
    (["NoSuchModel"], ["PGA"], "unknown_gsim"),
    (["BooreEtAl2014"], ["XYZ"], "unknown_imt"),
    (["ChiouYoungs2014"], ["PGA"], "missing_site_param"),
])
def test_input_errors(gsims, imts, code):
    with pytest.raises(compute.ComputeError) as err:
        compute.scenario(RUPTURE, SITES, gsims, imts)
    assert err.value.code == code


def test_bad_rupture():
    with pytest.raises(compute.ComputeError) as err:
        compute.scenario(b"<nrml/>", SITES, GSIMS, IMTS)
    assert err.value.code == "rupture_invalid"


def _body(**over):
    body = {"rupture_xml_b64": base64.b64encode(RUPTURE).decode(), "gsims": ["BooreEtAl2014"],
            "imts": ["PGA"], "sites": [{"lon": 29.5, "lat": 40.0, "vs30": 760, "vs30measured": True}]}
    return {**body, **over}


def test_http_requires_key(monkeypatch):
    monkeypatch.setenv("GMPE_SERVICE_KEY", "k" * 32)
    client = TestClient(app)
    assert client.post("/v1/scenario", json=_body()).status_code == 401
    assert client.post("/v1/scenario", json=_body(), headers={"X-Service-Key": "x"}).status_code == 401
    ok = client.post("/v1/scenario", json=_body(), headers={"X-Service-Key": "k" * 32})
    assert ok.status_code == 200 and ok.json()["results"]["BooreEtAl2014"]["PGA"]["mean_ln"][0] < 0
    bad = client.post("/v1/scenario", json=_body(gsims=["Nope"]), headers={"X-Service-Key": "k" * 32})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "unknown_gsim"


def test_http_without_configured_key_is_unavailable(monkeypatch):
    monkeypatch.delenv("GMPE_SERVICE_KEY", raising=False)
    assert TestClient(app).post("/v1/scenario", json=_body()).status_code == 503


def test_health_and_source():
    client = TestClient(app)
    assert client.get("/health").json()["engine"].startswith("3.23")
    assert client.get("/source").json()["license"].startswith("AGPL")
