"""Prueba sin internet: simula releases OCDS y verifica clasificación, filtros y correo.
Uso: python pruebas/prueba_local.py   (no toca tus datos reales)"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "buscador"))
import licitaciones as L  # noqa: E402

HOY = L.ahora()
MANANA = (HOY + L.dt.timedelta(days=10)).isoformat()
AYER = (HOY - L.dt.timedelta(days=2)).isoformat()


def rel(ocid, cat, titulo, desc, entidad, monto, cierre=MANANA, region=None, status="active"):
    return {
        "ocid": ocid, "date": HOY.isoformat(), "tag": ["tender"],
        "buyer": {"id": "B1", "name": entidad},
        "parties": [{"id": "B1", "name": entidad, "roles": ["buyer"], "address": {"region": region}}],
        "tender": {"title": titulo, "description": desc, "mainProcurementCategory": cat,
                   "procurementMethodDetails": "Licitación Pública Abreviada", "status": status,
                   "value": {"amount": monto, "currency": "PEN"},
                   "tenderPeriod": {"startDate": HOY.isoformat(), "endDate": HOY.isoformat()},
                   # el plazo real: propuestas ~6 días después del fin de consultas
                   "enquiryPeriod": {"endDate": (L.dt.datetime.fromisoformat(cierre) - L.dt.timedelta(days=6)).isoformat()},
                   "datePublished": HOY.isoformat()},
    }


CASOS = [
    rel("ocds-1", "works", "LP-SM-1-2026-MPP/CS-1",
        "MEJORAMIENTO DE PISTAS Y VEREDAS DE LA AV. LEGUIA, DISTRITO DE PISCO - PROVINCIA DE PISCO - DEPARTAMENTO DE ICA",
        "MUNICIPALIDAD PROVINCIAL DE PISCO", 2500000),
    rel("ocds-2", "services", "CP-SM-3-2026-GRA-1",
        "SUPERVISION DE LA EJECUCION DE LA OBRA: CREACION DEL SERVICIO DE AGUA POTABLE EN EL DISTRITO DE COCACHACRA, PROVINCIA DE ISLAY",
        "GOBIERNO REGIONAL DE AREQUIPA", 180000),
    rel("ocds-3", "goods", "AS-1", "ADQUISICION DE CEMENTO PORTLAND TIPO I", "MUNICIPALIDAD DISTRITAL DE SAN ANDRES", 90000),
    rel("ocds-4", "works", "LP-2", "MEJORAMIENTO DEL SERVICIO EDUCATIVO DE LA I.E. 123, DISTRITO DE JULIACA, PROVINCIA DE SAN ROMAN, DEPARTAMENTO DE PUNO",
        "PRONIED", 5000000, region="Lima"),
    rel("ocds-5", "services", "CP-4", "ELABORACION DEL EXPEDIENTE TECNICO DEL PROYECTO MEJORAMIENTO DEL CANAL DE RIEGO LA CANTUTA",
        "MUNICIPALIDAD DISTRITAL DE HUMAY", 60000, region="Ica"),
    rel("ocds-6", "works", "LP-5", "CONSTRUCCION DE HOSPITAL REGIONAL", "GOBIERNO REGIONAL DE PIURA", 95000000),  # monto fuera de rango
    rel("ocds-7", "works", "AS-6", "MEJORAMIENTO DE LOSA DEPORTIVA EN CHINCHA ALTA", "MUNICIPALIDAD PROVINCIAL DE CHINCHA", 400000, cierre=AYER),  # vencida
    rel("ocds-8", "services", "AS-7", "SERVICIO DE LIMPIEZA DE AMBIENTES", "MUNICIPALIDAD DE ICA", 50000),
]


def main():
    tmp = tempfile.mkdtemp()
    L.RUTA_DATOS = os.path.join(tmp, "docs", "datos.json")
    L.RUTA_ESTADO = os.path.join(tmp, "estado.json")
    L.RUTA_MUESTRA = os.path.join(tmp, "muestra.json")
    enviados = []
    L.descargar = lambda *a, **k: CASOS
    L.enviar_correo = lambda asunto, cuerpo: enviados.append((asunto, cuerpo)) or True
    L.main()

    datos = L.leer_json(L.RUTA_DATOS, {})
    regs = {r["id"]: r for r in datos["registros"]}
    def ok(cond, msg):
        print(("OK   " if cond else "FALLA"), msg)
        assert cond, msg

    ok(set(regs) == {"ocds-1", "ocds-2", "ocds-4", "ocds-5", "ocds-6", "ocds-7"}, "guarda solo obras/supervisión/consultoría")
    ok(regs["ocds-1"]["tipo"] == "Obra" and regs["ocds-1"]["departamento"] == "Ica", "obra Pisco → Ica")
    ok(regs["ocds-1"]["provincia"] == "Pisco" and regs["ocds-1"]["distrito"] == "Pisco", "provincia/distrito Pisco")
    ok("Pistas y veredas / vías urbanas" in regs["ocds-1"]["especialidades"], "especialidad pistas y veredas")
    ok(regs["ocds-2"]["tipo"] == "Supervisión" and regs["ocds-2"]["departamento"] == "Arequipa", "supervisión Arequipa")
    ok("Agua y saneamiento" in regs["ocds-2"]["especialidades"], "especialidad agua")
    ok(regs["ocds-4"]["departamento"] == "Puno", "PRONIED en Lima pero obra en Puno → Puno")
    ok(regs["ocds-5"]["tipo"] == "Consultoría de obra" and regs["ocds-5"]["departamento"] == "Ica", "expediente técnico Humay → Ica por dirección")
    ok("ocds-8" not in regs, "limpieza descartada")
    ok(len(enviados) == 1, "se envía un solo correo")
    cuerpo = enviados[0][1]
    ok("PISTAS Y VEREDAS" in cuerpo and "COCACHACRA" in cuerpo and "JULIACA" in cuerpo, "correo incluye las válidas")
    ok("HOSPITAL REGIONAL" not in cuerpo, "excluye monto fuera de rango del correo")
    ok("LOSA DEPORTIVA" not in cuerpo, "excluye vencidas del correo")
    print("Asunto:", enviados[0][0])

    # Segunda ejecución: nada nuevo → no hay correo
    enviados.clear()
    L.main()
    ok(len(enviados) == 0, "segunda ejecución sin duplicados")
    with open(os.path.join(tmp, "correo_ejemplo.html"), "w", encoding="utf-8") as fh:
        fh.write(cuerpo)
    print("Correo de ejemplo guardado en", os.path.join(tmp, "correo_ejemplo.html"))
    print("\nTODO OK")


if __name__ == "__main__":
    main()
