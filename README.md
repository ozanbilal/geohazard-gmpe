# GeoHazard GMPE servisi

GeoHazard'ın (hazard.geoproje.com.tr) senaryo yer hareketi hesabını yapan küçük HTTP servisi.
Değiştirilmemiş [OpenQuake Engine](https://github.com/gem/oq-engine) 3.23.2 `hazardlib`'ini kullanır;
bu yüzden servis **AGPL-3.0-or-later** lisanslıdır (bkz. `LICENSE`). GeoHazard'ın kendisi bu servisi
yalnız HTTP üzerinden çağırır; OpenQuake kodu GeoHazard sürecine girmez.

## Ne yapar

`POST /v1/scenario` — bir `multiPlanesRupture` NRML XML'i (base64), saha listesi (`lon`, `lat`, `vs30`,
`vs30measured`, gerekirse `z1pt0`/`z2pt5`), GSIM adları ve IMT'ler alır. Kırılmayı motorun
`RuptureConverter`'ıyla okur, mesafeleri ve bağlamı motorun `ContextMaker`'ıyla kurar ve her
GSIM × IMT × saha için ln-ortalama ile σ, τ, φ döndürür. Rastgele yer hareketi alanı üretmez.

- `GET /health` — canlılık ve motor sürümü
- `GET /source` — lisans ve bu kaynak kodunun adresi (`GMPE_SOURCE_URL`)

Her `/v1/scenario` isteği `X-Service-Key` başlığı taşır; `GMPE_SERVICE_KEY` ortam değişkeniyle sabit
zamanlı karşılaştırılır. Anahtar tanımlı değilse uç 503 döner.

## Ortam değişkenleri

| Ad | Anlam |
|---|---|
| `GMPE_SERVICE_KEY` | GeoHazard ile paylaşılan anahtar (zorunlu, ≥ 32 karakter) |
| `GMPE_SOURCE_URL` | Bu reponun herkese açık adresi (`/source` yanıtında görünür) |
| `PORT`, `WEB_CONCURRENCY` | uvicorn portu ve işçi sayısı |

## Geliştirme

```bash
py -3.11 -m venv .venv
.venv/Scripts/python -m pip install -r https://raw.githubusercontent.com/gem/oq-engine/v3.23.2/requirements-py311-win64.txt
.venv/Scripts/python -m pip install --no-deps openquake.engine==3.23.2
.venv/Scripts/python -m pip install -r requirements-service.txt pytest httpx
.venv/Scripts/python -m pytest tests -q
```

Linux imajı `Dockerfile` ile GEM'in sabitlenmiş tekerlekleriyle (`requirements-oq-linux64.txt`) kurulur.

## Lisans

Bu servis AGPL-3.0-or-later'dır. OpenQuake Engine © GEM Foundation, AGPL-3.0.
