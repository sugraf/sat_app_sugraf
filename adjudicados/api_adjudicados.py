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

COLS_TRA = ['F. ENTR.', 'CLIENTE', 'POBLACIÓN', 'MÁQUINA', 'EQUIPO', 'MARCA', 'MODELO', 'F. GARANTÍA', 'F. INSTALACIÓN', 'DESCRIPCIÓN', 'REALIZADO POR', 'URGENTE', 'PIRINEOS', 'GARANTÍA', 'MANTENIMIENTO', 'INSTALACIÓN', 'REVISAR', 'FECHA REALIZACIÓN', 'HORA ENTRADA', 'HORA SALIDA', 'HORAS TOTALES', 'RESUELTO O PENDIENTE', 'PIEZAS NECESARIAS', 'SOLUCIÓN', 'ESTADO']

def get_drive_service():
    creds_dict = json.loads(os.environ.get("GOOGLE_CREDENTIALS_JSON"))
    return build("drive", "v3", credentials=service_account.Credentials.from_service_account_info(
        creds_dict, scopes=["https://www.googleapis.com/auth/drive"]
    ))

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

def calcular_horas(h_in, h_out):
    if not h_in or not h_out: return ""
    try:
        t1 = datetime.strptime(h_in, "%H:%M")
        t2 = datetime.strptime(h_out, "%H:%M")
        diff = t2 - t1
        return str(diff)[:-3] # Devuelve "H:MM"
    except:
        return ""

def procesar_actualizacion_tratados(drive, parte: UpdateParte, estado: str):
    query = f"'{FOLDER_ID}' in parents and name = 'Avisos Tratados.xlsx' and trashed = false"
    res = drive.files().list(q=query, fields="files(id)").execute()
    if not res.get("files"): raise Exception("No se encuentra Avisos Tratados.xlsx")
    
    file_id = res.get("files")[0]['id']
    req = drive.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, req)
    done = False
    while not done: _, done = downloader.next_chunk()
    fh.seek(0)
    df = pd.read_excel(fh)
    
    # Aseguramos columnas
    for c in COLS_TRA:
        if c not in df.columns: df[c] = ''

    if 0 <= parte.aviso_index < len(df):
        horas_totales = calcular_horas(parte.hora_entrada, parte.hora_salida)
        
        df.at[parte.aviso_index, 'FECHA REALIZACIÓN'] = parte.fecha_realizacion
        df.at[parte.aviso_index, 'HORA ENTRADA'] = parte.hora_entrada
        df.at[parte.aviso_index, 'HORA SALIDA'] = parte.hora_salida
        df.at[parte.aviso_index, 'HORAS TOTALES'] = horas_totales
        df.at[parte.aviso_index, 'RESUELTO O PENDIENTE'] = parte.resuelto_pendiente
        df.at[parte.aviso_index, 'PIEZAS NECESARIAS'] = parte.piezas
        df.at[parte.aviso_index, 'SOLUCIÓN'] = parte.solucion
        df.at[parte.aviso_index, 'ESTADO'] = estado
        
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer: df.to_excel(writer, index=False)
        media_xls = MediaIoBaseUpload(io.BytesIO(output.getvalue()), mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        drive.files().update(fileId=file_id, media_body=media_xls).execute()
        
        return horas_totales
    raise Exception("Index fuera de rango")

@router.get("/mis-avisos")
def get_mis_avisos(tecnico: str):
    try:
        drive = get_drive_service()
        query = f"'{FOLDER_ID}' in parents and name = 'Avisos Tratados.xlsx' and trashed = false"
        res = drive.files().list(q=query, fields="files(id)").execute()
        if not res.get("files"): return {"avisos": []}
        
        req = drive.files().get_media(fileId=res.get("files")[0]['id'])
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, req)
        done = False
        while not done: _, done = downloader.next_chunk()
        fh.seek(0)
        df = pd.read_excel(fh)
        
        if 'REALIZADO POR' not in df.columns: return {"avisos": []}
        if 'ESTADO' not in df.columns: df['ESTADO'] = 'Vacío'
        df['REALIZADO POR'] = df['REALIZADO POR'].fillna('Pendiente')
        df['ESTADO'] = df['ESTADO'].replace('', 'Vacío').fillna('Vacío')
        
        avisos_mios = []
        for index, row in df.iterrows():
            asignado = str(row['REALIZADO POR'])
            if tecnico == 'master':
                if asignado != 'Pendiente' and asignado != 'nan' and asignado != '':
                    dic = row.fillna("").to_dict()
                    dic['aviso_index'] = index
                    avisos_mios.append(dic)
            else:
                if asignado == tecnico:
                    dic = row.fillna("").to_dict()
                    dic['aviso_index'] = index
                    avisos_mios.append(dic)
                
        return {"avisos": avisos_mios}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/guardar-progreso")
def guardar_progreso(parte: UpdateParte):
    try:
        drive = get_drive_service()
        procesar_actualizacion_tratados(drive, parte, "Abierto")
        return {"status": "ok"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.post("/resolver")
def resolver_aviso(parte: UpdateParte):
    try:
        drive = get_drive_service()
        horas = procesar_actualizacion_tratados(drive, parte, "Cerrado")
        
        # Generar TXT
        contenido = (
            f"=== PARTE DE TRABAJO FINALIZADO ===\n"
            f"TÉCNICO:           {parte.tecnico}\n"
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
