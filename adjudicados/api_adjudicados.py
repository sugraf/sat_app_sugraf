import io
import json
import os
from datetime import datetime
from fastapi import APIRouter, HTTPException
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
from pydantic import BaseModel
import pandas as pd

router = APIRouter()
FOLDER_ID = "1nK7_foRIcGb9oasij7spOn0kOLQHmVYb"

# Hemos añadido TIPO ASISTENCIA a las columnas oficiales
COLS_TRA = ['F. ENTR.', 'CLIENTE', 'POBLACIÓN', 'MÁQUINA', 'EQUIPO', 'MARCA', 'MODELO', 'F. GARANTÍA', 'F. INSTALACIÓN', 'DESCRIPCIÓN', 'REALIZADO POR', 'URGENTE', 'PIRINEOS', 'GARANTÍA', 'MANTENIMIENTO', 'INSTALACIÓN', 'REVISAR', 'TIPO ASISTENCIA', 'FECHA REALIZACIÓN', 'HORA ENTRADA', 'HORA SALIDA', 'HORAS TOTALES', 'RESUELTO O PENDIENTE', 'PIEZAS NECESARIAS', 'SOLUCIÓN', 'ESTADO']

def get_drive_service():
    creds_dict = json.loads(os.environ.get("GOOGLE_CREDENTIALS_JSON"))
    return build("drive", "v3", credentials=service_account.Credentials.from_service_account_info(
        creds_dict, scopes=["https://www.googleapis.com/auth/drive"]
    ))

def get_excel(drive, name, cols):
    """Lectura robusta idéntica a la del gestor para evitar fallos de conexión"""
    query = f"'{FOLDER_ID}' in parents and name = '{name}' and trashed = false"
    res = drive.files().list(q=query, fields="files(id, mimeType)").execute()
    
    if res.get("files"):
        file_info = res.get("files")[0]
        file_id = file_info['id']
        mime_type = file_info.get('mimeType', '')
        try:
            if mime_type == 'application/vnd.google-apps.spreadsheet':
                req = drive.files().export_media(fileId=file_id, mimeType='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            else:
                req = drive.files().get_media(fileId=file_id)
                
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, req)
            done = False
            while not done: 
                _, done = downloader.next_chunk()
            fh.seek(0)
            df = pd.read_excel(fh)
        except Exception:
            df = pd.DataFrame(columns=cols)
            
        # Nos aseguramos de que existan todas las columnas antes de operar
        for c in cols:
            if c not in df.columns: 
                df[c] = ''
        return file_id, df
    else:
        return None, pd.DataFrame(columns=cols)

def save_excel(drive, file_id, name, df):
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer: 
        df.to_excel(writer, index=False)
    media = MediaIoBaseUpload(io.BytesIO(output.getvalue()), mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    if file_id:
        drive.files().update(fileId=file_id, media_body=media).execute()
    else:
        drive.files().create(body={'name': name, 'parents': [FOLDER_ID]}, media_body=media).execute()

class UpdateParte(BaseModel):
    aviso_index: int
    fecha_realizacion: str
    hora_entrada: str
    hora_salida: str
    resuelto_pendiente: str
    piezas: str
    solucion: str
    cliente: str
    poblacion: str
    maquina: str
    tecnico: str
    tipo_asistencia: str

def calcular_horas(h_in, h_out):
    if not h_in or not h_out or str(h_in).strip() == '' or str(h_out).strip() == '': 
        return ""
    try:
        t1 = datetime.strptime(str(h_in).strip()[:5], "%H:%M")
        t2 = datetime.strptime(str(h_out).strip()[:5], "%H:%M")
        diff = t2 - t1
        return str(diff)[:-3] 
    except:
        return ""

def procesar_actualizacion_tratados(drive, parte: UpdateParte, estado: str):
    file_id, df = get_excel(drive, 'Avisos Tratados.xlsx', COLS_TRA)
    
    idx = int(parte.aviso_index)
    if 0 <= idx < len(df):
        horas_totales = calcular_horas(parte.hora_entrada, parte.hora_salida)
        
        # Usamos .loc para asegurar que la reasignación impacta al Dataframe real (y no a una copia fantasma)
        df.loc[idx, 'TIPO ASISTENCIA'] = parte.tipo_asistencia
        df.loc[idx, 'FECHA REALIZACIÓN'] = parte.fecha_realizacion
        df.loc[idx, 'HORA ENTRADA'] = parte.hora_entrada
        df.loc[idx, 'HORA SALIDA'] = parte.hora_salida
        df.loc[idx, 'HORAS TOTALES'] = horas_totales
        df.loc[idx, 'RESUELTO O PENDIENTE'] = parte.resuelto_pendiente
        df.loc[idx, 'PIEZAS NECESARIAS'] = parte.piezas
        df.loc[idx, 'SOLUCIÓN'] = parte.solucion
        df.loc[idx, 'ESTADO'] = estado
        
        save_excel(drive, file_id, 'Avisos Tratados.xlsx', df)
        return horas_totales
    raise Exception("Index fuera de rango")

@router.get("/mis-avisos")
def get_mis_avisos(tecnico: str):
    try:
        drive = get_drive_service()
        fid_tra, df = get_excel(drive, 'Avisos Tratados.xlsx', COLS_TRA)
        
        # Sanitizamos el dataframe para que no rompa el JSON formating de FastAPI (error 500)
        df = df.fillna('').astype(str).replace('nan', '')
        
        avisos_mios = []
        for index, row in df.iterrows():
            asignado = row.get('REALIZADO POR', '').strip()
            if not asignado: asignado = 'Pendiente'
            
            estado = row.get('ESTADO', '').strip()
            if not estado: estado = 'Vacío'
            
            tipo_asis = row.get('TIPO ASISTENCIA', '').strip()
            if not tipo_asis: tipo_asis = 'Presencial'
            
            row_dict = row.to_dict()
            row_dict['REALIZADO POR'] = asignado
            row_dict['ESTADO'] = estado
            row_dict['TIPO ASISTENCIA'] = tipo_asis
            row_dict['aviso_index'] = index

            if tecnico == 'master':
                if asignado != 'Pendiente':
                    avisos_mios.append(row_dict)
            else:
                if asignado == tecnico:
                    avisos_mios.append(row_dict)
                
        return {"avisos": avisos_mios}
    except Exception as e: 
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/guardar-progreso")
def guardar_progreso(parte: UpdateParte):
    try:
        drive = get_drive_service()
        # Se envía estado "Abierto" a la hora de procesarlo
        procesar_actualizacion_tratados(drive, parte, "Abierto")
        return {"status": "ok"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/resolver")
def resolver_aviso(parte: UpdateParte):
    try:
        drive = get_drive_service()
        horas = procesar_actualizacion_tratados(drive, parte, "Cerrado")
        
        contenido = (
            f"=== PARTE DE TRABAJO FINALIZADO ===\n"
            f"TÉCNICO:           {parte.tecnico}\n"
            f"MODALIDAD:         {parte.tipo_asistencia.upper()}\n"
            f"FECHA REALIZACIÓN: {parte.fecha_realizacion}\n"
            f"HORARIO:           {parte.hora_entrada} - {parte.hora_salida} ({horas}h)\n"
            f"ESTADO RESOLUCIÓN: {parte.resuelto_pendiente.upper()}\n"
            f"-----------------------------------\n"
            f"CLIENTE:   {parte.cliente}\n"
            f"DIRECCIÓN: {parte.poblacion}\n"
            f"MÁQUINA:   {parte.maquina}\n"
            f"-----------------------------------\n"
            f"PIEZAS NECESARIAS:\n{parte.piezas}\n"
            f"-----------------------------------\n"
            f"TRABAJOS REALIZADOS / SOLUCIÓN:\n{parte.solucion}\n"
        )
        nombre_txt = f"ParteResuelto_{parte.cliente.replace(' ', '_')}_{parte.fecha_realizacion}.txt"
        media_txt = MediaIoBaseUpload(io.BytesIO(contenido.encode("utf-8")), mimetype="text/plain")
        drive.files().create(body={"name": nombre_txt, "parents": [FOLDER_ID]}, media_body=media_txt).execute()
        
        return {"status": "ok"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
