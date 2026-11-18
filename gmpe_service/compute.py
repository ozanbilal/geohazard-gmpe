"""Senaryo yer hareketi: OpenQuake hazardlib ile medyan ve standart sapmalar.

Girdi, GeoHazard'ın ürettiği ``multiPlanesRupture`` XML'i (senaryo paketindeki ``rupture_model.xml``
ile aynı), saha listesi, GSIM adları ve IMT'lerdir. Kırılma motorun ``RuptureConverter``'ıyla okunur
(``readinput.get_rupture`` ile aynı yol); mesafeler ve bağlam motorun ``ContextMaker``'ıyla kurulur.
Hesap rastgele alan üretmez: her saha için ln-ortalama ve σ (toplam, olaylar arası, olay içi) döner.

Saha parametreleri açıkça verilir; ``vs30`` ve ``vs30measured`` zorunludur. GSIM ``z1pt0``/``z2pt5``
isterse ve istekte yoksa hata döner (sessiz varsayılan yok).
"""

from __future__ import annotations

import io
import logging
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

import numpy
from openquake.hazardlib import imt as imt_module
from openquake.hazardlib import nrml, sourceconverter, valid
from openquake.hazardlib.calc.filters import get_distances
from openquake.hazardlib.contexts import MAXMAG, MINMAG, ContextMaker
from openquake.hazardlib.site import SiteCollection

# GeoHazard ucuyla aynı sınırlar (servis tek başına daha geniş iş kabul etmez; hesap süresi ağ
# aralığının karesiyle artar: 1020 km kırılma, ağ 1 km, 200 saha, 5 GSIM, 30 IMT ≈ 19 s).
# Harita ızgarası için saha sınırı 2500; toplam iş saha × GSIM × IMT ≤ MAX_WORK
# (690 km kırılma, ağ 1 km, 2500 saha × 3 GSIM × 3 IMT ≈ 1,4 s).
_LOG = logging.getLogger("gmpe_service")

MAX_SITES = 2500
MAX_GSIMS = 5
MAX_IMTS = 30
MAX_WORK = 30_000
MIN_MESH_KM, MAX_MESH_KM = 1.0, 20.0
SITE_FIELDS = ("vs30", "vs30measured", "z1pt0", "z2pt5")
DISTANCES = ("rrup", "rjb", "rx", "ry0")


class ComputeError(ValueError):
    """İstemciye 422 olarak dönen girdi hatası (``code`` makinece okunur)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Site:
    lon: float
    lat: float
    vs30: float
    vs30measured: bool
    z1pt0: Optional[float] = None
    z2pt5: Optional[float] = None


def parse_gsims(names: Sequence[str]) -> List[object]:
    if not names or len(names) > MAX_GSIMS:
        raise ComputeError("gsims_count", f"1-{MAX_GSIMS} GSIM verilmeli.")
    out = []
    for name in names:
        try:
            out.append(valid.gsim(name))
        except Exception:  # motor ad/argüman hatasını çeşitli tiplerle bildirir
            raise ComputeError("unknown_gsim", f"GSIM tanınmadı: {name[:100]}") from None
    return out


def parse_imts(names: Sequence[str]) -> List[object]:
    if not names or len(names) > MAX_IMTS:
        raise ComputeError("imts_count", f"1-{MAX_IMTS} IMT verilmeli.")
    out = []
    for name in names:
        try:
            out.append(imt_module.from_string(name))
        except Exception:
            raise ComputeError("unknown_imt", f"IMT tanınmadı: {name[:40]}") from None
    return out


def check_support(gsim_names: Sequence[str], gsims: Sequence[object], imts: Sequence[object]) -> None:
    """GSIM'in tanımlamadığı IMT türü ya da katsayı tablosu dışındaki periyot motorda KeyError atar;
    hesaptan önce girdi hatası olarak reddedilir (aralık içi periyotları motor kendisi enterpole eder)."""
    for name, gsim in zip(gsim_names, gsims):
        defined = {getattr(kind, "__name__", str(kind)) for kind in gsim.DEFINED_FOR_INTENSITY_MEASURE_TYPES}
        coeffs = getattr(gsim, "COEFFS", None)
        for imt in imts:
            supported = imt.string.split("(")[0] in defined
            if supported and coeffs is not None:
                try:
                    coeffs[imt]
                except KeyError:
                    supported = False
            if not supported:
                raise ComputeError("imt_not_supported", f"{name}, {imt.string} için tanımlı değil.")


def read_rupture(xml: bytes, mesh_spacing: float):
    if not (MIN_MESH_KM <= mesh_spacing <= MAX_MESH_KM):
        raise ComputeError("mesh_spacing_range", f"rupture_mesh_spacing {MIN_MESH_KM:g}-{MAX_MESH_KM:g} km olmalı.")
    try:
        [node] = nrml.read(io.BytesIO(xml))
        rup = sourceconverter.RuptureConverter(mesh_spacing).convert_node(node)
    except Exception as exc:  # ayrıntı loga; yanıta sabit metin
        _LOG.info("kırılma okunamadı: %s", type(exc).__name__)
        raise ComputeError("rupture_invalid", "Kırılma XML'i okunamadı.") from None
    rup.tectonic_region_type = "*"
    return rup


def _site_array(sites: Sequence[Site], required: set) -> SiteCollection:
    if not sites or len(sites) > MAX_SITES:
        raise ComputeError("sites_count", f"1-{MAX_SITES} saha verilmeli.")
    params = sorted({"vs30", "vs30measured"} | (required & set(SITE_FIELDS)))
    for name in ("z1pt0", "z2pt5"):
        if name in params and any(getattr(s, name) is None for s in sites):
            raise ComputeError("missing_site_param", f"Seçilen GSIM her saha için {name} istiyor.")
    for s in sites:
        if not (-180 <= s.lon <= 180 and -90 <= s.lat <= 90):
            raise ComputeError("site_coordinates", "Saha koordinatı aralık dışı.")
        if not (s.vs30 > 0 and math.isfinite(s.vs30)):
            raise ComputeError("vs30_range", "vs30 pozitif ve sonlu olmalı.")
    sitecol = SiteCollection.from_points([s.lon for s in sites], [s.lat for s in sites],
                                         req_site_params=params)
    for name in params:
        sitecol.array[name] = [getattr(s, name) for s in sites]
    return sitecol


def scenario(xml: bytes, sites: Sequence[Site], gsim_names: Sequence[str], imt_names: Sequence[str],
             mesh_spacing: float = 2.0, maximum_distance: float = 300.0) -> Dict[str, object]:
    """Her GSIM × IMT × saha için ln-ortalama ve σ'lar; menzil dışındaki sahalar ``None``."""
    gsims = parse_gsims(gsim_names)
    imts = parse_imts(imt_names)
    check_support(gsim_names, gsims, imts)
    work = len(sites) * len(gsims) * len(imts)
    if work > MAX_WORK:
        raise ComputeError("request_too_large", f"saha × GSIM × IMT en çok {MAX_WORK} olmalı ({work}).")
    rup = read_rupture(xml, mesh_spacing)
    # Motorun _interp yolu: {"default": [(MINMAG, d), (MAXMAG, d)]} → büyüklükten bağımsız menzil.
    magdist = {"default": [(MINMAG, maximum_distance), (MAXMAG, maximum_distance)]}
    cmaker = ContextMaker("*", gsims, {"imtls": {i.string: [0] for i in imts},
                                       "maximum_distance": magdist})
    sitecol = _site_array(sites, set(cmaker.REQUIRES_SITES_PARAMETERS))
    ctxs = list(cmaker.genctxs([rup], sitecol, src_id=-1))
    n = len(sites)
    empty = [None] * n
    out_dist = {d: list(empty) for d in DISTANCES}
    results = {g: {i.string: {"mean_ln": list(empty), "sigma": list(empty), "tau": list(empty),
                              "phi": list(empty)} for i in imts} for g in gsim_names}
    if ctxs:
        ctx = ctxs[0]
        sids = [int(s) for s in ctx.sids]
        # Bağlam yalnız GSIM'lerin istediği mesafeleri taşır; dördü de motorun get_distances'iyle.
        for d in DISTANCES:
            values = get_distances(rup, sitecol, d)
            for sid in sids:
                out_dist[d][sid] = float(values[sid])
        try:
            arr = cmaker.get_mean_stds(ctxs, split_by_mag=False)  # (4, G, M, N)
        except KeyError as exc:  # check_support'un yakalamadığı ek katsayı tablosu
            _LOG.info("get_mean_stds KeyError: %s", exc)
            raise ComputeError("imt_not_supported", "Seçili GSIM'lerden biri bu IMT için tanımlı değil.") from None
        for g, gname in enumerate(gsim_names):
            for m, imt in enumerate(imts):
                block = results[gname][imt.string]
                for key, row in zip(("mean_ln", "sigma", "tau", "phi"), arr[:, g, m, :]):
                    for k, sid in enumerate(sids):
                        block[key][sid] = _finite(row[k])
    return {"magnitude": float(rup.mag), "rake": float(rup.rake), "distances": out_dist,
            "results": results}


def _finite(value: float) -> Optional[float]:
    value = float(value)
    return value if numpy.isfinite(value) else None
