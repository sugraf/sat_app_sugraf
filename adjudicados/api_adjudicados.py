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

# Se ha añadido 'TIPO ASISTENCIA' a las columnas obligatorias
COLS_TRA = ['F. ENTR.', 'CLIENTE', 'POBLACIÓN', 'MÁQUINA', 'EQUIPO', 'MARCA', 'MODELO', 'F. GARANTÍA', 'F. INSTALACIÓN', 'DESCRIPCIÓN', 'REALIZADO POR', 'URGENTE', 'PIRINEOS', 'GARANTÍA', 'MANTENIMIENTO', 'INSTALACIÓN', 'REVISAR', 'TIPO ASISTENCIA', 'FECHA REALIZACIÓN', 'HORA ENTRADA', 'HORA SALIDA', 'HORAS TOTALES', 'RESUELTO O PENDIENTE', 'PIEZAS NECESARIAS', 'SOLUCIÓN', 'ESTADO']

def get_drive_service():
    creds_dict = json.loads(os.environ.get("GOOGLE_CREDENTIALS_JSON"))
    return build("drive", "v3", credentials=service_account.Credentials.from_service_account_info(
        creds_dict, scopes=["https://www.googleapis.com/auth/drive"]
    ))

def get_excel(drive, name, cols):
    query = f"'{FOLDER_ID}' in parents and name = '{name}' and trashed = false"
    res = drive.files().list(q=query, fields="files(id)").execute()
    archivos = res.get("files", [])
    
    if archivos:
        file_id = archivos[0]['id']
        try:
            request = drive.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done: 
                _, done = downloader.next_chunk()
            fh.seek(0)
            df = pd.read_excel(fh)
        except Exception:
            df = pd.DataFrame(columns=cols)
            
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
    tipo_asistencia: str  # Nuevo campo para Presencial/Telemático

def calcular_horas(h_in, h_out):
    if not h_in or not h_out or str(h_in) == 'nan' or str(h_out) == 'nan': return ""
    try:
        t1 = datetime.strptime(str(h_in)[:5], "%H:%M")
        t2 = datetime.strptime(str(h_out)[:5], "%H:%M")
        diff = t2 - t1
        return str(diff)[:-3] 
    except:
        return ""

def procesar_actualizacion_tratados(drive, parte: UpdateParte, estado: str):
    file_id, df = get_excel(drive, 'Avisos Tratados.xlsx', COLS_TRA)
    
    if 0 <= parte.aviso_index < len(df):
        horas_totales = calcular_horas(parte.hora_entrada, parte.hora_salida)
        
        df.at[parte.aviso_index, 'TIPO ASISTENCIA'] = parte.tipo_asistencia
        df.at[parte.aviso_index, 'FECHA REALIZACIÓN'] = parte.fecha_realizacion
        df.at[parte.aviso_index, 'HORA ENTRADA'] = parte.hora_entrada
        df.at[parte.aviso_index, 'HORA SALIDA'] = parte.hora_salida
        df.at[parte.aviso_index, 'HORAS TOTALES'] = horas_totales
        df.at[parte.aviso_index, 'RESUELTO O PENDIENTE'] = parte.resuelto_pendiente
        df.at[parte.aviso_index, 'PIEZAS NECESARIAS'] = parte.piezas
        df.at[parte.aviso_index, 'SOLUCIÓN'] = parte.solucion
        df.at[parte.aviso_index, 'ESTADO'] = estado
        
        save_excel(drive, file_id, 'Avisos Tratados.xlsx', df)
        return horas_totales
    raise Exception("Index fuera de rango")

@router.get("/mis-avisos")
def get_mis_avisos(tecnico: str):
    try:
        drive = get_drive_service()
        fid_tra, df = get_excel(drive, 'Avisos Tratados.xlsx', COLS_TRA)
        
        if 'ESTADO' not in df.columns: df['ESTADO'] = 'Vacío'
        if 'TIPO ASISTENCIA' not in df.columns: df['TIPO ASISTENCIA'] = 'Presencial'
        
        df['REALIZADO POR'] = df['REALIZADO POR'].fillna('Pendiente')
        df['ESTADO'] = df['ESTADO'].replace('', 'Vacío').fillna('Vacío')
        
        # Conversión estricta a string para evitar que JSON formatee NaNs o Fechas mal y rompa el HTML
        df = df.astype(str).replace({'nan': '', 'NaT': '', 'None': '', '<NA>': ''})
        
        # Limpieza de formatos de hora (en caso de que Pandas haya leído HH:MM:SS)
        if 'HORA ENTRADA' in df.columns:
            df['HORA ENTRADA'] = df['HORA ENTRADA'].apply(lambda x: x[:5] if len(str(x)) >= 5 and ":" in str(x) else x)
        if 'HORA SALIDA' in df.columns:
            df['HORA SALIDA'] = df['HORA SALIDA'].apply(lambda x: x[:5] if len(str(x)) >= 5 and ":" in str(x) else x)
        
        avisos_mios = []
        for index, row in df.iterrows():
            asignado = str(row['REALIZADO POR'])
            if tecnico == 'master':
                if asignado not in ['Pendiente', 'nan', '']:
                    dic = row.to_dict()
                    dic['aviso_index'] = index
                    avisos_mios.append(dic)
            else:
                if asignado == tecnico:
                    dic = row.to_dict()
                    dic['aviso_index'] = index
                    avisos_mios.append(dic)
                
        return {"avisos": avisos_mios}
    except Exception as e: 
        raise HTTPException(status_code=500, detail=str(e))

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
