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
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

import numpy
from openquake.hazardlib import imt as imt_module
from openquake.hazardlib import nrml, sourceconverter, valid
from openquake.hazardlib.calc.filters import get_distances
from openquake.hazardlib.contexts import MAXMAG, MINMAG, ContextMaker
from openquake.hazardlib.site import SiteCollection

MAX_SITES = 2000
MAX_GSIMS = 10
MAX_IMTS = 40
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
        except Exception as exc:  # motor ad/argüman hatasını çeşitli tiplerle bildirir
            raise ComputeError("unknown_gsim", f"GSIM tanınmadı: {name} ({exc})") from None
    return out


def parse_imts(names: Sequence[str]) -> List[object]:
    if not names or len(names) > MAX_IMTS:
        raise ComputeError("imts_count", f"1-{MAX_IMTS} IMT verilmeli.")
    out = []
    for name in names:
        try:
            out.append(imt_module.from_string(name))
        except Exception:
            raise ComputeError("unknown_imt", f"IMT tanınmadı: {name}") from None
    return out


def read_rupture(xml: bytes, mesh_spacing: float):
    if not (0.1 <= mesh_spacing <= 20.0):
        raise ComputeError("mesh_spacing_range", "rupture_mesh_spacing 0.1-20 km olmalı.")
    try:
        [node] = nrml.read(io.BytesIO(xml))
        rup = sourceconverter.RuptureConverter(mesh_spacing).convert_node(node)
    except Exception as exc:
        raise ComputeError("rupture_invalid", f"Kırılma XML'i okunamadı: {exc}") from None
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
        arr = cmaker.get_mean_stds(ctxs, split_by_mag=False)  # (4, G, M, N)
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
