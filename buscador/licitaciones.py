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

API_INICIO = "https://contratacionesabiertas.oece.gob.pe/api/v1/releasesAfter?format=json&order=desc"
URL_PROCESO = "https://contratacionesabiertas.oece.gob.pe/proceso/{ocid}"
LIMA_TZ = dt.timezone(dt.timedelta(hours=-5))
ESTADOS_CERRADOS = {"complete", "cancelled", "unsuccessful", "withdrawn"}


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
_SEP = r"(?:\s*[-,/;:]|\s+(?:provincia|departamento|region|distrito|y)\b|\s*$|\s*\()"
RE_DEP_TXT = re.compile(r"(?:departamento|region|gobierno regional)\s+(?:de\s+|del\s+)?([a-z ]{3,25}?)" + _SEP)
RE_PROV_TXT = re.compile(r"(?:provincia|municipalidad provincial)\s+(?:de\s+|del\s+)?([a-z ]{3,30}?)" + _SEP)
RE_DIST_TXT = re.compile(r"(?:distrito|municipalidad distrital)\s+(?:de\s+|del\s+)?([a-z ]{3,30}?)" + _SEP)


def _departamento_en(texto_n):
    texto_n = texto_n.strip()
    if texto_n in DEPARTAMENTOS:
        return DEPARTAMENTOS[texto_n]
    for clave, nombre in DEPARTAMENTOS.items():
        if re.search(r"(?<![a-z])" + clave + r"(?![a-z])", texto_n):
            return nombre
    return None


def _titulo(s):
    return " ".join(w.capitalize() if w not in ("de", "del", "la", "las", "los", "y") else w
                    for w in s.split()) if s else None


def ubicar(release, texto_n, entidad_n):
    """Devuelve (departamento, provincia, distrito). Prioriza lo que dice el nombre del
    proyecto (dónde se hace la obra) sobre la dirección de la entidad (dónde está la oficina)."""
    dep = prov = dist = None
    m = RE_DEP_TXT.search(texto_n)
    if m:
        dep = _departamento_en(m.group(1))
    m = RE_PROV_TXT.search(texto_n) or RE_PROV_TXT.search(entidad_n)
    if m:
        prov = m.group(1).strip()
    m = RE_DIST_TXT.search(texto_n) or RE_DIST_TXT.search(entidad_n)
    if m:
        dist = m.group(1).strip()
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
    ocid = release.get("ocid") or release.get("id")
    titulo = tender.get("title") or ""
    descripcion = tender.get("description") or ""
    if not descripcion and tender.get("items"):
        descripcion = (tender["items"][0] or {}).get("description") or ""
    return {
        "id": ocid,
        "fuente": "SEACE",
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
        "estado": tender.get("status") or "",
        "publicado": iso(parse_fecha(tender.get("datePublished")) or parse_fecha(release.get("date"))),
        "cierre": iso(parse_fecha(periodo.get("endDate"))),
        "actualizado": iso(parse_fecha(release.get("date"))),
        "url": URL_PROCESO.format(ocid=ocid),
    }


# ----------------------------------------------------------------------------
# Descarga
# ----------------------------------------------------------------------------
CABECERAS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-PE,es;q=0.9",
    "Referer": "https://contratacionesabiertas.oece.gob.pe/",
}


def pedir(sesion, url, intentos=3):
    """Pide una página de la API; deja en el registro la respuesta exacta si falla."""
    variantes = [url]
    ultimo = None
    for variante in variantes:
        for i in range(intentos):
            try:
                r = sesion.get(variante, timeout=60)
                if r.status_code == 200:
                    return r.json()
                ultimo = (f"HTTP {r.status_code} | servidor: {r.headers.get('server', '?')} | "
                          f"respuesta: {r.text[:250].strip()!r}")
                print(f"  intento {i + 1} en {variante[:70]}… → {ultimo}")
                if r.status_code in (401, 403):
                    break  # bloqueo: no insistir con la misma dirección
            except (requests.RequestException, ValueError) as e:
                ultimo = str(e)[:250]
                print(f"  intento {i + 1} → error: {ultimo}")
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"La API del OECE no respondió ({ultimo}) en {url}")


def descargar(desde, max_paginas, pausa, diagnostico=False):
    sesion = requests.Session()
    sesion.headers.update(CABECERAS)
    url, paginas, viejas_seguidas = API_INICIO, 0, 0
    releases, muestra, mas_antigua = [], None, None
    while url and paginas < max_paginas:
        data = pedir(sesion, url.replace(".osce.", ".oece."))
        paginas += 1
        if muestra is None:
            muestra = data
        lote = data.get("releases") or data.get("results") or []
        recientes = 0
        for rel in lote:
            f = parse_fecha(rel.get("date"))
            if f and (mas_antigua is None or f < mas_antigua):
                mas_antigua = f
            if f is None or f >= desde:
                releases.append(rel)
                recientes += 1
        viejas_seguidas = viejas_seguidas + 1 if (lote and recientes == 0) else 0
        if viejas_seguidas >= 3 or not lote:
            break
        url = (data.get("links") or {}).get("next")
        time.sleep(pausa)
    print(f"Páginas leídas: {paginas} | releases desde {iso(desde)}: {len(releases)} | "
          f"fecha más antigua alcanzada: {iso(mas_antigua)}")
    if paginas >= max_paginas:
        print("AVISO: se llegó al máximo de páginas; sube max_paginas_por_ejecucion en config.json")
    if diagnostico and muestra is not None:
        guardar_diagnostico(muestra, releases)
    return releases


def guardar_diagnostico(muestra, releases):
    corto = dict(muestra)
    if isinstance(corto.get("releases"), list):
        corto["releases"] = corto["releases"][:5]
    guardar_json(RUTA_MUESTRA, corto)
    from collections import Counter
    cats = Counter(normalizar((r.get("tender") or {}).get("mainProcurementCategory")) or "(vacío)" for r in releases)
    tags = Counter(",".join(r.get("tag") or []) for r in releases)
    proc = Counter((r.get("tender") or {}).get("procurementMethodDetails") or "(vacío)" for r in releases)
    print("DIAGNÓSTICO — categorías:", dict(cats.most_common(10)))
    print("DIAGNÓSTICO — tags:", dict(tags.most_common(10)))
    print("DIAGNÓSTICO — procedimientos:", dict(proc.most_common(15)))
    print("DIAGNÓSTICO — claves de primer release:", list((releases[0] if releases else {}).keys()))


# ----------------------------------------------------------------------------
# Filtros de correo
# ----------------------------------------------------------------------------
def pasa_filtro_correo(reg, config, excluidas_rx):
    if reg["tipo"] not in config.get("tipos_para_correo", []):
        return False
    if reg.get("estado") in ESTADOS_CERRADOS:
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
    if reg["monto"] is None:
        return "Monto no indicado"
    simbolo = "S/" if reg.get("moneda") in (None, "", "PEN") else reg["moneda"]
    return f"{simbolo} {reg['monto']:,.0f}"


def fmt_fecha(valor):
    f = parse_fecha(valor)
    return f.strftime("%d/%m/%Y %H:%M") if f else "—"


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
  <div style="color:#374151">📍 {e(lugar)} &nbsp;|&nbsp; 💰 <b>{e(fmt_monto(reg))}</b> &nbsp;|&nbsp; ⏰ Cierre: {e(fmt_fecha(reg.get('cierre')))}</div>
  <div style="font-size:12px;color:#6b7280">{e(reg['titulo'])}</div>
</td></tr>"""


def armar_correo(prioritarias, resto, config, url_buscador):
    max_resto = int(config.get("max_lineas_resto_del_pais", 40))
    regiones = ", ".join(config.get("regiones_prioritarias", []))
    partes = [f"""<html><head><meta charset="utf-8"></head><body><div style="font-family:Arial,sans-serif;max-width:720px">
<h2 style="color:#111827;margin-bottom:4px">🏗️ Licitaciones nuevas — {ahora().strftime('%d/%m/%Y %H:%M')}</h2>
<p style="color:#374151;margin-top:0">{len(prioritarias)} en tus regiones prioritarias ({html.escape(regiones)}) y {len(resto)} en el resto del país.
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
    partes.append('<p style="font-size:12px;color:#6b7280">Fuente: SEACE vía datos abiertos del OECE. Verifica siempre plazos y requisitos en las bases oficiales.</p></div></body></html>')
    asunto = f"🏗️ Licitaciones nuevas: {len(prioritarias)} en tus regiones + {len(resto)} en el resto del país"
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
    excluidas_rx = [regex_palabra(p) for p in config.get("palabras_excluidas", [])]

    inicio = ahora()
    ultima = parse_fecha(estado.get("ultima_ejecucion_ok"))
    primera_vez = ultima is None
    desde = (inicio - dt.timedelta(days=int(config.get("dias_primera_carga", 7)))) if primera_vez \
        else (ultima - dt.timedelta(hours=6))  # solapamiento: lo repetido se descarta
    diagnostico = primera_vez or os.environ.get("DIAGNOSTICO") == "1"

    try:
        releases = descargar(desde, int(config.get("max_paginas_por_ejecucion", 600)),
                             float(config.get("pausa_entre_paginas_seg", 0.4)), diagnostico)
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
            # Actualizar con la versión más reciente, conservando cuándo lo vimos por primera vez
            if (reg.get("actualizado") or "") >= (previo.get("actualizado") or ""):
                reg["visto"] = previo.get("visto")
                registros[reg["id"]] = {k: (v if v not in (None, "", []) else previo.get(k)) for k, v in reg.items()}
        else:
            reg["visto"] = iso(inicio)
            registros[reg["id"]] = reg
            nuevos.append(reg)

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

    if prioritarias or resto:
        asunto, cuerpo = armar_correo(prioritarias, resto, config, url_del_buscador())
        enviar_correo(asunto, cuerpo)

    estado.update({"ultima_ejecucion_ok": iso(inicio), "fallas_seguidas": 0,
                   "ultimo_resumen": f"{len(nuevos)} nuevos, {len(prioritarias)} prioritarias"})
    guardar_json(RUTA_ESTADO, estado)


if __name__ == "__main__":
    main()
