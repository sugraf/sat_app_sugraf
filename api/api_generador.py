import io
import json
import os
from fastapi import APIRouter, HTTPException
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
from pydantic import BaseModel
import pandas as pd

router = APIRouter()

EQUIPOS_FILE_ID = "1mNdXqH6RLwXSIXxd9i3eexOMAkaYJKWN"

def get_drive_service():
    creds_raw = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    creds_dict = json.loads(creds_raw)
    creds = service_account.Credentials.from_service_account_info(
        creds_dict, scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)

def get_target_folder(drive):
    try:
        meta = drive.files().get(fileId=EQUIPOS_FILE_ID, fields="parents").execute()
        if meta.get('parents'):
            return meta['parents'][0]
    except Exception:
        pass
    return "1nK7_foRIcGb9oasij7spOn0kOLQHmVYb"

def procesar_excel_avisos_generador(drive, nuevo_aviso=None):
    folder_id = get_target_folder(drive)
    query = f"'{folder_id}' in parents and name = 'Avisos Sin Tratar.xlsx' and trashed = false"
    res = drive.files().list(q=query, fields="files(id)").execute()
    archivos = res.get("files", [])
    
    cols = ['F. ENTR.', 'CLIENTE', 'POBLACIÓN', 'MÁQUINA', 'EQUIPO', 'MARCA', 'MODELO', 'F. GARANTÍA', 'F. INSTALACIÓN', 'DESCRIPCIÓN', 'REALIZADO POR', 'URGENTE', 'PIRINEOS']
    
    if archivos:
        file_id = archivos[0]['id']
        request = drive.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done: 
            status, done = downloader.next_chunk()
        fh.seek(0)
        df = pd.read_excel(fh)
    else:
        file_id = None
        df = pd.DataFrame(columns=cols)

    if 'PIRINEOS' not in df.columns:
        df['PIRINEOS'] = 'NO'
    df['PIRINEOS'] = df['PIRINEOS'].replace('', 'NO').fillna('NO')
    
    if 'REALIZADO POR' not in df.columns:
        df['REALIZADO POR'] = 'Pendiente'
    df['REALIZADO POR'] = df['REALIZADO POR'].replace('', 'Pendiente').fillna('Pendiente')

    if nuevo_aviso:
        nuevo_df = pd.DataFrame([nuevo_aviso])
        df = pd.concat([df, nuevo_df], ignore_index=True)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False)
        
    media = MediaIoBaseUpload(io.BytesIO(output.getvalue()), mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    
    if file_id:
        drive.files().update(fileId=file_id, media_body=media).execute()
    else:
        meta = {'name': 'Avisos Sin Tratar.xlsx', 'parents': [folder_id]}
        drive.files().create(body=meta, media_body=media, fields='id').execute()
        
    return df.fillna("").to_dict(orient="records")

class NuevoAviso(BaseModel):
    fecha_entrada: str
    cliente: str
    poblacion: str
    maquina: str
    equipo: str
    marca: str
    modelo: str
    f_garan: str
    f_instal: str
    descripcion: str
    urgente: bool

@router.get("/clientes-maquinas")
def listar_clientes_maquinas():
    try:
        drive = get_drive_service()
        try:
            file_metadata = drive.files().get(fileId=EQUIPOS_FILE_ID, fields="mimeType").execute()
        except Exception as auth_error:
            return {"error": str(auth_error)}
            
        mime_type = file_metadata.get('mimeType')
        if mime_type == 'application/vnd.google-apps.spreadsheet':
            request = drive.files().export_media(fileId=EQUIPOS_FILE_ID, mimeType='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        else:
            request = drive.files().get_media(fileId=EQUIPOS_FILE_ID)
            
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done: 
            status, done = downloader.next_chunk()
        fh.seek(0)

        df = pd.read_excel(fh, header=4)
        clientes_map = {}
        
        for col in ['F.GARAN.', 'F. INSTALACION']:
            if col in df.columns:
                df[col] = df[col].astype(str).replace('nan', '').replace('NaT', '')

        for _, row in df.iterrows():
            c = str(row.get('CLIENTE', '')).strip()
            if not c or c.lower() == 'nan': continue
            
            n = str(row.get('NOMBRE', '')).strip()
            equipo = str(row.get('EQUIPO', '')).strip().replace('nan', '')
            marca = str(row.get('MARCA', '')).strip().replace('nan', '')
            modelo = str(row.get('MODELO', '')).strip().replace('nan', '')
            f_garan = str(row.get('F.GARAN.', '')).strip().replace('nan', '')
            f_instal = str(row.get('F. INSTALACION', '')).strip().replace('nan', '')
            poblacion = str(row.get('POBLACIÓN', '')).strip().replace('nan', '')

            if not n or n.lower() == 'nan':
                n = f"{equipo} {marca} {modelo}".strip()
                
            if c not in clientes_map:
                clientes_map[c] = {
                    "poblacion": poblacion,
                    "maquinas": []
                }
            elif poblacion and not clientes_map[c]["poblacion"]:
                 clientes_map[c]["poblacion"] = poblacion
            
            if n and not any(m['nombre'] == n for m in clientes_map[c]["maquinas"]):
                clientes_map[c]["maquinas"].append({
                    "nombre": n,
                    "equipo": equipo,
                    "marca": marca,
                    "modelo": modelo,
                    "f_garan": f_garan[:10] if f_garan else '',
                    "f_instal": f_instal[:10] if f_instal else ''
                })
                
        return clientes_map
    except Exception as e:
        return {"error": str(e)}

@router.post("/avisos")
def crear_aviso(aviso: NuevoAviso):
    try:
        drive = get_drive_service()
        fila_excel = {
            'F. ENTR.': aviso.fecha_entrada,
            'CLIENTE': aviso.cliente,
            'POBLACIÓN': aviso.poblacion,
            'MÁQUINA': aviso.maquina,
            'EQUIPO': aviso.equipo,
            'MARCA': aviso.marca,
            'MODELO': aviso.modelo,
            'F. GARANTÍA': aviso.f_garan,
            'F. INSTALACIÓN': aviso.f_instal,
            'DESCRIPCIÓN': aviso.descripcion,
            'REALIZADO POR': 'Pendiente',
            'URGENTE': 'SI' if aviso.urgente else 'NO',
            'PIRINEOS': 'NO'
        }
        procesar_excel_avisos_generador(drive, nuevo_aviso=fila_excel)
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
