"""
Fase 2: avisos y convocatorias fuera del SEACE.

  - Obras por Impuestos (OxI): convocatorias de municipalidades, gobiernos regionales y
    ProInversión (no pasan por el SEACE).
  - ANIN, PEIP y programas especiales: acercamientos al mercado, comunicados y
    convocatorias G2G / NEC que salen en sus páginas de gob.pe antes del SEACE.

Fuentes (configurables en config.json -> "fuentes_avisos"):
  tipo "rss":    búsquedas de Google Noticias (cubren todo el país, traen fecha).
  tipo "pagina": listados de gob.pe u otros portales; se extraen los enlaces nuevos.

Regla anti-avalancha: la primera vez que se lee una fuente, lo encontrado se guarda en el
buscador pero NO se envía por correo (salvo lo publicado en los últimos días).
"""
import datetime as dt
import email.utils
import hashlib
import html
import re
import time
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import quote_plus, urljoin, urlsplit, urlunsplit

LIMA_TZ = dt.timezone(dt.timedelta(hours=-5))
MESES = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
         "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12}
RE_FECHA_ES = re.compile(r"(\d{1,2}) de (" + "|".join(MESES) + r") de (20\d\d)")
PATRON_GOBPE = r"gob\.pe/institucion/[^/]+/(?:noticias|informes-publicaciones|campa(?:%C3%B1|ñ)as|normas-legales)/\d+"


# ----------------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------------
class _Enlaces(HTMLParser):
    """Junta (href, texto) de todos los <a> de una página."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.enlaces, self._href, self._texto = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._texto = []

    def handle_data(self, data):
        if self._href is not None:
            self._texto.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            texto = re.sub(r"\s+", " ", "".join(self._texto)).strip()
            self.enlaces.append((self._href, texto))
            self._href = None


def texto_plano(contenido_html):
    t = re.sub(r"(?is)<(script|style|noscript|header|footer|nav)[^>]*>.*?</\1>", " ", contenido_html)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", html.unescape(t)).strip()


def url_canonica(url):
    p = urlsplit(url)
    if "news.google." in p.netloc:
        return url  # el enlace de Google Noticias es único tal cual
    return urlunsplit((p.scheme or "https", p.netloc.lower(), p.path.rstrip("/"), "", ""))


def id_de(url):
    return "aviso-" + hashlib.sha1(url_canonica(url).encode("utf-8")).hexdigest()[:16]


def fecha_es(texto):
    m = RE_FECHA_ES.search(texto.lower())
    if not m:
        return None
    try:
        return dt.datetime(int(m.group(3)), MESES[m.group(2)], int(m.group(1)), 12, tzinfo=LIMA_TZ)
    except ValueError:
        return None


def compilar(lista, normalizar):
    return [re.compile(r"(?<![a-z0-9])" + re.escape(normalizar(p)) + r"(?![a-z0-9])") for p in lista]


def coincide(texto_n, regexes):
    return any(r.search(texto_n) for r in regexes)


def fragmento(texto, regexes, normalizar, largo=320):
    """Trozo del texto alrededor de la primera palabra clave encontrada."""
    texto_n = normalizar(texto)
    for r in regexes:
        m = r.search(texto_n)
        if m:
            ini = max(0, m.start() - 120)
            return ("…" if ini else "") + texto[ini:ini + largo].strip() + "…"
    return texto[:largo].strip()


# ----------------------------------------------------------------------------
# Lectores de fuentes
# ----------------------------------------------------------------------------
def leer_rss(sesion, fuente):
    url = fuente.get("url") or ("https://news.google.com/rss/search?q=" + quote_plus(fuente["consulta"]) +
                                "&hl=es-419&gl=PE&ceid=PE:es-419")
    r = sesion.get(url, timeout=60)
    r.raise_for_status()
    raiz = ET.fromstring(r.content)
    items = []
    for it in raiz.iter("item"):
        titulo = (it.findtext("title") or "").strip()
        enlace = (it.findtext("link") or "").strip()
        medio = (it.findtext("source") or "").strip()
        if medio and titulo.endswith(" - " + medio):
            titulo = titulo[: -len(" - " + medio)]
        fecha = None
        if it.findtext("pubDate"):
            try:
                fecha = email.utils.parsedate_to_datetime(it.findtext("pubDate")).astimezone(LIMA_TZ)
            except (TypeError, ValueError):
                fecha = None
        descripcion = texto_plano(it.findtext("description") or "")
        if titulo and enlace:
            items.append({"titulo": titulo, "url": enlace, "fecha": fecha, "medio": medio,
                          "resumen": descripcion[:300]})
    return items


def leer_pagina(sesion, fuente):
    items, vistos = [], set()
    patron = re.compile(fuente.get("patron") or PATRON_GOBPE, re.I)
    for url in fuente.get("urls") or [fuente.get("url")]:
        r = sesion.get(url, timeout=60)
        if r.status_code != 200:
            print(f"    {url} -> HTTP {r.status_code}")
            continue
        r.encoding = r.encoding or "utf-8"
        p = _Enlaces()
        p.feed(r.text)
        n = 0
        for href, texto in p.enlaces:
            if not href or href.startswith(("#", "mailto:", "javascript:")):
                continue
            absoluta = urljoin(url, href)
            if not patron.search(absoluta) or len(texto) < 8:
                continue
            clave = url_canonica(absoluta)
            if clave in vistos:
                continue
            vistos.add(clave)
            items.append({"titulo": texto, "url": absoluta, "fecha": None, "medio": urlsplit(url).netloc,
                          "resumen": ""})
            n += 1
        print(f"    {url} -> {n} enlaces")
        time.sleep(0.5)
    return items


def leer_detalle(sesion, url):
    try:
        r = sesion.get(url, timeout=60)
        if r.status_code == 200:
            r.encoding = r.encoding or "utf-8"
            return texto_plano(r.text)[:20000]
    except Exception as e:  # noqa: BLE001
        print(f"    detalle falló {url[:70]}: {e}")
    return ""


# ----------------------------------------------------------------------------
# Proceso principal de avisos
# ----------------------------------------------------------------------------
def revisar(config, almacen, estado, sesion, normalizar, departamento_de, ahora):
    """Lee todas las fuentes, actualiza 'almacen' (dict id->aviso) y devuelve los avisos nuevos
    que corresponde enviar por correo."""
    claves_rx = compilar(config.get("palabras_clave_avisos", []), normalizar)
    excluir_rx = compilar(config.get("palabras_excluidas_avisos", []), normalizar)
    dias_correo = int(config.get("dias_primera_carga", 3))
    fuentes_vistas = set(estado.get("fuentes_avisos_vistas", []))
    titulos_existentes = {normalizar(a.get("titulo"))[:120] for a in almacen.values()}
    nuevos_para_correo, resumen = [], {}

    for fuente in config.get("fuentes_avisos", []):
        nombre = fuente.get("nombre", "fuente")
        if fuente.get("activa") is False:
            continue
        primera_vez = nombre not in fuentes_vistas
        try:
            items = leer_rss(sesion, fuente) if fuente.get("tipo") == "rss" else leer_pagina(sesion, fuente)
        except Exception as e:  # noqa: BLE001
            print(f"  [{nombre}] no se pudo leer: {str(e)[:200]}")
            resumen[nombre] = "error"
            continue
        agregados = 0
        for it in items:
            aid = id_de(it["url"])
            titulo_n = normalizar(it["titulo"])
            if aid in almacen or titulo_n[:120] in titulos_existentes:
                continue
            texto_n = normalizar(f"{it['titulo']} {it.get('resumen', '')}")
            if coincide(texto_n, excluir_rx):
                continue
            relevante = coincide(texto_n, claves_rx)
            detalle = ""
            # Títulos genéricos ("Comunicado") se revisan por dentro, salvo en la primera lectura
            if not relevante and fuente.get("revisar_detalle") and not primera_vez:
                detalle = leer_detalle(sesion, it["url"])
                relevante = coincide(normalizar(detalle), claves_rx) and not coincide(normalizar(detalle[:3000]), excluir_rx)
                time.sleep(0.5)
            if not relevante:
                continue
            fecha = it.get("fecha") or (fecha_es(detalle) if detalle else None)
            texto_completo = f"{it['titulo']} {it.get('resumen', '')} {detalle[:4000]}"
            aviso = {
                "id": aid,
                "fuente": nombre,
                "grupo": fuente.get("grupo", "Avisos"),
                "titulo": it["titulo"],
                "resumen": it.get("resumen") or (fragmento(detalle, claves_rx, normalizar) if detalle else ""),
                "medio": it.get("medio", ""),
                "departamento": departamento_de(normalizar(texto_completo)),
                "fecha": (fecha or ahora).isoformat(timespec="minutes"),
                "visto": ahora.isoformat(timespec="minutes"),
                "url": it["url"],
            }
            almacen[aid] = aviso
            titulos_existentes.add(titulo_n[:120])
            agregados += 1
            reciente = fecha is not None and fecha >= ahora - dt.timedelta(days=dias_correo)
            if not primera_vez or reciente:
                nuevos_para_correo.append(aviso)
        fuentes_vistas.add(nombre)
        resumen[nombre] = f"{len(items)} leídos, {agregados} nuevos"
        print(f"  [{nombre}] {resumen[nombre]}{' (primera lectura: sin correo)' if primera_vez else ''}")

    estado["fuentes_avisos_vistas"] = sorted(fuentes_vistas)
    # Historial de avisos: 180 días
    limite = ahora - dt.timedelta(days=int(config.get("dias_historial_avisos", 180)))
    for k in [k for k, a in almacen.items() if (_f(a.get("fecha")) or ahora) < limite]:
        del almacen[k]
    return nuevos_para_correo, resumen


def _f(valor):
    try:
        return dt.datetime.fromisoformat(valor) if valor else None
    except ValueError:
        return None


def html_correo(avisos):
    if not avisos:
        return ""
    e = html.escape
    filas = []
    for a in sorted(avisos, key=lambda x: (x["grupo"], x["fecha"]), reverse=False):
        lugar = f"📍 {e(a['departamento'])} · " if a.get("departamento") else ""
        filas.append(f"""
<tr><td style="padding:10px;border-bottom:1px solid #e5e7eb;font-family:Arial,sans-serif;font-size:14px">
  <div style="font-size:12px;color:#6b7280">{e(a['grupo'])} · {e(a.get('medio') or a['fuente'])}</div>
  <div style="font-weight:bold;margin:3px 0"><a href="{e(a['url'])}" style="color:#1d4ed8;text-decoration:none">{e(a['titulo'][:300])}</a></div>
  <div style="color:#374151">{lugar}📅 {e(a['fecha'][:10])}</div>
  <div style="font-size:12px;color:#6b7280">{e((a.get('resumen') or '')[:280])}</div>
</td></tr>""")
    return ('<h3 style="color:#7c3aed">📣 Obras por Impuestos, ANIN, PEIP y programas especiales</h3>'
            '<table style="border-collapse:collapse;width:100%">' + "".join(filas) + "</table>")
