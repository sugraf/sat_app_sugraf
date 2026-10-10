# generador/api_generador.py
import io
import json
import os
import re
import sys
import unicodedata
from datetime import date, datetime, time
from fastapi import APIRouter, HTTPException
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
from pydantic import BaseModel
import pandas as pd

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)
from logger_movimientos import registrar_movimiento
from aviso_matcher import existe_aviso_duplicado

router = APIRouter()

FOLDER_ID_DEFAULT = "1nK7_foRIcGb9oasij7spOn0kOLQHmVYb"

# Mi unidad/Pirineos/Maquinas por cliente/Maquinas.xlsx: clientes, máquinas, fechas y emails.
MAQUINAS_FOLDER_ID = "10XZG9Am4EjzO3vRIk36h50Uzjsku4USt"
MAQUINAS_FICHERO = "Maquinas.xlsx"
MAQUINAS_HOJA = "ConsultaMaquinas"

def get_drive_service():
    creds_raw = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    creds_dict = json.loads(creds_raw)
    creds = service_account.Credentials.from_service_account_info(
        creds_dict, scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)

class NuevoAviso(BaseModel):
    fecha_entrada: str
    cliente: str
    poblacion: str
    maquina: str
    equipo: str
    marca: str
    modelo: str
    n_serie: str
    f_garan: str
    f_instal: str
    descripcion: str
    email: str = ''
    urgente: bool
    garantia: bool
    mantenimiento: bool
    instalacion: bool
    revisar: bool
    nuevo: bool
    parado: bool
    reclama: bool
    presupuesto: bool
    piezas: bool

@router.get("/clientes-maquinas")
def listar_clientes_maquinas():
    try:
        return leer_clientes_maquinas(get_drive_service())
    except Exception as e:
        return {"error": str(e)}

def leer_clientes_maquinas(drive):
    """Descarga Maquinas.xlsx y devuelve las máquinas agrupadas por cliente
    (o {"error": ...} si el Excel no tiene el formato esperado).
    También lo usa el módulo de Manuales."""
    query = f"'{MAQUINAS_FOLDER_ID}' in parents and name = '{MAQUINAS_FICHERO}' and trashed = false"
    res = drive.files().list(
        q=query, fields="files(id, mimeType)",
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    archivos = res.get("files", [])

    if not archivos:
        raise RuntimeError(f"No se encontró el archivo {MAQUINAS_FICHERO} en la carpeta 'Maquinas por cliente' de Google Drive. Comprueba que la carpeta está compartida con la cuenta de servicio de la app.")

    file_id = archivos[0]['id']
    mime_type = archivos[0].get("mimeType")

    if mime_type == "application/vnd.google-apps.spreadsheet":
        request = drive.files().export_media(
            fileId=file_id,
            mimeType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    else:
        request = drive.files().get_media(fileId=file_id, supportsAllDrives=True)

    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    fh.seek(0)

    return construir_clientes_maquinas(fh)

def _quitar_acentos(texto):
    texto = unicodedata.normalize("NFKD", texto)
    return "".join(ch for ch in texto if not unicodedata.combining(ch))

def _clean(value):
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return ""
    return str(value).replace("\u00a0", " ").strip()

def _normalize_key(value):
    text = _clean(value)
    text = text.replace("\u200b", "").replace("\ufeff", "")
    text = " ".join(text.split())
    text = _quitar_acentos(text)
    text = re.sub(r'[^a-zA-Z0-9\s]', '', text)
    return text.casefold()

def _fecha(value):
    """Fecha en formato AAAA-MM-DD. Las celdas vacías llegan como hora 00:00 o NaT."""
    if isinstance(value, (datetime, date)):
        return "" if pd.isna(value) else value.strftime("%Y-%m-%d")
    if isinstance(value, time):
        return ""
    return _clean(value)[:10]

def _emails(value):
    return [e for e in re.split(r"[;,\s]+", _clean(value)) if "@" in e]

def construir_clientes_maquinas(fichero):
    """Lee Maquinas.xlsx (hoja ConsultaMaquinas) y agrupa las máquinas por cliente."""
    hojas = pd.read_excel(fichero, sheet_name=None, dtype=object)
    df = hojas.get(MAQUINAS_HOJA)
    if df is None:
        df = next(iter(hojas.values()))

    # Columnas por nombre normalizado, por si cambian mayúsculas, acentos o guiones bajos.
    columnas = {re.sub(r'[^a-z0-9]', '', _quitar_acentos(str(c)).casefold()): c for c in df.columns}

    def col(row, nombre):
        c = columnas.get(nombre)
        return row[c] if c is not None else None

    if "nombre" not in columnas:
        return {"error": f"El Excel {MAQUINAS_FICHERO} no contiene la columna Nombre (cliente)"}

    clientes_map = {}

    for _, row in df.iterrows():
        c = _clean(col(row, "nombre"))
        if not c:
            continue

        key = _normalize_key(c)
        poblacion = _clean(col(row, "poblacion"))
        emails = _emails(col(row, "email"))

        if key not in clientes_map:
            clientes_map[key] = {
                "nombre": c,
                "poblacion": poblacion,
                "email": "",
                "maquinas": []
            }
        cliente = clientes_map[key]
        if poblacion and not cliente["poblacion"]:
            cliente["poblacion"] = poblacion
        actuales = [e for e in cliente["email"].split(", ") if e]
        for e in emails:
            if e.casefold() not in (a.casefold() for a in actuales):
                actuales.append(e)
        cliente["email"] = ", ".join(actuales)

        # Las máquinas dadas de baja no se ofrecen.
        if _quitar_acentos(_clean(col(row, "baja"))).casefold().startswith("s"):
            continue

        equipo = _clean(col(row, "equipo"))
        marca = _clean(col(row, "marca"))
        modelo = _clean(col(row, "modelo"))
        n = _clean(col(row, "nombremaquina")) or f"{equipo} {marca} {modelo}".strip()

        if n and not any(m["nombre"] == n for m in cliente["maquinas"]):
            cliente["maquinas"].append({
                "nombre": n,
                "equipo": equipo,
                "marca": marca,
                "modelo": modelo,
                "n_serie": _clean(col(row, "nserie")),
                "f_garan": _fecha(col(row, "fechagarantia")),
                "f_instal": _fecha(col(row, "fechainstalacion"))
            })

    return clientes_map

def _descargar_excel_por_nombre(drive, nombre):
    query = f"'{FOLDER_ID_DEFAULT}' in parents and name = '{nombre}' and trashed = false"
    res = drive.files().list(q=query, fields="files(id)").execute()
    archivos = res.get("files", [])

    if not archivos:
        return None, pd.DataFrame()

    file_id = archivos[0]['id']
    request = drive.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done: _, done = downloader.next_chunk()
    fh.seek(0)
    return file_id, pd.read_excel(fh)

@router.post("/avisos")
def crear_aviso(aviso: NuevoAviso):
    try:
        drive = get_drive_service()

        cols = ['F. ENTR.', 'CLIENTE', 'POBLACIÓN', 'MÁQUINA', 'EQUIPO', 'MARCA', 'MODELO', 'Nº SERIE', 'F. GARANTÍA', 'F. INSTALACIÓN', 'DESCRIPCIÓN', 'ASIGNADO A', 'URGENTE', 'PIRINEOS', 'GARANTÍA', 'MANTENIMIENTO', 'INSTALACIÓN', 'REVISAR', 'NUEVO', 'PARADO', 'RECLAMA', 'PRESUPUESTO', 'PIEZAS', 'ESTADO PIEZAS', 'OBSERVACIONES', 'EMAIL CLIENTE']
        file_id, df = _descargar_excel_por_nombre(drive, 'Avisos Sin Tratar.xlsx')

        for col in cols:
            if col not in df.columns:
                df[col] = ''

        _, df_tratados = _descargar_excel_por_nombre(drive, 'Avisos Tratados.xlsx')
        if existe_aviso_duplicado([df, df_tratados], aviso.cliente, aviso.maquina, aviso.descripcion, aviso.fecha_entrada):
            raise HTTPException(
                status_code=400,
                detail="Ya existe un aviso con esa misma descripción para este cliente, máquina y fecha. Por favor, modifica algo en la descripción para poder diferenciarlos."
            )

        fila_excel = {
            'F. ENTR.': aviso.fecha_entrada, 'CLIENTE': aviso.cliente, 'POBLACIÓN': aviso.poblacion,
            'MÁQUINA': aviso.maquina, 'EQUIPO': aviso.equipo, 'MARCA': aviso.marca, 'MODELO': aviso.modelo,
            'Nº SERIE': aviso.n_serie, 'F. GARANTÍA': aviso.f_garan, 'F. INSTALACIÓN': aviso.f_instal, 'DESCRIPCIÓN': aviso.descripcion,
            'EMAIL CLIENTE': aviso.email.strip(),
            'ASIGNADO A': 'Pendiente', 'PIRINEOS': 'NO',
            'URGENTE': 'SI' if aviso.urgente else 'NO', 
            'GARANTÍA': 'SI' if aviso.garantia else 'NO',
            'MANTENIMIENTO': 'SI' if aviso.mantenimiento else 'NO',
            'INSTALACIÓN': 'SI' if aviso.instalacion else 'NO',
            'REVISAR': 'SI' if aviso.revisar else 'NO',
            'NUEVO': 'SI' if aviso.nuevo else 'NO',
            'PARADO': 'SI' if aviso.parado else 'NO',
            'RECLAMA': 'SI' if aviso.reclama else 'NO',
            'PRESUPUESTO': 'SI' if aviso.presupuesto else 'NO',
            'PIEZAS': 'SI' if aviso.piezas else 'NO',
            'ESTADO PIEZAS': 'Pendiente' if aviso.piezas else ''
        }
        
        df = pd.concat([df, pd.DataFrame([fila_excel])], ignore_index=True)
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer: df.to_excel(writer, index=False)
            
        media = MediaIoBaseUpload(io.BytesIO(output.getvalue()), mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        
        if file_id:
            drive.files().update(fileId=file_id, media_body=media).execute()
        else:
            drive.files().create(body={'name': 'Avisos Sin Tratar.xlsx', 'parents': [FOLDER_ID_DEFAULT]}, media_body=media).execute()
            
        registrar_movimiento("creacion", fila_excel)
        return {"status": "ok"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
