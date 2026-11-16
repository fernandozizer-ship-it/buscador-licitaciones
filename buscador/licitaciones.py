#!/usr/bin/env python3
"""
Buscador y notificador de licitaciones de obra, supervisión y consultoría de obra.
Fase 1: SEACE (todas las entidades del país) vía datos abiertos OCDS del OECE.

Qué hace en cada ejecución:
  1. Descarga de la API del OECE los procesos publicados desde la última ejecución.
  2. Se queda solo con obras, supervisión y consultoría de obra.
  3. Los clasifica por región, especialidad, monto, entidad y tipo de procedimiento.
  4. Actualiza docs/datos.json (lo que muestra la página del buscador).
  5. Envía por correo los procesos NUEVOS que pasan tus filtros (config.json).

Uso:   python buscador/licitaciones.py
Variables de entorno (en GitHub van como "Secrets"):
  CORREO_DESTINO        uno o varios correos separados por coma
  Opción A (Gmail):     GMAIL_USUARIO, GMAIL_APP_PASSWORD
  Opción B (Brevo):     BREVO_API_KEY, BREVO_REMITENTE (correo verificado en Brevo)
  DIAGNOSTICO=1         guarda muestra_api.json con una muestra cruda de la API
"""
import datetime as dt
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
import unicodedata
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUTA_CONFIG = os.path.join(RAIZ, "buscador", "config.json")
RUTA_DATOS = os.path.join(RAIZ, "docs", "datos.json")
RUTA_ESTADO = os.path.join(RAIZ, "estado.json")
RUTA_MUESTRA = os.path.join(RAIZ, "muestra_api.json")

API_ARCHIVOS = "https://contratacionesabiertas.oece.gob.pe/api/v1/files?page={pagina}&paginateBy=50&format=json"
URL_PROCESO = "https://contratacionesabiertas.oece.gob.pe/proceso/{ocid}"
LIMA_TZ = dt.timezone(dt.timedelta(hours=-5))
ESTADOS_CERRADOS = {"complete", "cancelled", "unsuccessful", "withdrawn"}
ESTADOS_NO_VIGENTES = {"Cerrado", "Contratado", "Adjudicado"}


# ----------------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------------
def normalizar(texto):
    """minúsculas, sin tildes, espacios simples."""
    if not texto:
        return ""
    t = unicodedata.normalize("NFKD", str(texto))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t.lower()).strip()


def regex_palabra(palabra):
    p = re.escape(normalizar(palabra))
    return re.compile(r"(?<![a-z0-9])" + p + r"(?![a-z0-9])")


def parse_fecha(valor):
    if not valor or not isinstance(valor, str):
        return None
    v = valor.strip().replace("Z", "+00:00")
    for intento in (v, v[:19], v[:10]):
        try:
            f = dt.datetime.fromisoformat(intento)
            if f.tzinfo is None:
                f = f.replace(tzinfo=LIMA_TZ)
            return f
        except ValueError:
            continue
    return None


def iso(f):
    return f.astimezone(LIMA_TZ).isoformat(timespec="minutes") if f else None


def ahora():
    return dt.datetime.now(LIMA_TZ)


def leer_json(ruta, defecto):
    try:
        with open(ruta, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return defecto


def guardar_json(ruta, data, compacto=False):
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    with open(ruta, "w", encoding="utf-8") as fh:
        if compacto:
            json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(data, fh, ensure_ascii=False, indent=2)


# ----------------------------------------------------------------------------
# Ubicación
# ----------------------------------------------------------------------------
DEPARTAMENTOS = {
    "amazonas": "Amazonas", "ancash": "Áncash", "apurimac": "Apurímac", "arequipa": "Arequipa",
    "ayacucho": "Ayacucho", "cajamarca": "Cajamarca", "callao": "Callao", "cusco": "Cusco",
    "cuzco": "Cusco", "huancavelica": "Huancavelica", "huanuco": "Huánuco", "ica": "Ica",
    "junin": "Junín", "la libertad": "La Libertad", "lambayeque": "Lambayeque", "lima": "Lima",
    "loreto": "Loreto", "madre de dios": "Madre de Dios", "moquegua": "Moquegua", "pasco": "Pasco",
    "piura": "Piura", "puno": "Puno", "san martin": "San Martín", "tacna": "Tacna",
    "tumbes": "Tumbes", "ucayali": "Ucayali",
}
# Provincias de las regiones prioritarias (para detectar la región cuando solo aparece la provincia)
PROVINCIAS = {
    "pisco": "Ica", "chincha": "Ica", "nasca": "Ica", "nazca": "Ica", "palpa": "Ica",
    "canete": "Lima", "huaral": "Lima", "huaura": "Lima", "barranca": "Lima", "huarochiri": "Lima",
    "yauyos": "Lima", "canta": "Lima", "oyon": "Lima", "cajatambo": "Lima",
    "camana": "Arequipa", "caraveli": "Arequipa", "caylloma": "Arequipa", "condesuyos": "Arequipa",
    "islay": "Arequipa", "mollendo": "Arequipa",
    "mariscal nieto": "Moquegua", "ilo": "Moquegua", "general sanchez cerro": "Moquegua",
}
_CLAVE = r"(?:provincia|departamento|region|distrito|centro poblado|localidad|sector|anexo|caserio)"
_SEP = (r"(?=\s*[-,/;:(\"'\u201c\u201d]|\s+(?:de\s+la\s+|del\s+|de\s+|y\s+|en\s+(?:el|la)\s+)?" + _CLAVE +
        r"\b|\s+(?:y|e|con|para|segun|cui|codigo|ejecucion|mejoramiento|creacion|ampliacion|"
        r"contratacion|servicio|obra|meta)\b|\s*$)")
RE_DEP_TXT = re.compile(r"(?:departamento|region|gobierno regional)\s+(?:de\s+|del\s+)?([a-z ]{3,30}?)" + _SEP)
RE_PROV_TXT = re.compile(r"(?:provincia|municipalidad provincial)\s+(?:constitucional\s+)?(?:de\s+|del\s+)?"
                         r"([a-z ]{3,40}?)" + _SEP)
RE_DIST_TXT = re.compile(r"(?:distrito|municipalidad distrital)\s+(?:de\s+|del\s+)?([a-z ]{3,40}?)" + _SEP)
_CONECTORES = {"de", "del", "la", "las", "los", "el", "y", "e", "en"}


def limpiar_lugar(texto):
    """Quita conectores sobrantes ("perene de la" -> "perene") y nombres de región pegados."""
    if not texto:
        return None
    t = texto.strip()
    m = re.match(r"^y departamento (?:de\s+)?(.+)$", t)
    if m:
        t = m.group(1)
    t = re.sub(r"^(?:la\s+)?(?:provincia|distrito)\s+(?:de\s+|del\s+)?", "", t)
    palabras = t.split()
    # Cortar en palabras de lugar mal escritas o truncadas ("provinci", "laprovincia", "departa")
    for i, w in enumerate(palabras):
        if i > 0 and re.match(r"^(?:la|de|del)?(?:provin|depart|distri|region)", w):
            palabras = palabras[:i]
            break
    while palabras and palabras[-1] in _CONECTORES:
        palabras.pop()
    while palabras and palabras[0] in ("de", "del", "y", "e", "en"):
        palabras.pop(0)
    if len(palabras) > 1:
        for n in (3, 2, 1):  # "rodriguez de mendoza amazonas" -> "rodriguez de mendoza"
            cola = " ".join(palabras[-n:])
            if len(palabras) > n and cola in DEPARTAMENTOS:
                palabras = palabras[:-n]
                while palabras and palabras[-1] in _CONECTORES:
                    palabras.pop()
                break
    t = " ".join(palabras)
    if t in ("departamento", "provincia", "distrito", "region") or len(t) < 3:
        return None
    return t


def _departamento_en(texto_n):
    texto_n = texto_n.strip()
    if texto_n in DEPARTAMENTOS:
        return DEPARTAMENTOS[texto_n]
    for clave, nombre in DEPARTAMENTOS.items():
        if re.search(r"(?<![a-z])" + clave + r"(?![a-z])", texto_n):
            return nombre
    return None


def _titulo(s):
    if not s:
        return None
    palabras = s.split()
    return " ".join(w.capitalize() if (i == 0 or w not in ("de", "del", "la", "las", "los", "y")) else w
                    for i, w in enumerate(palabras))


def ubicar(release, texto_n, entidad_n):
    """Devuelve (departamento, provincia, distrito). Prioriza lo que dice el nombre del
    proyecto (dónde se hace la obra) sobre la dirección de la entidad (dónde está la oficina)."""
    dep = prov = dist = None
    m = RE_DEP_TXT.search(texto_n)
    if m:
        dep = _departamento_en(limpiar_lugar(m.group(1)) or "")
    m = RE_PROV_TXT.search(texto_n) or RE_PROV_TXT.search(entidad_n)
    if m:
        prov = limpiar_lugar(m.group(1))
    m = RE_DIST_TXT.search(texto_n) or RE_DIST_TXT.search(entidad_n)
    if m:
        dist = limpiar_lugar(m.group(1))
    if not dep and prov:
        dep = PROVINCIAS.get(prov) or DEPARTAMENTOS.get(prov)
    if not dep:
        m = RE_DEP_TXT.search(entidad_n)
        if m:
            dep = _departamento_en(m.group(1))
    if not dep:
        # Dirección registrada de la entidad compradora
        buyer_id = (release.get("buyer") or {}).get("id")
        for p in release.get("parties") or []:
            if "buyer" in (p.get("roles") or []) or (buyer_id and p.get("id") == buyer_id):
                a = p.get("address") or {}
                for campo in ("region", "department", "locality", "streetAddress"):
                    d = _departamento_en(normalizar(a.get(campo)))
                    if d:
                        dep = d
                        break
            if dep:
                break
    if not dep:
        for clave, nombre in PROVINCIAS.items():
            if re.search(r"(?<![a-z])" + clave + r"(?![a-z])", entidad_n):
                dep = nombre
                break
    if not dep:
        dep = _departamento_en(entidad_n)
    return dep, _titulo(prov), _titulo(dist)


# ----------------------------------------------------------------------------
# Clasificación
# ----------------------------------------------------------------------------
RE_SUPERVISION = re.compile(r"(?<![a-z])supervision(?![a-z])")
RE_CONSULTORIA = re.compile(r"consultoria de obra|expediente tecnico|elaboracion del? estudio|estudio definitivo|"
                            r"estudios? de pre ?inversion|perfil tecnico|ficha tecnica")
RE_CONTEXTO_OBRA = re.compile(r"(?<![a-z])(obra|obras|mejoramiento|creacion|ampliacion|instalacion|rehabilitacion|"
                              r"recuperacion|construccion|reconstruccion|renovacion|remodelacion|"
                              r"reparacion|cui|proyecto de inversion)(?![a-z])")
RE_EJECUCION_OBRA = re.compile(r"ejecucion de (la )?obra|contratacion de (la )?obra")


def clasificar_tipo(tender, texto_n):
    cat = normalizar(tender.get("mainProcurementCategory"))
    adic = [normalizar(x) for x in (tender.get("additionalProcurementCategories") or [])]
    if cat == "goods":
        return None
    if RE_SUPERVISION.search(texto_n) and RE_CONTEXTO_OBRA.search(texto_n):
        return "Supervisión"
    if RE_CONSULTORIA.search(texto_n) and (cat != "works"):
        return "Consultoría de obra"
    if cat == "works" or "works" in adic or RE_EJECUCION_OBRA.search(texto_n):
        return "Obra"
    if ("consultingservices" in adic or cat == "consultingservices") and RE_CONTEXTO_OBRA.search(texto_n):
        return "Consultoría de obra"
    return None


def compilar_especialidades(config):
    return {nombre: [regex_palabra(k) for k in claves]
            for nombre, claves in config.get("especialidades", {}).items()}


def especialidades_de(texto_n, especialidades_rx):
    return [nombre for nombre, rxs in especialidades_rx.items() if any(r.search(texto_n) for r in rxs)]


def texto_de(tender):
    partes = [tender.get("title"), tender.get("description")]
    for it in tender.get("items") or []:
        partes.append(it.get("description"))
        partes.append((it.get("classification") or {}).get("description"))
    return " ".join(p for p in partes if isinstance(p, str))


def estado_proceso(release, tender):
    if tender.get("status") in ESTADOS_CERRADOS:
        return "Cerrado"
    if release.get("contracts"):
        return "Contratado"
    if release.get("awards"):
        return "Adjudicado"
    tags = release.get("tag") or []
    if "contract" in tags:
        return "Contratado"
    if "award" in tags:
        return "Adjudicado"
    return "Convocado"


def convertir(release, especialidades_rx):
    """Convierte un release OCDS en un registro del buscador (o None si no es de interés)."""
    tender = release.get("tender") or {}
    if not tender:
        return None
    texto = texto_de(tender)
    texto_n = normalizar(texto)
    tipo = clasificar_tipo(tender, texto_n)
    if not tipo:
        return None
    buyer = release.get("buyer") or {}
    entidad = buyer.get("name")
    if not entidad:
        for p in release.get("parties") or []:
            if "buyer" in (p.get("roles") or []):
                entidad = p.get("name")
                break
    entidad_n = normalizar(entidad)
    dep, prov, dist = ubicar(release, texto_n, entidad_n)
    valor = tender.get("value") or {}
    monto = valor.get("amount")
    try:
        monto = round(float(monto), 2) if monto not in (None, "") else None
    except (TypeError, ValueError):
        monto = None
    periodo = tender.get("tenderPeriod") or {}
    consultas = tender.get("enquiryPeriod") or {}
    documentos = [{"titulo": (d.get("title") or "").strip(), "tipo": d.get("documentType") or "",
                   "fecha": iso(parse_fecha(d.get("datePublished"))), "url": d.get("url")}
                  for d in (tender.get("documents") or []) if d.get("url")]
    # Las bases integradas traen el cronograma actualizado (prórrogas incluidas)
    bases = (next((d for d in documentos if "integrad" in normalizar(d["titulo"])), None)
             or next((d for d in documentos if d["tipo"] == "biddingDocuments"
                      or re.search(r"\bbases\b", normalizar(d["titulo"]))), None))
    ocid = release.get("ocid") or release.get("id")
    titulo = tender.get("title") or ""
    descripcion = tender.get("description") or ""
    if not descripcion and tender.get("items"):
        descripcion = (tender["items"][0] or {}).get("description") or ""
    return {
        "id": ocid,
        "fuente": "SEACE",
        "sector": "Público",
        "procedencia": "SEACE (OECE)",
        "tipo": tipo,
        "especialidades": especialidades_de(texto_n, especialidades_rx),
        "titulo": titulo.strip(),
        "descripcion": descripcion.strip(),
        "entidad": (entidad or "").strip(),
        "departamento": dep,
        "provincia": prov,
        "distrito": dist,
        "monto": monto,
        "moneda": valor.get("currency") or "PEN",
        "procedimiento": tender.get("procurementMethodDetails") or tender.get("procurementMethod") or "",
        "estado": estado_proceso(release, tender),
        "publicado": iso(parse_fecha(tender.get("datePublished")) or parse_fecha(release.get("date"))),
        # En el SEACE v3 el "tenderPeriod" es solo el día de la convocatoria (no el cierre):
        # el plazo real es la presentación de propuestas, que se completa con completar_fechas()
        "convocatoria": iso(parse_fecha(periodo.get("startDate"))),
        "consultas_inicio": iso(parse_fecha(consultas.get("startDate"))),
        "consultas_fin": iso(parse_fecha(consultas.get("endDate"))),
        "documentos": documentos[-12:],
        "bases_url": bases["url"] if bases else None,
        "vr_reservado": bool(tender.get("hasTenderInformationProtectedByLaw")) or not monto,
        "postores": tender.get("numberOfTenderers"),
        "actualizado": iso(parse_fecha(release.get("date"))),
        "url": URL_PROCESO.format(ocid=ocid),
    }



# ----------------------------------------------------------------------------
# Fechas del procedimiento: consultas, presentación de propuestas, buena pro y etapa
# ----------------------------------------------------------------------------
RE_FECHA_DMA = r"(\d{1,2})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(20\d\d)"
RE_SIGUIENTE_ETAPA = re.compile(r"convocatoria|registro de participantes|formulacion de consultas|absolucion de consultas|"
                                r"integracion de (?:las )?bases|presentacion de (?:ofertas|propuestas)|calificacion|"
                                r"evaluacion|otorgamiento de la buena pro|consentimiento")
RE_PROPUESTAS = re.compile(r"presentacion de (?:ofertas|propuestas)(?: \(?electronica\)?)?(.{0,200})")
RE_BUENA_PRO = re.compile(r"otorgamiento de la buena pro(.{0,200})")
RE_TERMINADO = re.compile(r"buena pro|otorgamiento|consentimiento|acta de adjudicacion|desiert|cancelaci|nulidad|contrato")


def _fechas_en(trozo):
    salida = []
    for d, m, a in re.findall(RE_FECHA_DMA, trozo or ""):
        try:
            salida.append(dt.datetime(int(a), int(m), int(d), 23, 59, tzinfo=LIMA_TZ))
        except ValueError:
            pass
    return salida


def cronograma_de_texto(texto, convocatoria=None):
    """Busca en el texto de las bases la fecha de presentación de propuestas y de la buena pro."""
    t = normalizar(texto)
    resultado = {}
    for clave, rx in (("propuestas", RE_PROPUESTAS), ("buena_pro", RE_BUENA_PRO)):
        for m in rx.finditer(t):
            trozo = m.group(1)
            corte = RE_SIGUIENTE_ETAPA.search(trozo)
            fechas = _fechas_en(trozo[:corte.start()] if corte else trozo)
            if convocatoria:
                fechas = [f for f in fechas if convocatoria - dt.timedelta(days=1) <= f <= convocatoria + dt.timedelta(days=150)]
            if fechas:
                resultado[clave] = max(fechas)
                break
    if "propuestas" in resultado and "buena_pro" in resultado and resultado["buena_pro"] < resultado["propuestas"]:
        del resultado["buena_pro"]
    return resultado


def completar_fechas(r, hoy):
    """Fecha de presentación de propuestas (de las bases o estimada) y etapa actual del proceso."""
    conv = parse_fecha(r.get("convocatoria")) or parse_fecha(r.get("publicado"))
    fin_consultas = parse_fecha(r.get("consultas_fin"))
    propuestas = parse_fecha(r.get("propuestas")) if r.get("cronograma_fuente") == "bases" else None
    if propuestas is None:
        # Estimado con los plazos habituales: propuestas ~6 días después del fin de consultas
        if fin_consultas:
            propuestas = fin_consultas + dt.timedelta(days=6)
        elif conv:
            propuestas = conv.replace(hour=23, minute=59) + dt.timedelta(days=11)
        # Si hubo prórroga, la absolución o integración de bases se publicó después: propuestas ~5 días más tarde
        for d in r.get("documentos") or []:
            f = parse_fecha(d.get("fecha"))
            if f and propuestas and re.search(r"absolucion|integrad|prorroga|postergacion", normalizar(d.get("titulo"))):
                propuestas = max(propuestas, f.replace(hour=23, minute=59) + dt.timedelta(days=5))
        r["propuestas"] = iso(propuestas)
        r["cronograma_fuente"] = "estimado" if propuestas else None
    r["cierre"] = iso(propuestas)
    titulos = normalizar(" ".join(d.get("titulo", "") for d in r.get("documentos") or []))
    if r.get("estado") in ("Adjudicado", "Contratado", "Cerrado"):
        r["etapa"] = {"Cerrado": "Cerrado / cancelado"}.get(r["estado"], r["estado"])
    elif re.search(r"desiert", titulos):
        r["etapa"] = "Desierto"
    elif RE_TERMINADO.search(titulos):
        r["etapa"] = "Buena pro otorgada"
    elif fin_consultas and hoy <= fin_consultas:
        r["etapa"] = "Consultas abiertas"
    elif propuestas and hoy <= propuestas:
        r["etapa"] = "Por presentar propuestas"
    elif propuestas:
        r["etapa"] = "En evaluación"
    else:
        r["etapa"] = "Convocado"
    return r


def leer_cronogramas(registros, prioridad_ids, config, hoy):
    """Abre las bases (PDF del SEACE) de los procesos vigentes y lee su cronograma."""
    tope = int(config.get("max_bases_por_ejecucion", 30))
    if tope <= 0:
        return
    try:
        from pypdf import PdfReader
    except ImportError:
        print("Cronogramas: falta pypdf, se usan fechas estimadas")
        return
    import io
    regiones = set(config.get("regiones_prioritarias", []))
    candidatos = [r for r in registros.values() if r.get("bases_url") and not r.get("cronograma_revisado")
                  and r.get("etapa") in ("Consultas abiertas", "Por presentar propuestas", "Convocado")]
    candidatos.sort(key=lambda r: (r["id"] not in prioridad_ids, r.get("departamento") not in regiones,
                                   r.get("tipo") != "Obra", (r.get("publicado") or "")), reverse=False)
    sesion = nueva_sesion()
    leidos = encontrados = 0
    for r in candidatos[:tope]:
        r["cronograma_revisado"] = True
        try:
            resp = sesion.get(r["bases_url"], timeout=120, stream=True)
            datos = b""
            for bloque in resp.iter_content(1 << 20):
                datos += bloque
                if len(datos) > 30_000_000:
                    raise RuntimeError("PDF de más de 30 MB")
            if resp.status_code != 200 or not datos.startswith(b"%PDF"):
                raise RuntimeError(f"HTTP {resp.status_code}, no es PDF")
            lector = PdfReader(io.BytesIO(datos))
            texto = " ".join((p.extract_text() or "") for p in lector.pages[:25])
            leidos += 1
            crono = cronograma_de_texto(texto, parse_fecha(r.get("convocatoria")) or parse_fecha(r.get("publicado")))
            if crono.get("propuestas"):
                encontrados += 1
                r["propuestas"] = iso(crono["propuestas"])
                r["cronograma_fuente"] = "bases"
                if crono.get("buena_pro"):
                    r["buena_pro"] = iso(crono["buena_pro"])
            print(f"  bases {r['titulo'][:28]}: {len(texto)} caracteres, "
                  f"propuestas={'%s' % crono['propuestas'].date() if crono.get('propuestas') else 'no indicado'}")
        except Exception as e:  # noqa: BLE001
            print(f"  bases {r.get('titulo', '')[:28]}: no se pudo leer ({str(e)[:90]})")
        completar_fechas(r, hoy)
        time.sleep(0.5)
    print(f"Cronogramas: {leidos} bases leídas, {encontrados} con fecha de propuestas "
          f"({max(0, len(candidatos) - tope)} quedan para la próxima ejecución)")

# ----------------------------------------------------------------------------
# Descarga (archivos mensuales de datos abiertos del OECE)
# ----------------------------------------------------------------------------
# La API "releasesAfter" no ordena por fecha, así que se usan los archivos de
# descarga masiva por mes (los mismos de contratacionesabiertas.oece.gob.pe/descargas),
# que el OECE regenera a diario.
CABECERAS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-PE,es;q=0.9",
    "Referer": "https://contratacionesabiertas.oece.gob.pe/",
}
RE_ANIO_MES = re.compile(r"(20\d\d)[/_\-.]?(0[1-9]|1[0-2]|[1-9])(?!\d)")


def nueva_sesion():
    sesion = requests.Session()
    sesion.headers.update(CABECERAS)
    return sesion


def pedir(sesion, url, intentos=3):
    """Pide un JSON a la API; deja en el registro la respuesta exacta si falla."""
    ultimo = None
    for i in range(intentos):
        try:
            r = sesion.get(url, timeout=90)
            if r.status_code == 200:
                return r.json()
            ultimo = (f"HTTP {r.status_code} | respuesta: {r.text[:250].strip()!r}")
            print(f"  intento {i + 1} en {url[:80]} -> {ultimo}")
            if r.status_code in (401, 403):
                break
        except (requests.RequestException, ValueError) as e:
            ultimo = str(e)[:250]
            print(f"  intento {i + 1} -> error: {ultimo}")
        time.sleep(3 * (i + 1))
    raise RuntimeError(f"La API del OECE no respondió ({ultimo}) en {url}")


def mes_de_archivo(item):
    """Intenta saber a qué (año, mes) corresponde una entrada de la lista de archivos."""
    anio = item.get("year") or item.get("anio") or item.get("año")
    mes = item.get("month") or item.get("mes")
    try:
        if anio and mes:
            return int(anio), int(mes)
    except (TypeError, ValueError):
        pass
    textos = [str(v) for k, v in item.items() if k != "files" and isinstance(v, (str, int))]
    textos += [str(v) for v in (item.get("files") or {}).values()]
    for t in textos:
        m = RE_ANIO_MES.search(t)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


def meses_entre(desde, hasta):
    a, m = desde.year, desde.month
    meses = []
    while (a, m) <= (hasta.year, hasta.month):
        meses.append((a, m))
        a, m = (a + 1, 1) if m == 12 else (a, m + 1)
    return meses


def listar_archivos(sesion, meses, diagnostico):
    """Devuelve {(año, mes): url_json} para los meses pedidos."""
    encontrados, sin_mes, primera = {}, [], None
    for pagina in range(1, 8):
        data = pedir(sesion, API_ARCHIVOS.format(pagina=pagina))
        if primera is None:
            primera = data
        resultados = data.get("results") or data.get("data") or []
        for item in resultados:
            url = ((item.get("files") or {}).get("json") or "").replace(".osce.", ".oece.")
            if not url:
                continue
            am = mes_de_archivo(item)
            if am is None:
                sin_mes.append(url)
            elif am in meses and am not in encontrados:
                encontrados[am] = url
        total_pag = (data.get("pagination") or {}).get("num_pages") or 1
        if set(meses) <= set(encontrados) or pagina >= int(total_pag):
            break
    if diagnostico and primera is not None:
        corto = dict(primera)
        if isinstance(corto.get("results"), list):
            corto["results"] = corto["results"][:6]
        guardar_json(RUTA_MUESTRA, {"lista_de_archivos": corto})
    print(f"Archivos mensuales encontrados: {sorted(encontrados)} | sin mes reconocible: {len(sin_mes)}")
    if not encontrados and sin_mes:
        print("AVISO: no se reconoció el mes de los archivos; se usa el primero de la lista.")
        encontrados[meses[-1]] = sin_mes[0]
    if not encontrados:
        raise RuntimeError("No se encontraron archivos mensuales en la API del OECE")
    return encontrados


def descargar_archivo(sesion, url, destino):
    for i in range(3):
        try:
            with sesion.get(url, timeout=300, stream=True) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]!r}")
                total = 0
                with open(destino, "wb") as fh:
                    for bloque in r.iter_content(1 << 20):
                        fh.write(bloque)
                        total += len(bloque)
            print(f"  descargado {total / 1e6:.1f} MB")
            return
        except (requests.RequestException, RuntimeError) as e:
            print(f"  intento {i + 1} de descarga falló: {e}")
            time.sleep(10 * (i + 1))
    raise RuntimeError(f"No se pudo descargar {url}")


def iterar_objetos_json(texto_fh):
    """Recorre uno a uno los elementos de "records" o "releases" sin cargar todo el archivo en memoria."""
    decodificador = json.JSONDecoder()
    buf, pos, fin = "", 0, False

    def leer():
        nonlocal buf, fin
        trozo = texto_fh.read(1 << 20)
        if not trozo:
            fin = True
        buf += trozo

    inicio = re.compile(r'"(records|releases)"\s*:\s*\[')
    while True:
        m = inicio.search(buf)
        if m:
            pos = m.end()
            break
        if fin:
            return
        leer()
    while True:
        while True:
            while pos < len(buf) and buf[pos] in " \t\r\n,":
                pos += 1
            if pos < len(buf) or fin:
                break
            leer()
        if pos >= len(buf) or buf[pos] == "]":
            return
        try:
            obj, fin_obj = decodificador.raw_decode(buf, pos)
        except json.JSONDecodeError:
            if fin:
                raise
            leer()
            continue
        yield obj
        pos = fin_obj
        if pos > (8 << 20):
            buf, pos = buf[pos:], 0


def releases_de_archivo(ruta):
    """Entrega releases (compilados) desde un .zip / .json / .jsonl del OECE."""
    import io
    import zipfile

    def de_objeto(obj):
        if "compiledRelease" in obj:
            return obj["compiledRelease"]
        if "releases" in obj and "tender" not in obj:
            con_tender = [r for r in obj.get("releases") or [] if isinstance(r, dict) and r.get("tender")]
            return con_tender[-1] if con_tender else None
        return obj

    def de_flujo(nombre, binario):
        texto = io.TextIOWrapper(binario, encoding="utf-8", errors="replace")
        if nombre.endswith(".jsonl"):
            for linea in texto:
                linea = linea.strip()
                if linea:
                    rel = de_objeto(json.loads(linea))
                    if rel:
                        yield rel
        else:
            for obj in iterar_objetos_json(texto):
                rel = de_objeto(obj)
                if rel:
                    yield rel

    if zipfile.is_zipfile(ruta):
        with zipfile.ZipFile(ruta) as z:
            for nombre in z.namelist():
                if nombre.lower().endswith((".json", ".jsonl")):
                    with z.open(nombre) as fh:
                        yield from de_flujo(nombre.lower(), fh)
    else:
        import gzip
        with open(ruta, "rb") as crudo:
            gz = crudo.read(2) == b"\x1f\x8b"
        abrir = gzip.open if gz else open
        with abrir(ruta, "rb") as fh:
            yield from de_flujo(ruta.lower().replace(".gz", ""), fh)


def descargar(desde, diagnostico=False):
    """Descarga los archivos mensuales necesarios y devuelve los releases publicados desde 'desde'."""
    import tempfile
    sesion = nueva_sesion()
    # Siempre se releen los últimos ~2 meses: así se actualiza la etapa (buena pro, desierto) de lo ya guardado
    meses = meses_entre(min(desde, ahora() - dt.timedelta(days=int(os.environ.get("DIAS_REFRESCO", "62")))), ahora())
    archivos = listar_archivos(sesion, meses, diagnostico)
    carpeta = tempfile.mkdtemp(prefix="oece_")
    releases, leidos = [], 0
    from collections import Counter
    categorias = Counter()
    for am, url in sorted(archivos.items()):
        print(f"Mes {am[0]}-{am[1]:02d}: {url}")
        destino = os.path.join(carpeta, f"{am[0]}_{am[1]:02d}.dat")
        descargar_archivo(sesion, url, destino)
        for rel in releases_de_archivo(destino):
            leidos += 1
            tender = rel.get("tender") or {}
            categorias[normalizar(tender.get("mainProcurementCategory")) or "(vacio)"] += 1
            if diagnostico and leidos <= 2:
                muestra = leer_json(RUTA_MUESTRA, {})
                muestra.setdefault("ejemplos_de_procesos", []).append(rel)
                guardar_json(RUTA_MUESTRA, muestra)
            releases.append(rel)
        try:
            os.remove(destino)
        except OSError:
            pass
    print(f"Procesos leídos: {leidos} (meses {', '.join(f'{a}-{m:02d}' for a, m in meses)})")
    print("DIAGNÓSTICO — categorías:", dict(categorias.most_common(8)))
    return releases


# ----------------------------------------------------------------------------
# Filtros de correo
# ----------------------------------------------------------------------------
def pasa_filtro_correo(reg, config, excluidas_rx):
    if reg["tipo"] not in config.get("tipos_para_correo", []):
        return False
    if reg.get("estado") in ESTADOS_NO_VIGENTES or reg.get("etapa") in ("Buena pro otorgada", "Desierto", "En evaluación"):
        return False
    cierre = parse_fecha(reg.get("cierre"))
    if cierre and cierre < ahora():
        return False
    texto_n = normalizar(f"{reg['titulo']} {reg['descripcion']}")
    if any(r.search(texto_n) for r in excluidas_rx):
        return False
    solo = config.get("solo_estas_especialidades") or []
    if solo and not set(solo) & set(reg["especialidades"]):
        return False
    rango = config.get("montos_para_correo", {}).get(reg["tipo"], {})
    if reg["monto"] is None:
        return bool(config.get("incluir_sin_monto", True))
    if reg.get("moneda") not in (None, "", "PEN"):
        return True  # monto en otra moneda: se envía y tú decides
    return rango.get("minimo", 0) <= reg["monto"] <= rango.get("maximo", float("inf"))


# ----------------------------------------------------------------------------
# Correo
# ----------------------------------------------------------------------------
def fmt_monto(reg):
    if not reg.get("monto"):
        return "Valor referencial reservado / no publicado"
    simbolo = "S/" if reg.get("moneda") in (None, "", "PEN") else reg["moneda"]
    return f"{simbolo} {reg['monto']:,.0f}"


def fmt_fecha(valor):
    f = parse_fecha(valor)
    return f.strftime("%d/%m/%Y %H:%M") if f else "—"


def fmt_dia(valor):
    f = parse_fecha(valor)
    return f.astimezone(LIMA_TZ).strftime("%d/%m/%Y") if f else "—"


def fila_html(reg):
    e = html.escape
    lugar = " · ".join(x for x in (reg.get("distrito"), reg.get("provincia"), reg.get("departamento")) if x) or "Ubicación no detectada"
    nombre = reg["descripcion"] or reg["titulo"]
    especialidad = ", ".join(reg["especialidades"]) or "Sin clasificar"
    return f"""
<tr><td style="padding:10px;border-bottom:1px solid #e5e7eb;font-family:Arial,sans-serif;font-size:14px">
  <div style="font-size:12px;color:#6b7280">{e(reg['tipo'])} · {e(especialidad)} · {e(reg['procedimiento'])}</div>
  <div style="font-weight:bold;margin:3px 0"><a href="{e(reg['url'])}" style="color:#1d4ed8;text-decoration:none">{e(nombre[:300])}</a></div>
  <div>{e(reg['entidad'])}</div>
  <div style="color:#374151">📍 {e(lugar)} &nbsp;|&nbsp; 💰 <b>{e(fmt_monto(reg))}</b></div>
  <div style="color:#374151">📅 Publicado: {e(fmt_dia(reg.get('publicado')))} &nbsp;|&nbsp; ❓ Consultas hasta: {e(fmt_dia(reg.get('consultas_fin')))} &nbsp;|&nbsp; 📨 <b>Propuestas: {e(fmt_dia(reg.get('cierre')))}{" (según bases)" if reg.get("cronograma_fuente") == "bases" else " (aprox.)"}</b>{f" &nbsp;|&nbsp; 🏆 Buena pro: {e(fmt_dia(reg.get('buena_pro')))}" if reg.get("buena_pro") else ""}</div>
  {f'<div style="font-size:12px"><a href="{e(reg["bases_url"])}">📄 Descargar bases</a></div>' if reg.get("bases_url") else ""}
  <div style="font-size:12px;color:#6b7280">{e(reg['titulo'])}</div>
</td></tr>"""


def armar_correo(prioritarias, resto, config, url_buscador, avisos_html="", n_avisos=0):
    max_resto = int(config.get("max_lineas_resto_del_pais", 40))
    regiones = ", ".join(config.get("regiones_prioritarias", []))
    partes = [f"""<html><head><meta charset="utf-8"></head><body><div style="font-family:Arial,sans-serif;max-width:720px">
<h2 style="color:#111827;margin-bottom:4px">🏗️ Licitaciones nuevas — {ahora().strftime('%d/%m/%Y %H:%M')}</h2>
<p style="color:#374151;margin-top:0">SEACE: {len(prioritarias)} en tus regiones prioritarias ({html.escape(regiones)}) y {len(resto)} en el resto del país.{f" Además, {n_avisos} avisos de Obras por Impuestos / ANIN / PEIP." if n_avisos else ""}
<br><a href="{html.escape(url_buscador)}">Abrir el buscador completo</a></p>"""]
    if prioritarias:
        partes.append('<h3 style="color:#b91c1c">🔴 Regiones prioritarias</h3><table style="border-collapse:collapse;width:100%">')
        partes += [fila_html(r) for r in prioritarias]
        partes.append("</table>")
    if resto:
        partes.append('<h3 style="color:#1f2937">Resto del país</h3><table style="border-collapse:collapse;width:100%">')
        partes += [fila_html(r) for r in resto[:max_resto]]
        partes.append("</table>")
        if len(resto) > max_resto:
            partes.append(f'<p>… y {len(resto) - max_resto} más en el <a href="{html.escape(url_buscador)}">buscador</a>.</p>')
    if avisos_html:
        partes.append(avisos_html)
    partes.append('<p style="font-size:12px;color:#6b7280">Fuente: SEACE vía datos abiertos del OECE. Verifica siempre plazos y requisitos en las bases oficiales.</p></div></body></html>')
    asunto = f"🏗️ Licitaciones nuevas: {len(prioritarias)} en tus regiones + {len(resto)} en el resto del país"
    if n_avisos:
        asunto += f" + {n_avisos} avisos OxI/ANIN/PEIP"
    return asunto, "".join(partes)


def enviar_correo(asunto, cuerpo_html):
    destinos = [d.strip() for d in os.environ.get("CORREO_DESTINO", "").split(",") if d.strip()]
    if not destinos:
        print("Sin CORREO_DESTINO configurado: no se envía correo.")
        return False
    brevo = os.environ.get("BREVO_API_KEY")
    if brevo:
        remitente = os.environ.get("BREVO_REMITENTE") or destinos[0]
        r = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": brevo, "content-type": "application/json", "accept": "application/json"},
            json={"sender": {"email": remitente, "name": "Buscador de Licitaciones"},
                  "to": [{"email": d} for d in destinos], "subject": asunto, "htmlContent": cuerpo_html},
            timeout=60,
        )
        if r.status_code >= 300:
            raise RuntimeError(f"Brevo rechazó el envío: {r.status_code} {r.text[:300]}")
        print(f"Correo enviado por Brevo a {destinos}")
        return True
    usuario, clave = os.environ.get("GMAIL_USUARIO"), os.environ.get("GMAIL_APP_PASSWORD")
    if usuario and clave:
        msg = MIMEMultipart("alternative")
        msg["Subject"], msg["From"], msg["To"] = asunto, f"Buscador de Licitaciones <{usuario}>", ", ".join(destinos)
        msg.attach(MIMEText(cuerpo_html, "html", "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=60) as s:
            s.login(usuario, clave.replace(" ", ""))
            s.sendmail(usuario, destinos, msg.as_string())
        print(f"Correo enviado por Gmail a {destinos}")
        return True
    print("No hay credenciales de correo (Gmail o Brevo): no se envía correo.")
    return False


_DEP_COMPACTO = {k.replace(" ", ""): v for k, v in DEPARTAMENTOS.items()}
_PROV_COMPACTO = {k.replace(" ", ""): v for k, v in PROVINCIAS.items()}


# Siglas de gobiernos regionales que aparecen en la nomenclatura de los procesos OxI
SIGLAS_GORE = {r"grsm": "San Martín", r"grll": "La Libertad", r"gr\.?\s?lamb": "Lambayeque", r"grl": "Lima",
               r"gr\.?\s?cusco|grc": "Cusco", r"gr\.?\s?ica|grica": "Ica", r"grmoq|gr\.?\s?moquegua": "Moquegua",
               r"grtacna|gr\.?\s?tacna": "Tacna", r"grpiura|gr\.?\s?piura": "Piura", r"grpuno|gr\.?\s?puno": "Puno",
               r"grjunin|gr\.?\s?junin": "Junín", r"grucayali|gr\.?\s?ucayali|gru": "Ucayali",
               r"grancash|gr\.?\s?ancash": "Áncash", r"grcaj|gr\.?\s?cajamarca": "Cajamarca", r"grhco": "Huánuco"}
RE_SIGLAS_GORE = [(re.compile(r"(?<![a-z])(?:" + k + r")(?![a-z])"), v) for k, v in SIGLAS_GORE.items()]


def departamento_aviso(texto_n, pistas=()):
    """Región de un aviso: primero lo explícito ("departamento de…", "provincia de…"),
    luego la entidad dueña de la página (regioncusco, munipisco) y por último el título."""
    dep = ubicar({}, texto_n, "")[0]
    if dep:
        return dep
    for rx, nombre in RE_SIGLAS_GORE:
        if rx.search(texto_n):
            return nombre
    for pista in pistas:
        p = normalizar(pista)
        if not p:
            continue
        compacto = p.replace(" ", "")
        dep = _DEP_COMPACTO.get(compacto) or _PROV_COMPACTO.get(compacto)
        if not dep:
            dep = ubicar({}, p, p)[0] or _departamento_en(re.sub(r"[/\-_.]", " ", p))
        if dep:
            return dep
    return None


def revisar_avisos(config, estado, inicio):
    """Lee las fuentes de la Fase 2 sin poner en riesgo la Fase 1 si algo falla."""
    if not config.get("fuentes_avisos"):
        return [], ""
    ruta = os.path.join(RAIZ, "docs", "avisos.json")
    almacen = {a["id"]: a for a in leer_json(ruta, {"avisos": []}).get("avisos", [])}
    try:
        import avisos as AV
        print("Avisos (Obras por Impuestos, ANIN, PEIP):")
        nuevos, _ = AV.revisar(config, almacen, estado, nueva_sesion(), normalizar, departamento_aviso, inicio)
        lista = sorted(almacen.values(), key=lambda a: a.get("fecha") or "", reverse=True)
        guardar_json(ruta, {"actualizado": iso(inicio), "total": len(lista), "avisos": lista}, compacto=True)
        print(f"Avisos nuevos para correo: {len(nuevos)} | total avisos en buscador: {len(lista)}")
        return nuevos, AV.html_correo(nuevos)
    except Exception as e:  # noqa: BLE001
        print("AVISO: falló la revisión de avisos (la búsqueda del SEACE sigue normal):", e)
        return [], ""


def url_del_buscador():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        duenio, nombre = repo.split("/", 1)
        return f"https://{duenio.lower()}.github.io/{nombre}/"
    return "(configura GitHub Pages)"


# ----------------------------------------------------------------------------
# Principal
# ----------------------------------------------------------------------------
def main():
    config = leer_json(RUTA_CONFIG, {})
    estado = leer_json(RUTA_ESTADO, {})
    almacen = leer_json(RUTA_DATOS, {"registros": []})
    registros = {r["id"]: r for r in almacen.get("registros", [])}
    especialidades_rx = compilar_especialidades(config)
    # Recalcular provincia/distrito de lo ya guardado con las reglas actuales (es rápido)
    for r in registros.values():
        _, prov, dist = ubicar({}, normalizar(f"{r.get('titulo', '')} {r.get('descripcion', '')}"),
                               normalizar(r.get("entidad")))
        r["provincia"], r["distrito"] = prov, dist
    excluidas_rx = [regex_palabra(p) for p in config.get("palabras_excluidas", [])]

    inicio = ahora()
    ultima = parse_fecha(estado.get("ultima_ejecucion_ok"))
    primera_vez = ultima is None or not registros
    if primera_vez:
        # El buscador arranca con varias semanas de historia; el correo solo con lo más reciente
        desde = inicio - dt.timedelta(days=int(config.get("dias_carga_inicial_buscador", 45)))
        correo_desde = inicio - dt.timedelta(days=int(config.get("dias_primera_carga", 3)))
    else:
        # Margen de 3 días por si el OECE publica con retraso (lo ya visto no se repite)
        desde = correo_desde = ultima - dt.timedelta(days=3)
    diagnostico = primera_vez or os.environ.get("DIAGNOSTICO") == "1"

    try:
        releases = descargar(desde, diagnostico)
    except RuntimeError as e:
        estado["fallas_seguidas"] = int(estado.get("fallas_seguidas", 0)) + 1
        estado["ultimo_error"] = f"{iso(inicio)} {e}"
        guardar_json(RUTA_ESTADO, estado)
        print("ERROR:", e)
        if estado["fallas_seguidas"] == 2:
            try:
                enviar_correo("⚠️ El buscador de licitaciones no pudo leer el SEACE",
                              f"<p>Dos ejecuciones seguidas fallaron.</p><pre>{html.escape(str(e))}</pre>"
                              "<p>Si sigue fallando, revisa la pestaña Actions de tu repositorio.</p>")
            except Exception as e2:  # noqa: BLE001
                print("Tampoco se pudo enviar el aviso:", e2)
        sys.exit(1)

    nuevos = []
    for rel in releases:
        reg = convertir(rel, especialidades_rx)
        if not reg or not reg["id"]:
            continue
        previo = registros.get(reg["id"])
        if previo:
            # Actualizar con la versión más reciente, conservando cuándo lo vimos y lo leído de las bases
            if (reg.get("actualizado") or "") >= (previo.get("actualizado") or "") or "consultas_fin" not in previo:
                reg["visto"] = previo.get("visto")
                if reg.get("bases_url") and reg["bases_url"] != previo.get("bases_url"):
                    previo = {**previo, "cronograma_revisado": False}  # salieron bases nuevas (p. ej. integradas)
                registros[reg["id"]] = {**previo, **{k: v for k, v in reg.items() if v not in (None, "", [])}}
        elif (parse_fecha(reg.get("publicado")) or inicio) < desde - dt.timedelta(days=int(config.get("dias_carga_inicial_buscador", 45))):
            continue  # muy antiguo para agregarlo ahora
        else:
            reg["visto"] = iso(inicio)
            registros[reg["id"]] = reg
            if (parse_fecha(reg.get("publicado")) or inicio) >= correo_desde:
                nuevos.append(reg)

    # Fechas reales del procedimiento (consultas, propuestas, buena pro) y etapa actual
    for r in registros.values():
        completar_fechas(r, inicio)
    leer_cronogramas(registros, {r["id"] for r in nuevos}, config, inicio)

    # Historial: descartar lo muy antiguo
    limite = inicio - dt.timedelta(days=int(config.get("dias_historial", 365)))
    registros = {k: v for k, v in registros.items()
                 if (parse_fecha(v.get("publicado")) or inicio) >= limite}
    lista = sorted(registros.values(), key=lambda r: r.get("publicado") or "", reverse=True)
    guardar_json(RUTA_DATOS, {"actualizado": iso(inicio), "total": len(lista), "registros": lista}, compacto=True)

    prioritarias_set = set(config.get("regiones_prioritarias", []))
    para_correo = [r for r in nuevos if pasa_filtro_correo(r, config, excluidas_rx)]
    clave_orden = lambda r: (-(r["monto"] or 0))  # noqa: E731
    prioritarias = sorted([r for r in para_correo if r.get("departamento") in prioritarias_set], key=clave_orden)
    resto = sorted([r for r in para_correo if r.get("departamento") not in prioritarias_set], key=clave_orden)
    print(f"Procesos nuevos de obra/supervisión/consultoría: {len(nuevos)} | para correo: "
          f"{len(prioritarias)} prioritarias + {len(resto)} resto | total en buscador: {len(lista)}")

    # Fase 2: avisos de Obras por Impuestos, ANIN, PEIP y programas especiales
    avisos_nuevos, avisos_html = revisar_avisos(config, estado, inicio)

    if prioritarias or resto or avisos_nuevos:
        asunto, cuerpo = armar_correo(prioritarias, resto, config, url_del_buscador(), avisos_html, len(avisos_nuevos))
        enviar_correo(asunto, cuerpo)

    estado.update({"ultima_ejecucion_ok": iso(inicio), "fallas_seguidas": 0,
                   "ultimo_resumen": f"{len(nuevos)} nuevos, {len(prioritarias)} prioritarias"})
    guardar_json(RUTA_ESTADO, estado)


if __name__ == "__main__":
    main()
