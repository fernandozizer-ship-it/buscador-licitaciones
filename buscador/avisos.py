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
import base64
import binascii
import datetime as dt
import email.utils
import hashlib
import html
import json
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
    consulta = "&".join(x for x in p.query.split("&") if x and not x.lower().startswith(("utm_", "fbclid", "gclid")))
    return urlunsplit((p.scheme or "https", p.netloc.lower(), p.path.rstrip("/"), consulta, ""))


def id_de(url):
    return "aviso-" + hashlib.sha1(url_canonica(url).encode("utf-8")).hexdigest()[:16]


RE_LIMITE = re.compile(r"(?:presentacion|recepcion|entrega) de (?:las )?(?:propuestas|ofertas|expresion(?:es)? de interes|sobres)(.{0,140})")
RE_DMA = re.compile(r"(\d{1,2})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(20\d\d)")


def fecha_limite_de(texto_n, desde=None):
    """Fecha de presentación de propuestas / expresiones de interés mencionada en el texto."""
    for m in RE_LIMITE.finditer(texto_n):
        trozo = m.group(1)
        candidatas = []
        for d, mes, a in RE_DMA.findall(trozo):
            try:
                candidatas.append(dt.datetime(int(a), int(mes), int(d), 23, 59, tzinfo=LIMA_TZ))
            except ValueError:
                pass
        f = fecha_es(trozo)
        if f:
            candidatas.append(f.replace(hour=23, minute=59))
        if desde:
            candidatas = [c for c in candidatas if desde - dt.timedelta(days=2) <= c <= desde + dt.timedelta(days=200)]
        if candidatas:
            return min(candidatas)
    return None


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
    for espera in (30, 90):  # Google News responde 429 si recibe muchas consultas seguidas: reintentar
        if r.status_code != 429:
            break
        time.sleep(espera)
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


def leer_web(sesion, fuente):
    """Búsqueda web (incluye publicaciones de LinkedIn y páginas de empresas que el buscador
    tenga indexadas). Usa Bing en formato RSS; si no responde, DuckDuckGo en HTML."""
    consulta = fuente["consulta"]
    items = []
    try:
        url = ("https://www.bing.com/search?format=rss&count=30&setlang=es&cc=PE&q=" + quote_plus(consulta))
        r = sesion.get(url, timeout=60)
        if r.status_code == 200 and b"<rss" in r.content[:500]:
            raiz = ET.fromstring(r.content)
            for it in raiz.iter("item"):
                titulo = (it.findtext("title") or "").strip()
                enlace = (it.findtext("link") or "").strip()
                fecha = None
                if it.findtext("pubDate"):
                    try:
                        fecha = email.utils.parsedate_to_datetime(it.findtext("pubDate")).astimezone(LIMA_TZ)
                    except (TypeError, ValueError):
                        fecha = None
                if titulo and enlace:
                    items.append({"titulo": titulo, "url": enlace, "fecha": fecha, "medio": urlsplit(enlace).netloc,
                                  "resumen": texto_plano(it.findtext("description") or "")[:300], "_fecha_web": True})
    except Exception as e:  # noqa: BLE001
        print(f"    Bing no respondió: {str(e)[:120]}")
    if items and not resultados_relevantes(items, consulta):
        print("    Bing devolvió resultados sin relación con la búsqueda; se prueba DuckDuckGo")
        items = []
    if not items:
        try:
            r = sesion.get("https://html.duckduckgo.com/html/?kl=pe-es&q=" + quote_plus(consulta), timeout=60)
            if r.status_code != 200 or "result__a" not in r.text:
                print(f"    DuckDuckGo no entregó resultados (HTTP {r.status_code})")
            bloques = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</a>',
                                 r.text, re.S)
            for href, tit, snip in bloques:
                m = re.search(r"uddg=([^&]+)", href)
                enlace = requests_unquote(m.group(1)) if m else html.unescape(href)
                items.append({"titulo": texto_plano(tit), "url": enlace, "fecha": None,
                              "medio": urlsplit(enlace).netloc, "resumen": texto_plano(snip)[:300], "_fecha_web": True})
        except Exception as e:  # noqa: BLE001
            print(f"    DuckDuckGo no respondió: {str(e)[:120]}")
    return items


def resultados_relevantes(items, consulta):
    """True si al menos un resultado contiene alguna palabra significativa de la búsqueda."""
    palabras = {w.lower() for w in re.findall(r"[A-Za-zÁÉÍÓÚáéíóúñÑ]{6,}", re.sub(r"site:\S+", "", consulta))}
    palabras -= {"linkedin", "posts"}
    if not palabras:
        return True
    texto = " ".join(f"{i['titulo']} {i.get('resumen', '')}" for i in items).lower()
    return any(p in texto for p in palabras)


def requests_unquote(texto):
    from urllib.parse import unquote
    return unquote(texto)


MEDIOS_PRENSA = ("andina", "elperuano", "gestion", "elcomercio", "larepublica", "rpp", "infobae", "proactivo",
                 "energiminas", "rumbominero", "revistaeconomia", "correo", "peru21", "expreso", "semanaeconomica",
                 "mineriaenergia", "minasypetroleo", "construccionindustria", "news.google")


def procedencia_de(url, medio=""):
    dominio = urlsplit(url or "").netloc.lower().replace("www.", "")
    if not dominio:
        return medio or "Desconocida"
    if "linkedin.com" in dominio:
        return "LinkedIn"
    if "seace" in dominio or "oece.gob.pe" in dominio:
        return "SEACE (OECE)"
    if "investinperu" in dominio or "proinversion" in dominio:
        return "ProInversión (portal oficial)"
    if "petroperu" in dominio or "suplos" in dominio:
        return "Portal de Petroperú"
    if dominio.endswith("gob.pe"):
        return "gob.pe (portal del Estado)"
    if any(m in dominio for m in MEDIOS_PRENSA):
        return f"Prensa: {dominio}" if "news.google" not in dominio else f"Prensa: {medio or 'Google Noticias'}"
    if "facebook.com" in dominio:
        return "Facebook"
    return f"Web de la empresa: {dominio}"


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
            if not patron.search(absoluta) or len(texto) < 8 or len(texto) > 260 or "Ver documentos" in texto:
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


# ----------------------------------------------------------------------------
# ProInversión: lista oficial de procesos de selección OxI (web.investinperu.pe)
# ----------------------------------------------------------------------------
URL_PROINVERSION = "https://web.investinperu.pe/es/oxi/procesos-de-seleccion"


def _campo(bloque, etiqueta):
    m = re.search(r"<b>\s*" + etiqueta + r"\s*:?\s*</b>(.*?)</p>", bloque, re.S | re.I)
    return re.sub(r"\s+", " ", texto_plano(m.group(1))).strip() if m else ""


def leer_proinversion(sesion, fuente):
    """Lee las tarjetas de convocatorias OxI (nombre del proyecto, entidad, monto, tipo, fecha,
    departamento y estado). Intenta pedir más resultados por página; si el portal no lo acepta,
    se queda con la primera página (las más recientes)."""
    base = fuente.get("url") or URL_PROINVERSION
    r = sesion.get(base, timeout=90)  # abre la página (cookies de sesión) y sirve de respaldo
    html_paginas, vistas = [], set()
    por_pagina = int(fuente.get("por_pagina", 50))
    for pagina in range(1, int(fuente.get("paginas", 20)) + 1):
        filtro = {"Page": pagina, "AnoInicial": 0, "AnoFinal": 0, "MontoInicial": 0, "MontoFinal": 99999999999,
                  "TipologiaList": "", "EstadoConvocatoriaList": [], "TipoConvocatoriaList": [],
                  "PageNumberPaging": pagina, "PageSizePaging": por_pagina, "MaximumPageNumbersToDisplay": "3",
                  "NombreProyecto": "", "DepartamentoCodigo": "00", "ProvinciaCodigo": "0000",
                  "DistritoCodigo": "000000", "NivelGobierno": "0", "TipoProyecto": "0",
                  "CodigoSNIPInvierte": "", "IdSector": "0", "IdTipologia": "0"}
        try:
            rp = sesion.post(urljoin(base, "/es/oxi/FilterDetalleSeleccion"), data=json.dumps(filtro), timeout=90,
                             headers={"Content-Type": "application/json; charset=utf-8", "Referer": base,
                                      "X-Requested-With": "XMLHttpRequest"})
            n = rp.text.count('class="portafolio-card"') if rp.status_code == 200 else 0
            ids = set(re.findall(r"idConvocatoria=(\d+)", rp.text))
            if not n or (ids and ids <= vistas):
                print(f"    página {pagina}: sin convocatorias nuevas (HTTP {rp.status_code}); fin")
                break
            vistas |= ids
            html_paginas.append(rp.text)
        except Exception as e:  # noqa: BLE001
            print(f"    consulta interna falló: {str(e)[:150]}")
            break
        time.sleep(1)
    if not html_paginas and r.status_code == 200:
        html_paginas = [r.text]
    tarjetas = [t for h in html_paginas for t in h.split('class="portafolio-card"')[1:]]
    print(f"    {base} -> {len(html_paginas)} páginas, {len(tarjetas)} convocatorias en total")
    items = []
    for t in tarjetas:
        m = re.search(r'<a href="([^"]*FichaProyectoConvocatoria[^"]*)"[^>]*>(.*?)</a>', t, re.S)
        if not m:
            continue
        url = urljoin(base, html.unescape(m.group(1)))
        nombre = re.sub(r"\s+", " ", texto_plano(m.group(2))).strip()
        entidad = re.search(r'card-text-oxi-seleccion"><b>([^<]+)</b>', t)
        monto_txt = _campo(t, "Monto Convocatoria")
        tipo_conv = _campo(t, "Tipo de Convocatoria")
        intervencion = _campo(t, "Tipo de Intervenci(?:ó|&#243;|o)n")
        fecha_m = re.search(r"Fecha Convocatoria:\s*</b>\s*</span>\s*(\d{2}/\d{2}/\d{4})", t)
        dep = re.search(r"Departamento:\s*<span[^>]*>([^<]+)</span>", t)
        estado = re.search(r"Estado:\s*([^<]+)</span>", t)
        fecha = None
        if fecha_m:
            d, mth, y = fecha_m.group(1).split("/")
            fecha = dt.datetime(int(y), int(mth), int(d), 12, tzinfo=LIMA_TZ)
        monto = None
        mm = re.search(r"([\d,]+(?:\.\d+)?)", monto_txt.replace(" ", ""))
        if mm:
            try:
                monto = float(mm.group(1).replace(",", ""))
            except ValueError:
                monto = None
        estado_txt = estado.group(1).strip() if estado else ""
        items.append({
            "titulo": nombre, "url": url, "fecha": fecha, "medio": "ProInversión", "resumen": "",
            "_completo": {
                "obra": nombre[:1] + nombre[1:].lower() if nombre.isupper() else nombre,
                "entidad": html.unescape(entidad.group(1)).strip().title() if entidad else "",
                "monto": monto,
                "tipo": "Supervisión" if "supervisora" in tipo_conv.lower() else "Ejecución de obra (empresa privada financista)",
                "intervencion": intervencion,
                "departamento_txt": dep.group(1).strip() if dep else "",
                "estado_oxi": estado_txt,
                "clase": "Resultado / buena pro" if "adjudic" in estado_txt.lower() else (
                    "Convocatoria / proceso de selección" if "proceso" in estado_txt.lower() else "Documento de un proceso en curso"),
            },
        })
    return items


def leer_detalle(sesion, url, con_html=False):
    try:
        r = sesion.get(url, timeout=60)
        if r.status_code == 200:
            r.encoding = r.encoding or "utf-8"
            texto = texto_plano(r.text)[:20000]
            return (texto, r.text) if con_html else texto
    except Exception as e:  # noqa: BLE001
        print(f"    detalle falló {url[:70]}: {e}")
    return ("", "") if con_html else ""


RE_PDF_GOBPE = re.compile(r'href="(https://cdn\.www\.gob\.pe/uploads/document/file/[^"]+?\.pdf[^"]*)"', re.I)
RE_PDF_CUALQUIERA = re.compile(r'href="([^"]+?\.pdf(?:\?[^"]*)?)"', re.I)
RE_SUBPAGINA_GOBPE = re.compile(r'href="((?:https://www\.gob\.pe)?/institucion/[^"/]+/(?:informes-publicaciones|normas-legales)/\d+[^"]*)"', re.I)


def enlaces_pdf(html_pagina, base="https://www.gob.pe"):
    """Enlaces a PDF de una página de gob.pe (los del CDN primero)."""
    urls = [html.unescape(u) for u in RE_PDF_GOBPE.findall(html_pagina or "")]
    for u in RE_PDF_CUALQUIERA.findall(html_pagina or ""):
        u = html.unescape(u)
        urls.append(u if u.startswith("http") else base + (u if u.startswith("/") else "/" + u))
    return list(dict.fromkeys(urls))


def subpaginas_gobpe(html_pagina, propia="", maximo=3):
    """En colecciones y campañas de gob.pe, los PDF están dentro de cada publicación de la lista."""
    vistas = []
    for u in RE_SUBPAGINA_GOBPE.findall(html_pagina or ""):
        u = html.unescape(u).split("#")[0]
        u = u if u.startswith("http") else "https://www.gob.pe" + u
        if u.rstrip("/") != propia.rstrip("/") and u not in vistas:
            vistas.append(u)
    return vistas[:maximo]


def texto_de_pdfs(sesion, html_pagina, max_pdfs=2, max_paginas=4):
    """Lee las primeras páginas de los PDF adjuntos (bases, convocatoria) de una publicación de gob.pe."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    import io
    textos = []
    for url in enlaces_pdf(html_pagina)[:max_pdfs]:
        try:
            r = sesion.get(url, timeout=90)
            if r.status_code != 200 or len(r.content) > 15_000_000:
                continue
            lector = PdfReader(io.BytesIO(r.content))
            for pagina in lector.pages[:max_paginas]:
                textos.append(pagina.extract_text() or "")
        except Exception as e:  # noqa: BLE001
            print(f"    no se pudo leer el PDF {url[:70]}: {str(e)[:100]}")
    return re.sub(r"\s+", " ", " ".join(textos))[:15000]


# ----------------------------------------------------------------------------
# Enlaces de Google Noticias -> dirección real de la publicación
# ----------------------------------------------------------------------------
def resolver_google(sesion, url):
    """Convierte un enlace news.google.com/rss/articles/... en la dirección original."""
    if "news.google." not in (url or "") or "/articles/" not in url:
        return url
    gid = url.split("/articles/")[1].split("?")[0]
    # Formato antiguo: la dirección viene codificada dentro del identificador
    try:
        crudo = base64.urlsafe_b64decode(gid + "=" * (-len(gid) % 4))
        m = re.search(rb"https?://[\x21-\x7e]+", crudo)
        if m and b"AU_yqL" not in crudo[:8]:
            return m.group(0).decode("ascii", "ignore")
    except (ValueError, binascii.Error):
        pass
    # Formato nuevo: se pide la dirección a Google con la firma de la página del artículo
    try:
        r = sesion.get(f"https://news.google.com/rss/articles/{gid}", timeout=30)
        sg = re.search(r'data-n-a-sg="([^"]+)"', r.text)
        ts = re.search(r'data-n-a-ts="([^"]+)"', r.text)
        if not (sg and ts):
            return url
        carga = ('["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
                 '"X","X",1,[1,1,1],1,1,null,0,0,null,0],"' + gid + '",' + ts.group(1) + ',"' + sg.group(1) + '"]')
        cuerpo = "f.req=" + quote_plus(json.dumps([[["Fbv4je", carga, None, "generic"]]]))
        r2 = sesion.post("https://news.google.com/_/DotsSplashUi/data/batchexecute", data=cuerpo,
                         headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}, timeout=30)
        m = re.search(r'garturlres\\",\\"(https?://[^\\"]+)', r2.text)
        if m:
            return m.group(1)
    except Exception as e:  # noqa: BLE001
        print(f"    no se pudo resolver enlace de Google: {str(e)[:120]}")
    return url


# ----------------------------------------------------------------------------
# Extraer datos legibles de una publicación
# ----------------------------------------------------------------------------
_INICIOS_PIP = (r"MEJORAMIENTO|CREACI[OÓ]N|AMPLIACI[OÓ]N|RECUPERACI[OÓ]N|INSTALACI[OÓ]N|RENOVACI[OÓ]N|"
                r"CONSTRUCCI[OÓ]N|REHABILITACI[OÓ]N|REPARACI[OÓ]N|ADECUACI[OÓ]N|OPTIMIZACI[OÓ]N|"
                r"REMODELACI[OÓ]N|REMODELACION|ELABORACI[OÓ]N DEL EXPEDIENTE")
RE_OBRA_COMILLAS = re.compile(r"[“\"«¿]\s*((?:" + _INICIOS_PIP + r")\b[^”\"»?]{15,400})[”\"»?]", re.I)
RE_OBRA_LIBRE = re.compile(r"((?:" + _INICIOS_PIP + r")\s+(?:DEL?|DE LOS|DE LAS|Y|EN)\b.{15,320}?)"
                           r"(?=\s*,?\s*(?:con\s+)?(?:CUI|C[OÓ]DIGO)\b|\s+bajo\b|\s+mediante\b|\.\s|\s+-\s|$)", re.I)
RE_CUI = re.compile(r"(?:CUI|C[OÓ]DIGO [UÚ]NICO DE INVERSIONES?)\s*(?:N[°º.o]*\s*)?:?\s*(\d{6,7})", re.I)
RE_MONTO = re.compile(r"S/\.?\s?(\d{1,3}(?:[ .,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d+)?)\s*(millones|mill\.?|MM)?", re.I)
RE_ENTIDAD = re.compile(r"((?:Municipalidad|MUNICIPALIDAD) (?:Provincial|Distrital|PROVINCIAL|DISTRITAL)(?: de| DE)? [A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ]+(?: [A-ZÁÉÍÓÚÑa-záéíóúñ][\wÁÉÍÓÚÑáéíóúñ]+){0,3}"
                        r"|(?:Gobierno|GOBIERNO) (?:Regional|REGIONAL)(?: de| DE| del| DEL)? [A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ]+(?: [A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ]+)?"
                        r"|(?:Universidad|UNIVERSIDAD) (?:Nacional|NACIONAL)(?: [A-ZÁÉÍÓÚÑa-z][\wÁÉÍÓÚÑáéíóúñ]+){1,5})")


def _monto(texto):
    for m in RE_MONTO.finditer(texto):
        num = m.group(1).replace(" ", "")
        # 1.234.567,89 / 1,234,567.89 / 12.5
        if re.search(r"[.,]\d{1,2}$", num) and len(re.findall(r"[.,]", num)) >= 1 and not re.search(r"[.,]\d{3}$", num):
            entero, dec = num[:-3] if num[-3] in ".," else num[:-2], num[-2:]
            num = re.sub(r"[.,]", "", entero) + "." + dec.strip(".,")
        else:
            num = re.sub(r"[.,](?=\d{3}(\D|$))", "", num).replace(",", ".")
        try:
            valor = float(num)
        except ValueError:
            continue
        if m.group(2):
            valor *= 1_000_000
        if valor >= 10_000:
            return round(valor, 2)
    return None


RE_SUBCONTRATO = re.compile(r"subcontrat|sub contrat|paquetes? de partidas|invitacion a cotizar|invitamos a cotizar|"
                            r"solicitamos cotizacion|buscamos (empresas|contratistas|proveedores)|requerimos (empresas|contratistas)|"
                            r"registro de (proveedores|contratistas|subcontratistas)|convocatoria (a|de) (proveedores|contratistas)|"
                            r"proveedores locales|empresas comunales|desarrollo de proveedores|nuevos proveedores|"
                            r"(busca|buscan|convoca|convocan|invita|invitan|requiere|requieren)[a-z ]{0,30}(proveedores|contratistas|empresas (de|contratistas|locales))")


def clasificar(titulo_n, texto_n, es_nota):
    if RE_SUBCONTRATO.search(titulo_n + " " + texto_n[:1500]) and not re.search(r"adjudic|buena pro", titulo_n):
        return "Búsqueda de subcontratistas / proveedores"
    if re.search(r"contratacion futura|aviso de contratacion", titulo_n + " " + texto_n[:800]):
        return "Acercamiento al mercado"
    if re.search(r"acercamiento al mercado|consulta al mercado|interaccion con el mercado|difusion del requerimiento|contratacion futura", titulo_n + " " + texto_n[:1500]):
        return "Acercamiento al mercado"
    if re.search(r"buena pro|adjudicacion|adjudico|adjudica|acta de evaluacion|resultado|ganador", titulo_n):
        return "Resultado / buena pro"
    if re.search(r"circular|absolucion|acta de recepcion|bases integradas|cronograma|fe de erratas|modificacion|prorroga|comunicado n", titulo_n):
        return "Documento de un proceso en curso"
    if re.search(r"proceso de seleccion|convocatoria|bases|expresion(es)? de interes|licitacion|concurso publico|convoca", titulo_n):
        return "Nota sobre una convocatoria" if es_nota and not re.search(r"proceso de seleccion n|bases", titulo_n) else "Convocatoria / proceso de selección"
    return "Nota de prensa"


def tipo_contratacion(texto_n):
    if re.search(r"entidad privada supervisora|empresa privada supervisora|(?<![a-z])eps(?![a-z])|supervision de (la )?(obra|ejecucion)", texto_n):
        if re.search(r"empresa privada (financista|que financia|para el financiamiento)|financiamiento y ejecucion", texto_n):
            return "Ejecución y supervisión"
        return "Supervisión"
    if re.search(r"expediente tecnico|estudio definitivo", texto_n) and not re.search(r"ejecucion de (la )?obra", texto_n):
        return "Expediente técnico"
    if re.search(r"empresa privada|financiamiento y ejecucion|ejecucion de (la )?obra|ejecucion del proyecto|construccion", texto_n):
        return "Ejecución de obra"
    return None


VERSION_PDF = 2
URL_FICHA_CUI = "https://ofi5.mef.gob.pe/ssi/"  # Seguimiento de Inversiones del MEF: se busca por CUI


def enriquecer(sesion, aviso, normalizar, departamento_de, especialidades_rx, contexto_rx=None, excluir_rx=None):
    """Abre la publicación y completa: clase, obra, tipo, especialidad, entidad, monto, CUI y fecha."""
    url_real = resolver_google(sesion, aviso["url"])
    if url_real != aviso["url"]:
        aviso["url_original"] = url_real
    aviso["procedencia"] = procedencia_de(url_real, aviso.get("medio"))
    detalle, html_det = leer_detalle(sesion, url_real, con_html=True) if not url_real.lower().endswith(".pdf") else ("", "")
    titulo = aviso.get("titulo", "")
    texto = f"{titulo}. {aviso.get('resumen', '')} {detalle[:12000]}"
    if "gob.pe" in url_real and not (RE_OBRA_COMILLAS.search(texto) or RE_OBRA_LIBRE.search(texto)):
        # Bases y convocatorias: el nombre del proyecto suele estar solo en el PDF adjunto
        html_con_pdfs = html_det
        if not enlaces_pdf(html_det):
            for sub in subpaginas_gobpe(html_det, url_real):
                _, html_sub = leer_detalle(sesion, sub, con_html=True)
                html_con_pdfs += html_sub
                if enlaces_pdf(html_sub):
                    break
        texto_pdf = texto_de_pdfs(sesion, html_con_pdfs)
        aviso["pdf_revisado"] = VERSION_PDF
        print(f"    PDF: {len(enlaces_pdf(html_con_pdfs))} adjuntos, {len(texto_pdf)} caracteres leídos | {titulo[:70]}")
        if texto_pdf:
            texto += " " + texto_pdf
    texto_n = normalizar(texto)
    titulo_n = normalizar(titulo)
    if detalle and contexto_rx and not aviso.get("sin_contexto_ok") and not coincide(normalizar(f"{titulo} {detalle[:8000]}"), contexto_rx):
        aviso["descartar"] = "no es de obra"
    if detalle and excluir_rx and coincide(normalizar(f"{titulo} {detalle[:2000]}"), excluir_rx):
        aviso["descartar"] = "excluido"
    aviso.pop("pendiente_contexto", None)
    es_nota = "/noticias/" in url_real or ("gob.pe" not in url_real and "investinperu" not in url_real)
    aviso["clase"] = clasificar(titulo_n, texto_n, es_nota)
    limite = fecha_limite_de(texto_n, _f(aviso.get("fecha")))
    if limite and aviso["clase"] not in ("Resultado / buena pro", "Nota de prensa"):
        aviso["fecha_limite"] = limite.isoformat(timespec="minutes")
    m = RE_OBRA_COMILLAS.search(texto) or RE_OBRA_LIBRE.search(texto)
    if m:
        obra = re.sub(r"\s+", " ", m.group(1)).strip(" ,.;:-")
        aviso["obra"] = obra[:1].upper() + obra[1:].lower() if obra.isupper() else obra
    m = RE_CUI.search(texto)
    if m:
        aviso["cui"] = m.group(1)
    monto = _monto(texto)
    if monto:
        aviso["monto"] = monto
    aviso["tipo"] = tipo_contratacion(texto_n)
    esp_texto = normalizar(aviso.get("obra") or titulo)
    aviso["especialidades"] = [n for n, rxs in especialidades_rx.items() if any(r.search(esp_texto) for r in rxs)]
    m = RE_ENTIDAD.search(detalle[:6000] or texto)
    if m:
        aviso["entidad"] = re.sub(r"\s+", " ", m.group(1)).strip()
    if not aviso.get("fecha_real"):
        f = fecha_es(detalle[:4000]) if detalle else None
        if f:
            aviso["fecha"], aviso["fecha_real"] = f.isoformat(timespec="minutes"), True
    if detalle and not aviso.get("resumen"):
        aviso["resumen"] = detalle[:300]
    pistas = pistas_de(url_real, titulo, aviso.get("entidad") or aviso.get("medio"))
    if aviso.get("entidad"):
        pistas.insert(0, aviso["entidad"])
    aviso["departamento"] = departamento_de(normalizar(f"{titulo} {aviso.get('obra', '')} {aviso.get('entidad', '')}"),
                                            pistas) or aviso.get("departamento")
    aviso["enriquecido"] = bool(detalle) or url_real == aviso["url"]
    return aviso


def volcar_diagnostico(sesion, urls, carpeta):
    """Guarda copias de páginas para poder armar lectores específicos (p. ej. ProInversión)."""
    import os
    os.makedirs(carpeta, exist_ok=True)
    for i, url in enumerate(urls, 1):
        try:
            r = sesion.get(url, timeout=60)
            nombre = os.path.join(carpeta, f"pagina_{i}.html")
            with open(nombre, "w", encoding="utf-8") as fh:
                fh.write(f"<!-- {url} | HTTP {r.status_code} | {r.headers.get('content-type')} -->\n")
                fh.write(r.text[:600000])
            print(f"    diagnóstico guardado: {url} -> HTTP {r.status_code}, {len(r.text)} caracteres")
        except Exception as e:  # noqa: BLE001
            print(f"    diagnóstico falló {url}: {e}")


# ----------------------------------------------------------------------------
# Proceso principal de avisos
# ----------------------------------------------------------------------------
def sector_por_grupo(grupo):
    g = (grupo or "").lower()
    if "impuestos" in g:
        return "Público con financiamiento privado (OxI)"
    if "petroper" in g:
        return "Empresa estatal"
    if any(x in g for x in ("miner", "subcontrat", "privad", "constructora")):
        return "Privado"
    return "Público"


RE_SLUG_GOBPE = re.compile(r"gob\.pe/institucion/([a-z0-9\-]+)/", re.I)


def pistas_de(url, titulo, medio=""):
    """Pistas para ubicar la región: la entidad dueña de la página en gob.pe y el título."""
    pistas = []
    m = RE_SLUG_GOBPE.search(url or "")
    if m:
        slug = m.group(1).lower()
        slug = re.sub(r"^(gobierno-regional-de-|gobierno-regional-|region|gore-?|gr-?|municipalidad-?(provincial-|distrital-)?(de-)?|muni|mp|md)", "", slug)
        pistas.append(slug.replace("-", " "))
    pistas += [titulo or "", medio or ""]
    return pistas


# ----------------------------------------------------------------------------
# Convocatorias encontradas en LinkedIn (barrido a pedido, archivo buscador/linkedin.json)
# ----------------------------------------------------------------------------
UNIDADES_LINKEDIN = {"min": 1 / 1440, "h": 1 / 24, "d": 1, "dia": 1, "dias": 1, "sem": 7, "semana": 7, "semanas": 7,
                     "mes": 30, "meses": 30, "ano": 365, "anos": 365, "año": 365, "años": 365}


def fecha_desde_relativa(hace, barrido):
    """'4 sem' leído el 2026-11-16 -> 2026-10-19 (LinkedIn solo muestra el tiempo transcurrido)."""
    m = re.match(r"\s*(\d+)\s*([a-zñ]+)", (hace or "").lower())
    if not m or m.group(2) not in UNIDADES_LINKEDIN:
        return None
    return barrido - dt.timedelta(days=int(m.group(1)) * UNIDADES_LINKEDIN[m.group(2)])


def fecha_de_id_linkedin(url, tz):
    """El número de una publicación de LinkedIn (share/activity/ugcPost) lleva la hora en que se publicó."""
    m = re.search(r"(?:share|activity|ugcPost)[-:](\d{18,20})", url or "")
    if not m:
        return None
    try:
        return dt.datetime.fromtimestamp((int(m.group(1)) >> 22) / 1000, tz)
    except (ValueError, OverflowError, OSError):
        return None


def cargar_linkedin(ruta, almacen, ahora, config, normalizar, departamento_de):
    """Agrega al buscador las convocatorias del archivo linkedin.json. Devuelve las nuevas."""
    import os
    if not os.path.exists(ruta):
        return []
    try:
        with open(ruta, encoding="utf-8") as f:
            datos = json.load(f)
    except (OSError, ValueError) as e:
        print(f"  [LinkedIn] no se pudo leer linkedin.json: {e}")
        return []
    nuevos = []
    for it in datos.get("convocatorias", []):
        aid = "linkedin-" + hashlib.sha1(f"{it.get('autor_url')}|{it.get('titulo')}".encode("utf-8")).hexdigest()[:16]
        barrido = dt.datetime.fromisoformat(it.get("barrido") or datos.get("barrido"))
        if barrido.tzinfo is None:
            barrido = barrido.replace(tzinfo=ahora.tzinfo)
        exacta = fecha_de_id_linkedin(it.get("url_publicacion"), ahora.tzinfo)
        if it.get("fecha_publicacion"):
            exacta = dt.datetime.fromisoformat(it["fecha_publicacion"]).replace(tzinfo=ahora.tzinfo)
        publicado = exacta or fecha_desde_relativa(it.get("hace"), barrido)
        texto_n = normalizar(f"{it.get('titulo', '')} {it.get('resumen', '')} {it.get('lugar', '')}")
        aviso = {
            "id": aid, "origen": "linkedin", "fuente": "LinkedIn (barrido)", "medio": "LinkedIn",
            "grupo": it.get("grupo", "Subcontratos de constructoras"),
            "clase": "Búsqueda de subcontratistas / proveedores",
            "sector": it.get("sector", "Privado"), "procedencia": "LinkedIn",
            "titulo": it["titulo"], "obra": it.get("obra"), "resumen": it.get("resumen", ""),
            "empresa": it.get("empresa"), "publicado_por": it.get("publicado_por"), "contacto": it.get("contacto"),
            "lugar": it.get("lugar"), "partidas": it.get("partidas"),
            "departamento": it.get("departamento") or departamento_de(texto_n, [normalizar(it.get("lugar", ""))]),
            "fecha": (publicado or barrido).isoformat(timespec="minutes"), "fecha_real": publicado is not None,
            "fecha_aproximada": exacta is None, "hace_linkedin": it.get("hace"),
            "visto": barrido.isoformat(timespec="minutes"),
            "url": it.get("url_publicacion") or it.get("autor_url"), "url_autor": it.get("autor_url"),
            "enriquecido": True,
        }
        aviso["especialidades"] = [n for n, k in config.get("especialidades", {}).items()
                                   if coincide(texto_n, compilar(k, normalizar))]
        if aid not in almacen:
            nuevos.append(aviso)
        else:
            aviso["visto"] = almacen[aid].get("visto", aviso["visto"])
        almacen[aid] = aviso
    print(f"  [LinkedIn (barrido)] {len(datos.get('convocatorias', []))} en el archivo, {len(nuevos)} nuevos")
    return nuevos


def revisar(config, almacen, estado, sesion, normalizar, departamento_de, ahora):
    """Lee todas las fuentes, actualiza 'almacen' (dict id->aviso) y devuelve los avisos nuevos
    que corresponde enviar por correo."""
    claves_rx = compilar(config.get("palabras_clave_avisos", []), normalizar)
    excluir_rx = compilar(config.get("palabras_excluidas_avisos", []), normalizar)
    contexto_rx = compilar(config.get("palabras_contexto_obra", []), normalizar)
    excluir_detalle_rx = compilar(config.get("palabras_excluidas_en_detalle", []), normalizar)

    def tiene_contexto(texto_n):
        return coincide(texto_n, contexto_rx) if contexto_rx else True
    # Limpieza de lo ya guardado con las reglas actuales (región y exclusiones)
    excl_por_fuente = {f["nombre"]: compilar(f["palabras_excluidas"], normalizar)
                       for f in config.get("fuentes_avisos", []) if f.get("palabras_excluidas")}
    for k in list(almacen):
        a = almacen[k]
        if a.get("origen") == "linkedin":
            continue  # revisadas a mano en el barrido: no se filtran de nuevo
        if a.get("fuente") in excl_por_fuente and coincide(normalizar(f"{a.get('titulo', '')} {a.get('resumen', '')}"), excl_por_fuente[a["fuente"]]):
            del almacen[k]
            continue
        if coincide(normalizar(f"{a.get('titulo', '')} {a.get('resumen', '')}"), excluir_rx):
            del almacen[k]
            continue
        if not tiene_contexto(normalizar(f"{a.get('titulo', '')} {a.get('resumen', '')} {a.get('obra', '')}")):
            if a.get("enriquecido"):
                del almacen[k]  # ya se revisó por dentro y no es de obra
                continue
            a["pendiente_contexto"] = True  # se verificará al abrir el detalle
        a["grupo"] = config.get("renombrar_grupos", {}).get(a.get("grupo"), a.get("grupo"))
        a.setdefault("sector", sector_por_grupo(a.get("grupo", "")))
        a.setdefault("procedencia", procedencia_de(a.get("url_original") or a.get("url"), a.get("medio")))
        if not a.get("departamento"):
            a["departamento"] = departamento_de(normalizar(f"{a.get('titulo', '')} {a.get('resumen', '')}"),
                                                pistas_de(a.get("url"), a.get("titulo"), a.get("medio")))
    dias_correo = int(config.get("dias_primera_carga", 3))
    dias_max = int(config.get("dias_maximos_aviso_correo", 7))

    def correo_ok(fecha, primera_vez):
        """Primera lectura de una fuente: solo lo de los últimos días. Después: todo lo nuevo,
        salvo lo que trae una fecha de publicación antigua (historial que recién aparece)."""
        if fecha is None:
            return not primera_vez
        if fecha >= ahora - dt.timedelta(days=dias_correo):
            return True
        return not primera_vez and fecha >= ahora - dt.timedelta(days=dias_max)
    fuentes_vistas = set(estado.get("fuentes_avisos_vistas", []))
    rechazados = set(estado.get("avisos_rechazados", []))
    if estado.get("version_filtros") != config.get("version_filtros"):
        rechazados = set()  # las reglas cambiaron: se vuelve a evaluar lo descartado
        estado["version_filtros"] = config.get("version_filtros")
    # Fuentes cuyo lector cambió: su próxima lectura vuelve a ser "primera" (sin avalancha de correo)
    ya = set(estado.get("fuentes_reiniciadas", []))
    for nombre_r in config.get("reiniciar_primera_lectura", []):
        if nombre_r not in ya:
            fuentes_vistas.discard(nombre_r)
            ya.add(nombre_r)
    estado["fuentes_reiniciadas"] = sorted(ya)
    titulos_existentes = {normalizar(a.get("titulo"))[:120] for a in almacen.values()}
    nuevos_para_correo, resumen = [], {}

    for fuente in config.get("fuentes_avisos", []):
        nombre = fuente.get("nombre", "fuente")
        if fuente.get("activa") is False:
            continue
        primera_vez = nombre not in fuentes_vistas
        claves_fuente = compilar(fuente["palabras_clave"], normalizar) if fuente.get("palabras_clave") else claves_rx
        depurar = nombre in config.get("depurar_fuentes", [])
        try:
            if fuente.get("tipo") == "rss":
                if not fuente.get("url"):
                    time.sleep(float(config.get("pausa_google_segundos", 4)))  # Google News limita consultas seguidas
                items = leer_rss(sesion, fuente)
            elif fuente.get("tipo") == "web":
                items = leer_web(sesion, fuente)
            elif fuente.get("tipo") == "proinversion":
                items = leer_proinversion(sesion, fuente)
            else:
                items = leer_pagina(sesion, fuente)
        except Exception as e:  # noqa: BLE001
            print(f"  [{nombre}] no se pudo leer: {str(e)[:200]}")
            resumen[nombre] = "error"
            continue
        agregados = 0
        for it in items:
            aid = id_de(it["url"])
            titulo_n = normalizar(it["titulo"])
            completo = it.get("_completo")
            if completo and aid in almacen:
                almacen[aid].update({"clase": completo["clase"], "estado_oxi": completo["estado_oxi"]})
                continue
            if aid in almacen or (titulo_n[:120] in titulos_existentes and not completo):
                continue
            if completo:
                fecha = it.get("fecha")
                dep_txt = normalizar(completo.pop("departamento_txt"))
                aviso = {"id": aid, "fuente": nombre, "grupo": fuente.get("grupo", "Obras por Impuestos"),
                         "sector": "Público con financiamiento privado (OxI)", "procedencia": "ProInversión (portal oficial)",
                         "titulo": it["titulo"], "resumen": completo.get("intervencion", ""), "medio": "ProInversión",
                         "departamento": departamento_de(dep_txt, [dep_txt]) if dep_txt else None,
                         "fecha": (fecha or ahora).isoformat(timespec="minutes"), "fecha_real": fecha is not None,
                         "visto": ahora.isoformat(timespec="minutes"), "url": it["url"], "enriquecido": True, **completo}
                esp_n = normalizar(it["titulo"])
                aviso["especialidades"] = [n for n, k in config.get("especialidades", {}).items()
                                           if coincide(esp_n, compilar(k, normalizar))]
                almacen[aid] = aviso
                titulos_existentes.add(titulo_n[:120])
                agregados += 1
                if correo_ok(fecha, primera_vez) and aviso["clase"] != "Resultado / buena pro":
                    nuevos_para_correo.append(aviso)
                continue
            texto_n = normalizar(f"{it['titulo']} {it.get('resumen', '')}")
            excluir_fuente = fuente.get("palabras_excluidas")
            if coincide(texto_n, excluir_rx) or (excluir_fuente and coincide(texto_n, compilar(excluir_fuente, normalizar))):
                if nombre in config.get("depurar_fuentes", []):
                    print(f"      · FUERA  (palabra excluida en título) | {it['titulo'][:90]}")
                continue
            relevante = coincide(texto_n, claves_fuente)
            con_contexto = tiene_contexto(texto_n) or fuente.get("requiere_contexto") is False
            detalle = ""
            if aid in rechazados:
                continue
            # Se abre la publicación cuando el título no dice si es de obra; los títulos genéricos
            # ("Comunicado") solo se abren en páginas oficiales (no en noticias ni buscadores)
            generico_ok = fuente.get("tipo") == "pagina" and not primera_vez
            if fuente.get("revisar_detalle") and ((relevante and not con_contexto) or (not relevante and generico_ok)):
                detalle = leer_detalle(sesion, resolver_google(sesion, it["url"]))
                detalle_n = normalizar(detalle[:8000])
                relevante = relevante or coincide(detalle_n, claves_fuente)
                con_contexto = con_contexto or tiene_contexto(detalle_n)
                if coincide(normalizar(detalle[:2500]), excluir_detalle_rx):
                    relevante = False
                time.sleep(0.5)
            if depurar:
                print(f"      · {'ENTRA ' if relevante and con_contexto else 'FUERA '} clave={relevante} obra={con_contexto} "
                      f"detalle={len(detalle)}c | {it['titulo'][:90]}")
            if not (relevante and con_contexto):
                if detalle:
                    rechazados.add(aid)  # ya se revisó por dentro: no volver a abrirlo
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
                "departamento": departamento_de(normalizar(texto_completo),
                                                pistas_de(it["url"], it["titulo"], it.get("medio"))),
                "fecha": (fecha or ahora).isoformat(timespec="minutes"),
                "fecha_real": fecha is not None,
                "visto": ahora.isoformat(timespec="minutes"),
                "url": it["url"],
                "sector": fuente.get("sector") or sector_por_grupo(fuente.get("grupo", "")),
                "procedencia": procedencia_de(it["url"], it.get("medio")),
            }
            if fuente.get("empresa"):
                aviso["empresa"] = fuente["empresa"]
            if fuente.get("requiere_contexto") is False:
                aviso["sin_contexto_ok"] = True
            m_li = re.match(r"^(.{3,80}?) (?:en|on) LinkedIn", it["titulo"])
            if m_li:
                aviso["empresa"] = m_li.group(1).strip()
            almacen[aid] = aviso
            titulos_existentes.add(titulo_n[:120])
            agregados += 1
            if correo_ok(fecha, primera_vez):
                nuevos_para_correo.append(aviso)
        fuentes_vistas.add(nombre)
        resumen[nombre] = f"{len(items)} leídos, {agregados} nuevos"
        print(f"  [{nombre}] {resumen[nombre]}{' (primera lectura: sin correo)' if primera_vez else ''}")

    import os
    ruta_li = config.get("archivo_linkedin") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "linkedin.json")
    for aviso in cargar_linkedin(ruta_li, almacen, ahora, config, normalizar, departamento_de):
        if correo_ok(_f(aviso["fecha"]) if aviso.get("fecha_real") else None, False):
            nuevos_para_correo.append(aviso)
    estado["fuentes_avisos_vistas"] = sorted(fuentes_vistas)
    estado["avisos_rechazados"] = sorted(rechazados)[-4000:]

    # Completar datos legibles (obra, tipo, monto, CUI, fecha); primero lo que va al correo
    especialidades_rx = {n: compilar(k, normalizar) for n, k in config.get("especialidades", {}).items()}
    pendientes = [a for a in nuevos_para_correo if not a.get("enriquecido")]
    pendientes += [a for a in almacen.values() if a.get("pendiente_contexto") and a not in pendientes]
    pendientes += [a for a in almacen.values() if not a.get("enriquecido") and a not in pendientes]
    pendientes += [a for a in almacen.values() if not a.get("obra") and a.get("pdf_revisado") != VERSION_PDF
                   and "gob.pe" in (a.get("url_original") or a.get("url") or "") and a not in pendientes]
    tope = int(config.get("max_avisos_a_completar_por_ejecucion", 80))
    for a in pendientes[:tope]:
        if a.get("fecha_real") is None:
            a["fecha_real"] = a.get("fecha") != a.get("visto")
        try:
            enriquecer(sesion, a, normalizar, departamento_de, especialidades_rx, contexto_rx, excluir_detalle_rx)
        except Exception as e:  # noqa: BLE001
            print(f"    no se pudo completar {a['url'][:70]}: {str(e)[:120]}")
        time.sleep(0.4)
    if pendientes:
        print(f"  Avisos completados con detalle: {min(len(pendientes), tope)} de {len(pendientes)} pendientes")
    # Obras financiadas por empresas privadas (Camisea, mineras, industria): qué empresa y cómo participar
    empresas = [(e, re.compile(e["patron"])) for e in config.get("empresas_privadas", [])]
    for a in almacen.values():
        if a.get("origen") == "linkedin":
            continue
        texto_a = normalizar(f"{a.get('titulo', '')} {a.get('resumen', '')} {a.get('obra') or ''} {a.get('empresa') or ''}")
        hallada = next((e for e, rx in empresas if rx.search(texto_a)), None)
        if hallada and (a.get("sector") == "Privado" or "privad" in (a.get("grupo") or "").lower()):
            a["empresa_privada"] = hallada["nombre"]
            a["como_postular"] = hallada["como_postular"]
        if "privad" in (a.get("grupo") or "").lower():
            if not hallada and not re.search(r"empresa|consorcio|minera|aporte|financiad|financiara|privad|donac", texto_a):
                a["sector"] = "Público"  # anuncio de obra pública (municipio, región): se licitará en el SEACE
            elif a.get("clase") in ("Nota de prensa", "Nota sobre una convocatoria"):
                a["clase"] = "Anuncio de obra privada"
    # Sin nombre de obra: completar con el de otro aviso del mismo CUI, y siempre dejar la ficha del MEF
    obra_por_cui = {a["cui"]: a["obra"] for a in almacen.values() if a.get("cui") and a.get("obra")}
    for a in almacen.values():
        if a.get("cui"):
            a["ficha_cui"] = URL_FICHA_CUI.format(cui=a["cui"])
            if not a.get("obra") and a["cui"] in obra_por_cui:
                a["obra"] = obra_por_cui[a["cui"]]
    # Tras abrir el detalle, descartar lo que resultó no ser contratación de obra
    descartados = [k for k, a in almacen.items() if a.get("origen") != "linkedin" and (a.get("descartar") or (
        a.get("enriquecido") and coincide(normalizar(f"{a.get('titulo')} {a.get('obra', '')}"), excluir_rx)))]
    for k in descartados:
        del almacen[k]
    if descartados:
        print(f"  Descartados tras revisar su contenido (no eran de obra): {len(descartados)}")
    nuevos_para_correo = [a for a in nuevos_para_correo
                          if a["id"] in almacen and a.get("clase") != "Resultado / buena pro"]

    if config.get("diagnostico_urls"):
        import os
        volcar_diagnostico(sesion, config["diagnostico_urls"],
                           os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "diagnostico"))
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


COLORES_CLASE = {"Búsqueda de subcontratistas / proveedores": "#7c3aed", "Convocatoria / proceso de selección": "#15803d", "Documento de un proceso en curso": "#1d4ed8",
                 "Acercamiento al mercado": "#b45309", "Anuncio de obra privada": "#0e7490", "Resultado / buena pro": "#b91c1c",
                 "Nota sobre una convocatoria": "#0f766e", "Nota de prensa": "#6b7280"}
ORDEN_CLASE = list(COLORES_CLASE)


def fecha_legible(a):
    if a.get("origen") == "linkedin":
        f = (a.get("fecha") or "")[:10]
        if a.get("fecha_aproximada") and a.get("hace_linkedin"):
            return f"Publicado en LinkedIn hace {a['hace_linkedin']} (≈ {f}, visto el {(a.get('visto') or '')[:10]})"
        return f"Publicado en LinkedIn el {f}"
    if a.get("fecha_real") is False or (a.get("fecha_real") is None and a.get("fecha") == a.get("visto")):
        return "Fecha no indicada (detectado " + (a.get("visto") or "")[:10] + ")"
    return (a.get("fecha") or "")[:10]


def html_correo(avisos):
    if not avisos:
        return ""
    e = html.escape
    filas = []
    for a in sorted(avisos, key=lambda x: (ORDEN_CLASE.index(x.get("clase")) if x.get("clase") in ORDEN_CLASE else 9,
                                           x.get("fecha") or "")):
        clase = a.get("clase") or "Aviso"
        color = COLORES_CLASE.get(clase, "#6b7280")
        nombre = a.get("obra") or a["titulo"]
        datos = [x for x in (a.get("sector"), a.get("procedencia"), a.get("tipo"), ", ".join(a.get("especialidades") or []) or None,
                             a.get("empresa") or a.get("entidad"),
                             a.get("departamento") and "📍 " + a["departamento"],
                             a.get("monto") and f"💰 S/ {a['monto']:,.0f}", a.get("cui") and f"CUI {a['cui']}") if x]
        filas.append(f"""
<tr><td style="padding:10px;border-bottom:1px solid #e5e7eb;font-family:Arial,sans-serif;font-size:14px">
  <div style="font-size:12px"><span style="background:{color};color:#fff;padding:2px 7px;border-radius:10px">{e(clase)}</span>
   <span style="color:#6b7280">&nbsp;{e(a['grupo'])}</span></div>
  <div style="font-weight:bold;margin:5px 0"><a href="{e(a.get('url_original') or a['url'])}" style="color:#1d4ed8;text-decoration:none">{e(nombre[:300])}</a></div>
  {f'<div style="font-size:12px;color:#6b7280">{e(a["titulo"][:200])}</div>' if a.get("obra") else ""}
  <div style="color:#374151">{e(" · ".join(datos))}</div>
  {f'<div style="font-size:13px"><a href="{e(a["url"])}" style="color:#0a66c2">🔗 Ver publicación en LinkedIn</a> · 👤 {e(a.get("publicado_por") or "")}{" · ✉️ " + e(a["contacto"]) if a.get("contacto") else ""}{" · 🔧 " + e(a["partidas"]) if a.get("partidas") else ""}</div>' if a.get("origen") == "linkedin" else ""}
  {f'<div style="font-size:13px;color:#b45309"><b>⏰ Presentación de propuestas: {e(a["fecha_limite"][8:10] + "/" + a["fecha_limite"][5:7] + "/" + a["fecha_limite"][:4])}</b></div>' if a.get("fecha_limite") else ""}
  {f'<div style="font-size:12px;color:#0e7490">🏢 {e(a["empresa_privada"])} · 🧭 {e(a["como_postular"])}</div>' if a.get("como_postular") else ""}
  <div style="font-size:12px;color:#6b7280">📅 {e(fecha_legible(a))}{f' · <a href="{e(a["ficha_cui"])}" style="color:#1d4ed8">consultar CUI {e(a["cui"])} en el MEF</a>' if a.get("ficha_cui") else ""}</div>
</td></tr>""")
    return ('<h3 style="color:#7c3aed">📣 Avisos: OxI, ANIN, PEIP, Petroperú, mineras y subcontratos</h3>'
            '<table style="border-collapse:collapse;width:100%">' + "".join(filas) + "</table>")
