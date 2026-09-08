# api_generador.py
import io
import json
import os
import re
from fastapi import APIRouter, HTTPException
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
from pydantic import BaseModel
import pandas as pd

router = APIRouter()

FOLDER_ID_DEFAULT = "1nK7_foRIcGb9oasij7spOn0kOLQHmVYb"

def get_drive_service():
    creds_raw = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    creds_dict = json.loads(creds_raw)
    creds = service_account.Credentials.from_service_account_info(
        creds_dict, scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)

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
    urgente: bool
    garantia: bool
    mantenimiento: bool
    instalacion: bool
    revisar: bool

@router.get("/clientes-maquinas")
def listar_clientes_maquinas():
    try:
        drive = get_drive_service()
        
        query = f"'{FOLDER_ID_DEFAULT}' in parents and name = 'Clientes_maquinas.xlsx' and trashed = false"
        res = drive.files().list(q=query, fields="files(id, mimeType)").execute()
        archivos = res.get("files", [])
        
        if not archivos:
            return {"error": "No se encontró el archivo Clientes_maquinas.xlsx en Google Drive"}
            
        file_id = archivos[0]['id']
        mime_type = archivos[0].get("mimeType")

        if mime_type == "application/vnd.google-apps.spreadsheet":
            request = drive.files().export_media(
                fileId=file_id,
                mimeType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
        else:
            request = drive.files().get_media(fileId=file_id)

        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        fh.seek(0)

        df = pd.read_excel(fh, header=4)
        df.columns = [str(col).replace("\u00a0", " ").strip() for col in df.columns]

        if "CLIENTE" not in df.columns:
            return {"error": "El Excel no contiene la columna CLIENTE"}

        idx_cliente = list(df.columns).index("CLIENTE")
        idx_poblacion = idx_cliente + 3
        
        col_serie = None
        if "MODELO" in df.columns:
            idx_modelo = list(df.columns).index("MODELO")
            idx_serie = idx_modelo + 2
            if idx_serie < len(df.columns):
                col_serie = df.columns[idx_serie]

        def clean(value):
            if pd.isna(value):
                return ""
            return str(value).replace("\u00a0", " ").strip()

        clientes_map = {}

        def normalize_key(value):
            text = clean(value)
            text = text.replace("\u200b", "").replace("\ufeff", "")
            text = " ".join(text.split())
            import unicodedata
            text = unicodedata.normalize("NFKD", text)
            text = "".join(ch for ch in text if not unicodedata.combining(ch))
            text = re.sub(r'[^a-zA-Z0-9\s]', '', text)
            return text.casefold()

        for _, row in df.iterrows():
            if idx_cliente >= len(row): continue
            
            c = clean(row.iloc[idx_cliente])
            if not c:
                continue

            key = normalize_key(c)
            
            poblacion = ""
            if idx_poblacion < len(row):
                poblacion = clean(row.iloc[idx_poblacion])
            
            if not poblacion and "POBLACIÓN" in df.columns:
                poblacion = clean(row.get("POBLACIÓN", ""))

            n = clean(row.get("NOMBRE", ""))
            equipo = clean(row.get("EQUIPO", ""))
            marca = clean(row.get("MARCA", ""))
            modelo = clean(row.get("MODELO", ""))
            n_serie = clean(row.get(col_serie, "")) if col_serie else ""
            f_garan = clean(row.get("F.GARAN.", ""))
            f_instal = clean(row.get("F. INSTALACION", ""))

            if not n:
                n = f"{equipo} {marca} {modelo}".strip()

            if key not in clientes_map:
                clientes_map[key] = {
                    "nombre": c,
                    "poblacion": poblacion,
                    "maquinas": []
                }
            elif poblacion and not clientes_map[key]["poblacion"]:
                clientes_map[key]["poblacion"] = poblacion

            if n and not any(m["nombre"] == n for m in clientes_map[key]["maquinas"]):
                clientes_map[key]["maquinas"].append({
                    "nombre": n,
                    "equipo": equipo,
                    "marca": marca,
                    "modelo": modelo,
                    "n_serie": n_serie,
                    "f_garan": f_garan[:10] if f_garan else "",
                    "f_instal": f_instal[:10] if f_instal else ""
                })

        return clientes_map

    except Exception as e:
        return {"error": str(e)}


@router.post("/avisos")
def crear_aviso(aviso: NuevoAviso):
    try:
        drive = get_drive_service()
        query = f"'{FOLDER_ID_DEFAULT}' in parents and name = 'Avisos Sin Tratar.xlsx' and trashed = false"
        res = drive.files().list(q=query, fields="files(id)").execute()
        archivos = res.get("files", [])
        
        cols = ['F. ENTR.', 'CLIENTE', 'POBLACIÓN', 'MÁQUINA', 'EQUIPO', 'MARCA', 'MODELO', 'Nº SERIE', 'F. GARANTÍA', 'F. INSTALACIÓN', 'DESCRIPCIÓN', 'REALIZADO POR', 'URGENTE', 'PIRINEOS', 'GARANTÍA', 'MANTENIMIENTO', 'INSTALACIÓN', 'REVISAR']
        if archivos:
            file_id = archivos[0]['id']
            request = drive.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done: _, done = downloader.next_chunk()
            fh.seek(0)
            df = pd.read_excel(fh)
        else:
            file_id = None
            df = pd.DataFrame(columns=cols)

        if 'PIRINEOS' not in df.columns: df['PIRINEOS'] = 'NO'
        if 'REALIZADO POR' not in df.columns: df['REALIZADO POR'] = 'Pendiente'
        if 'GARANTÍA' not in df.columns: df['GARANTÍA'] = 'NO'
        if 'MANTENIMIENTO' not in df.columns: df['MANTENIMIENTO'] = 'NO'
        if 'INSTALACIÓN' not in df.columns: df['INSTALACIÓN'] = 'NO'
        if 'REVISAR' not in df.columns: df['REVISAR'] = 'NO'
        
        fila_excel = {
            'F. ENTR.': aviso.fecha_entrada, 'CLIENTE': aviso.cliente, 'POBLACIÓN': aviso.poblacion,
            'MÁQUINA': aviso.maquina, 'EQUIPO': aviso.equipo, 'MARCA': aviso.marca, 'MODELO': aviso.modelo,
            'Nº SERIE': aviso.n_serie, 'F. GARANTÍA': aviso.f_garan, 'F. INSTALACIÓN': aviso.f_instal, 'DESCRIPCIÓN': aviso.descripcion,
            'REALIZADO POR': 'Pendiente', 'PIRINEOS': 'NO',
            'URGENTE': 'SI' if aviso.urgente else 'NO', 
            'GARANTÍA': 'SI' if aviso.garantia else 'NO',
            'MANTENIMIENTO': 'SI' if aviso.mantenimiento else 'NO',
            'INSTALACIÓN': 'SI' if aviso.instalacion else 'NO',
            'REVISAR': 'SI' if aviso.revisar else 'NO'
        }
        
        df = pd.concat([df, pd.DataFrame([fila_excel])], ignore_index=True)
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer: df.to_excel(writer, index=False)
            
        media = MediaIoBaseUpload(io.BytesIO(output.getvalue()), mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        
        if file_id:
            drive.files().update(fileId=file_id, media_body=media).execute()
        else:
            drive.files().create(body={'name': 'Avisos Sin Tratar.xlsx', 'parents': [FOLDER_ID_DEFAULT]}, media_body=media).execute()
            
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
