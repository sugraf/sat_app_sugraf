# api_adjudicados.py
import io
import json
import os
import smtplib
from email.message import EmailMessage
from datetime import datetime
from fastapi import APIRouter, HTTPException
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
from pydantic import BaseModel
import pandas as pd

router = APIRouter()
FOLDER_ID = "1nK7_foRIcGb9oasij7spOn0kOLQHmVYb"

COLS_TRA = ['F. ENTR.', 'CLIENTE', 'POBLACIÓN', 'MÁQUINA', 'EQUIPO', 'MARCA', 'MODELO', 'F. GARANTÍA', 'F. INSTALACIÓN', 'DESCRIPCIÓN', 'ASIGNADO A', 'URGENTE', 'PIRINEOS', 'GARANTÍA', 'MANTENIMIENTO', 'INSTALACIÓN', 'REVISAR', 'TIPO ASISTENCIA', 'FECHA REALIZACIÓN', 'HORA ENTRADA', 'HORA SALIDA', 'HORAS TOTALES', 'RESUELTO O PENDIENTE', 'PIEZAS NECESARIAS', 'SOLUCIÓN', 'ESTADO', 'OPCIÓN A VENTA', 'DETALLE VENTA', 'ESTADO PIEZAS', 'OBSERVACIONES']

def get_drive_service():
    creds_dict = json.loads(os.environ.get("GOOGLE_CREDENTIALS_JSON"))
    return build("drive", "v3", credentials=service_account.Credentials.from_service_account_info(
        creds_dict, scopes=["https://www.googleapis.com/auth/drive"]
    ))

def get_excel(drive, name, cols):
    query = f"'{FOLDER_ID}' in parents and name = '{name}' and trashed = false"
    res = drive.files().list(q=query, fields="files(id, mimeType)").execute()
    archivos = res.get("files", [])
    
    if archivos:
        file_info = archivos[0]
        file_id = file_info['id']
        mime_type = file_info.get('mimeType', '')
        
        if mime_type == 'application/vnd.google-apps.spreadsheet':
            request = drive.files().export_media(fileId=file_id, mimeType='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        else:
            request = drive.files().get_media(fileId=file_id)
            
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done: 
            _, done = downloader.next_chunk()
        fh.seek(0)
        
        df = pd.read_excel(fh, engine='openpyxl', dtype=object)
        
        for c in cols:
            if c not in df.columns: 
                df[c] = ''
                
        df = df.astype(object)
        return file_id, df
    else:
        return None, pd.DataFrame(columns=cols, dtype=object)

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
    pirineos: str
    opcion_venta: str
    detalle_venta: str
    estado_piezas: str

class ArchivarParte(BaseModel):
    aviso_index: int

class MarcarPiezas(BaseModel):
    aviso_index: int

def calcular_horas(h_in, h_out):
    if not h_in or not h_out or str(h_in).strip() == '' or str(h_out).strip() == '': 
        return ""
    try:
        ins = str(h_in).strip().split('\n')
        outs = str(h_out).strip().split('\n')
        total_seconds = 0
        for i in range(min(len(ins), len(outs))):
            i_str = ins[i].strip()
            o_str = outs[i].strip()
            if not i_str or not o_str:
                continue
            t1 = datetime.strptime(i_str[:5], "%H:%M")
            t2 = datetime.strptime(o_str[:5], "%H:%M")
            if t2 >= t1:
                total_seconds += (t2 - t1).total_seconds()
            else:
                total_seconds += (t2 - t1).total_seconds() + 86400
        
        hours = int(total_seconds // 3600)
        minutes = int((total_seconds % 3600) // 60)
        
        if hours == 0 and minutes == 0: 
            return ""
        return f"{hours:02d}:{minutes:02d}"
    except:
        return ""

def enviar_email_venta(row_data):
    opcion_venta = str(row_data.get('OPCIÓN A VENTA', '')).strip().upper()
    if opcion_venta != 'SI':
        return False, False, None

    user = "sugraf.digitalhub@gmail.com"
    pwd = "sugrafmarketing_digitalhub"

    cliente = str(row_data.get('CLIENTE', '')).strip()
    poblacion = str(row_data.get('POBLACIÓN', '')).strip()
    maquina = str(row_data.get('MÁQUINA', '')).strip()
    tecnico = str(row_data.get('ASIGNADO A', '')).strip()
    detalle_venta = str(row_data.get('DETALLE VENTA', '')).strip()

    msg = EmailMessage()
    msg['Subject'] = f"Posible oportunidad de venta - {cliente}"
    msg['From'] = user
    msg['To'] = "lucia@sugraf.es, info@sugraf.es"

    cuerpo = (
        f"Buenos días,\n\n"
        f"Se ha detectado una posible oportunidad de venta en un parte de trabajo.\n\n"
        f"Cliente: {cliente}\n"
        f"Población: {poblacion}\n"
        f"Máquina: {maquina}\n"
        f"Técnico: {tecnico}\n\n"
        f"Detalle de la posible venta:\n"
        f"{detalle_venta}\n\n"
        f"Un saludo."
    )

    msg.set_content(cuerpo)

    try:
        print(f"Intentando enviar email de oportunidad de venta para {cliente}...")
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(user, pwd)
            print("Autenticación SMTP correcta.")
            smtp.send_message(msg)
            print("Email enviado correctamente.")
        return True, True, None
    except Exception as e:
        error_str = str(e)
        print(f"Error al enviar email: {error_str}")
        return True, False, error_str

def procesar_actualizacion_tratados(drive, parte: UpdateParte, estado: str):
    file_id, df = get_excel(drive, 'Avisos Tratados.xlsx', COLS_TRA)
    
    idx = int(parte.aviso_index)
    horas_totales = calcular_horas(parte.hora_entrada, parte.hora_salida)
    
    df.loc[idx, 'TIPO ASISTENCIA'] = parte.tipo_asistencia
    df.loc[idx, 'PIRINEOS'] = parte.pirineos
    df.loc[idx, 'FECHA REALIZACIÓN'] = parte.fecha_realizacion
    df.loc[idx, 'HORA ENTRADA'] = parte.hora_entrada
    df.loc[idx, 'HORA SALIDA'] = parte.hora_salida
    df.loc[idx, 'HORAS TOTALES'] = horas_totales
    df.loc[idx, 'RESUELTO O PENDIENTE'] = parte.resuelto_pendiente
    df.loc[idx, 'PIEZAS NECESARIAS'] = parte.piezas
    df.loc[idx, 'SOLUCIÓN'] = parte.solucion
    df.loc[idx, 'ESTADO'] = estado
    df.loc[idx, 'OPCIÓN A VENTA'] = parte.opcion_venta
    df.loc[idx, 'DETALLE VENTA'] = parte.detalle_venta
    df.loc[idx, 'ESTADO PIEZAS'] = parte.estado_piezas
    
    save_excel(drive, file_id, 'Avisos Tratados.xlsx', df)
    return horas_totales

@router.get("/mis-avisos")
def get_mis_avisos(tecnico: str):
    try:
        drive = get_drive_service()
        fid_tra, df = get_excel(drive, 'Avisos Tratados.xlsx', COLS_TRA)
        
        df = df.fillna('').astype(str).replace('nan', '')
        
        avisos_mios = []
        for index, row in df.iterrows():
            asignado = row.get('ASIGNADO A', '').strip()
            if not asignado: asignado = 'Pendiente'
            
            estado = row.get('ESTADO', '').strip()
            if not estado: estado = 'Vacío'
            
            tipo_asis = row.get('TIPO ASISTENCIA', '').strip()
            if not tipo_asis: tipo_asis = 'Presencial'

            pirineos_val = row.get('PIRINEOS', '').strip()
            if not pirineos_val: pirineos_val = 'NO'
            
            row_dict = row.to_dict()
            row_dict['ASIGNADO A'] = asignado
            row_dict['ESTADO'] = estado
            row_dict['TIPO ASISTENCIA'] = tipo_asis
            row_dict['PIRINEOS'] = pirineos_val
            row_dict['OPCIÓN A VENTA'] = row.get('OPCIÓN A VENTA', '').strip() or 'NO'
            row_dict['DETALLE VENTA'] = row.get('DETALLE VENTA', '').strip()
            row_dict['ESTADO PIEZAS'] = row.get('ESTADO PIEZAS', '').strip()
            row_dict['HORAS TOTALES'] = row.get('HORAS TOTALES', '').strip()
            row_dict['aviso_index'] = index

            if tecnico == 'Master':
                if asignado != 'Pendiente':
                    avisos_mios.append(row_dict)
            else:
                if asignado == tecnico:
                    avisos_mios.append(row_dict)
                
        return {"avisos": avisos_mios}
    except Exception as e: 
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/piezas-recibidas")
def marcar_piezas(data: MarcarPiezas):
    try:
        drive = get_drive_service()
        fid_tra, df = get_excel(drive, 'Avisos Tratados.xlsx', COLS_TRA)
        idx = int(data.aviso_index)
        
        if 0 <= idx < len(df):
            df.loc[idx, 'ESTADO PIEZAS'] = 'Piezas Recibidas'
            save_excel(drive, fid_tra, 'Avisos Tratados.xlsx', df)
            return {"status": "ok"}
        else:
            raise Exception("Index out of bounds")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/guardar-progreso")
def guardar_progreso(parte: UpdateParte):
    try:
        drive = get_drive_service()
        procesar_actualizacion_tratados(drive, parte, "Abierto")
        return {"status": "ok"}
    except Exception as e: 
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/editar-cerrado")
def editar_cerrado(parte: UpdateParte):
    try:
        drive = get_drive_service()
        procesar_actualizacion_tratados(drive, parte, "Cerrado")
        return {"status": "ok"}
    except Exception as e: 
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/resolver")
def resolver_aviso(parte: UpdateParte):
    try:
        drive = get_drive_service()
        procesar_actualizacion_tratados(drive, parte, "Cerrado")
        return {"status": "ok"}
    except Exception as e: 
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/reabrir")
def reabrir_aviso(parte: UpdateParte):
    try:
        drive = get_drive_service()
        procesar_actualizacion_tratados(drive, parte, "Abierto")
        return {"status": "ok"}
    except Exception as e: 
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/archivar")
def archivar_aviso(parte: ArchivarParte):
    try:
        drive = get_drive_service()
        fid_tra, df_tra = get_excel(drive, 'Avisos Tratados.xlsx', COLS_TRA)
        idx = int(parte.aviso_index)
        
        if 0 <= idx < len(df_tra):
            row_to_archive = df_tra.iloc[idx].copy()
            
            # Enviar el email SOLO cuando se procesa a Pirineos (archivo final)
            attempted, sent, err = enviar_email_venta(row_to_archive)
            
            fid_fin, df_fin = get_excel(drive, 'Partes Finalizados.xlsx', COLS_TRA)
            df_fin = pd.concat([df_fin, pd.DataFrame([row_to_archive])], ignore_index=True)
            
            df_tra = df_tra.drop(index=idx).reset_index(drop=True)
            
            save_excel(drive, fid_fin, 'Partes Finalizados.xlsx', df_fin)
            save_excel(drive, fid_tra, 'Avisos Tratados.xlsx', df_tra)
            
            return {
                "status": "ok",
                "email_attempted": attempted,
                "email_sent": sent,
                "email_error": err
            }
        else:
            raise Exception("Index out of bounds")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
