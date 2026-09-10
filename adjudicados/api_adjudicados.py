# api_adjudicados.py
import io
import json
import os
import smtplib
import base64
from email.message import EmailMessage
from datetime import datetime
from fastapi import APIRouter, HTTPException
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
from pydantic import BaseModel
import pandas as pd
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.lib.colors import HexColor

router = APIRouter()
FOLDER_ID = "1nK7_foRIcGb9oasij7spOn0kOLQHmVYb"

COLS_SIN = ['F. ENTR.', 'CLIENTE', 'POBLACIÓN', 'MÁQUINA', 'EQUIPO', 'MARCA', 'MODELO', 'Nº SERIE', 'F. GARANTÍA', 'F. INSTALACIÓN', 'DESCRIPCIÓN', 'ASIGNADO A', 'URGENTE', 'PIRINEOS', 'GARANTÍA', 'MANTENIMIENTO', 'INSTALACIÓN', 'REVISAR', 'NUEVO', 'PARADO', 'RECLAMA', 'PRESUPUESTO', 'PIEZAS', 'ESTADO PIEZAS', 'OBSERVACIONES']
COLS_TRA = ['F. ENTR.', 'CLIENTE', 'POBLACIÓN', 'MÁQUINA', 'EQUIPO', 'MARCA', 'MODELO', 'Nº SERIE', 'F. GARANTÍA', 'F. INSTALACIÓN', 'DESCRIPCIÓN', 'ASIGNADO A', 'URGENTE', 'PIRINEOS', 'GARANTÍA', 'MANTENIMIENTO', 'INSTALACIÓN', 'REVISAR', 'NUEVO', 'PARADO', 'RECLAMA', 'PRESUPUESTO', 'PIEZAS', 'TIPO ASISTENCIA', 'FECHA REALIZACIÓN', 'HORA ENTRADA', 'HORA SALIDA', 'HORAS TOTALES', 'RESUELTO O PENDIENTE', 'PIEZAS NECESARIAS', 'SOLUCIÓN', 'ESTADO', 'OPCIÓN A VENTA', 'DETALLE VENTA', 'ESTADO PIEZAS', 'OBSERVACIONES']

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

def get_and_increment_part_number(drive):
    query = f"'{FOLDER_ID}' in parents and name = 'numeracion_partes.txt' and trashed = false"
    res = drive.files().list(q=query, fields="files(id)").execute()
    archivos = res.get("files", [])

    if archivos:
        file_id = archivos[0]['id']
        request = drive.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        current_num = fh.getvalue().decode('utf-8').strip()
        try:
            num = int(current_num)
        except:
            num = 10000
    else:
        file_id = None
        num = 10000

    next_num = num + 1

    media = MediaIoBaseUpload(io.BytesIO(str(next_num).encode('utf-8')), mimetype='text/plain')
    if file_id:
        drive.files().update(fileId=file_id, media_body=media).execute()
    else:
        drive.files().create(body={'name': 'numeracion_partes.txt', 'parents': [FOLDER_ID]}, media_body=media).execute()

    return num

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

class CerrarYEnviarParte(BaseModel):
    base_parte: UpdateParte
    pdf_fechas: str
    pdf_horas_in: str
    pdf_horas_out: str
    pdf_tecnico: str
    pdf_cliente: str
    pdf_poblacion: str
    pdf_maquina: str
    pdf_marca: str
    pdf_modelo: str
    pdf_n_serie: str
    pdf_motivo: str
    pdf_piezas: str
    pdf_trabajo: str
    pdf_observaciones: str
    pdf_estado: str
    email_tecnico: str
    email_laura: str
    email_cliente: str
    firma_tecnico_b64: str
    firma_cliente_b64: str
    pdf_nombre_cliente: str

class ArchivarParte(BaseModel):
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
    if estado != "Cerrado":  # If it is saving progress we keep the original state of the pieces unless updated
        df.loc[idx, 'ESTADO PIEZAS'] = parte.estado_piezas

    save_excel(drive, file_id, 'Avisos Tratados.xlsx', df)
    return horas_totales

def generar_pdf_parte(datos: CerrarYEnviarParte, num_parte: int):
    buffer = io.BytesIO()
    c = pdf_canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    color_sugraf = HexColor("#006858")
    c.setStrokeColor(color_sugraf)

    logo_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logo_grande.png")
    if os.path.exists(logo_path):
        c.drawImage(logo_path, 40, height - 70, width=140, height=45, preserveAspectRatio=True, mask='auto')
    else:
        c.setFont("Helvetica-Bold", 24)
        c.setFillColor(color_sugraf)
        c.drawString(40, height - 60, "SUGRAF")

    c.setFont("Helvetica-Bold", 11)
    c.setFillColorRGB(0,0,0)
    c.drawString(width - 250, height - 60, f"ORDEN DE TRABAJO Nº: {num_parte}")

    y_cursor = height - 100

    fechas = [f.strip() for f in datos.pdf_fechas.split('\n')]
    horas_in = [h.strip() for h in datos.pdf_horas_in.split('\n')]
    horas_out = [h.strip() for h in datos.pdf_horas_out.split('\n')]

    num_lineas = max(len(fechas), len(horas_in), len(horas_out))
    if num_lineas == 0:
        num_lineas = 1

    extra_height = max(0, (num_lineas - 1) * 15)
    box1_h = 35 + extra_height

    c.rect(40, y_cursor - box1_h, width - 80, box1_h)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(45, y_cursor - 15, "Fechas y Horas:")
    c.setFont("Helvetica", 9)

    y_text = y_cursor - 28
    for i in range(num_lineas):
        f = fechas[i] if i < len(fechas) else ""
        hin = horas_in[i] if i < len(horas_in) else ""
        hout = horas_out[i] if i < len(horas_out) else ""

        line_str = f"Fecha: {f}    |    Hora inicio: {hin}    |    Hora fin: {hout}"
        c.drawString(45, y_text, line_str)
        y_text -= 15

    y_cursor -= (box1_h + 10)

    # Calculamos lineas del Motivo para expandir el bloque si es necesario
    lines_motivo = simpleSplit(datos.pdf_motivo, "Helvetica", 9, width - 150)
    extra_mot = max(0, (len(lines_motivo) - 1) * 12)
    box2_h = 95 + extra_mot

    c.rect(40, y_cursor - box2_h, width - 80, box2_h)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(45, y_cursor - 15, "Técnico:")
    c.setFont("Helvetica", 9)
    c.drawString(95, y_cursor - 15, datos.pdf_tecnico)

    c.setFont("Helvetica-Bold", 9)
    c.drawString(width/2, y_cursor - 15, "Cliente:")
    c.setFont("Helvetica", 9)
    c.drawString(width/2 + 45, y_cursor - 15, datos.pdf_cliente)

    c.setFont("Helvetica-Bold", 9)
    c.drawString(45, y_cursor - 35, "Localidad:")
    c.setFont("Helvetica", 9)
    c.drawString(100, y_cursor - 35, datos.pdf_poblacion)

    c.setFont("Helvetica-Bold", 9)
    c.drawString(45, y_cursor - 55, "Motivo aviso:")
    c.setFont("Helvetica", 9)
    y_m = y_cursor - 55
    for l in lines_motivo:
        c.drawString(115, y_m, l)
        y_m -= 12

    c.setFont("Helvetica-Bold", 9)
    c.drawString(45, y_cursor - 75 - extra_mot, "Máquina / Equipo:")
    c.setFont("Helvetica", 9)
    c.drawString(135, y_cursor - 75 - extra_mot, datos.pdf_maquina[:45])

    c.setFont("Helvetica-Bold", 9)
    c.drawString(45, y_cursor - 90 - extra_mot, f"Marca: {datos.pdf_marca}   |   Modelo: {datos.pdf_modelo}   |   Nº Serie: {datos.pdf_n_serie}")
    y_cursor -= (box2_h + 10)

    # Reducimos el espacio de trabajo realizado proporcionalmente al motivo
    box3_h = max(40, 130 - extra_height - extra_mot)
    c.rect(40, y_cursor - box3_h, width - 80, box3_h)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(45, y_cursor - 15, "Trabajo realizado:")
    c.setFont("Helvetica", 9)
    lines = simpleSplit(datos.pdf_trabajo, "Helvetica", 9, width - 90)
    y_text = y_cursor - 30

    max_lines_trabajo = int((box3_h - 20) / 15)
    for l in lines[:max_lines_trabajo]:
        c.drawString(45, y_text, l)
        y_text -= 15
    y_cursor -= (box3_h + 10)

    box4_h = 80
    c.rect(40, y_cursor - box4_h, width - 80, box4_h)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(45, y_cursor - 15, "Piezas / Repuestos:")
    c.setFont("Helvetica", 9)
    lines = simpleSplit(datos.pdf_piezas, "Helvetica", 9, width - 90)
    y_text = y_cursor - 30
    for l in lines[:3]:
        c.drawString(45, y_text, l)
        y_text -= 15
    y_cursor -= (box4_h + 10)

    box5_h = 70
    c.rect(40, y_cursor - box5_h, width - 80, box5_h)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(45, y_cursor - 15, "Observaciones:")
    c.setFont("Helvetica", 9)
    lines = simpleSplit(datos.pdf_observaciones, "Helvetica", 9, width - 90)
    y_text = y_cursor - 30
    for l in lines[:2]:
        c.drawString(45, y_text, l)
        y_text -= 15
    y_cursor -= (box5_h + 10)

    c.setFont("Helvetica-Bold", 11)
    c.drawString(45, y_cursor - 15, f"Estado del Aviso: {datos.pdf_estado.upper()}")
    y_cursor -= 30

    # Firmas Centradas
    c.setFont("Helvetica-Bold", 10)
    c.drawCentredString(140, y_cursor - 10, f"Fdo: {datos.pdf_tecnico} (Técnico)")
    c.drawCentredString(width - 140, y_cursor - 10, f"Fdo: {datos.pdf_nombre_cliente} (Cliente)")

    y_firmas = y_cursor - 90

    c.setStrokeColorRGB(0.8, 0.8, 0.8)
    c.rect(50, y_firmas, 180, 70)
    c.rect(width - 230, y_firmas, 180, 70)

    if datos.firma_tecnico_b64 and "," in datos.firma_tecnico_b64:
        b64_data = datos.firma_tecnico_b64.split(",")[1]
        try:
            img_bytes = base64.b64decode(b64_data)
            img = ImageReader(io.BytesIO(img_bytes))
            c.drawImage(img, 50, y_firmas, width=180, height=70, mask='auto')
        except Exception as e:
            pass

    if datos.firma_cliente_b64 and "," in datos.firma_cliente_b64:
        b64_data = datos.firma_cliente_b64.split(",")[1]
        try:
            img_bytes = base64.b64decode(b64_data)
            img = ImageReader(io.BytesIO(img_bytes))
            c.drawImage(img, width - 230, y_firmas, width=180, height=70, mask='auto')
        except Exception as e:
            pass

    c.setFont("Helvetica", 8)
    c.setFillColorRGB(0.4, 0.4, 0.4)
    c.drawCentredString(width/2, 30, "TECNOLOGIA Y PRODUCTOS GRAFICOS, S.A. Pol. Alcalde Caballero")
    c.drawCentredString(width/2, 20, "C/Monasterio de las Descalzas Reales, nave 6-7 50014 Zaragoza Telf. 976 304 220 www.sugraf.es info@sugraf.es")

    c.showPage()
    c.save()
    return buffer.getvalue()

def enviar_email_cierre(datos: CerrarYEnviarParte, pdf_bytes, num_parte):
    user = "sugraf.digitalhub@gmail.com"
    pwd = "dbsn dinz jakv vkay"

    msg = EmailMessage()
    msg['Subject'] = f"Parte de Trabajo Sugraf Nº {num_parte} - {datos.pdf_cliente} - {datos.pdf_maquina}"
    msg['From'] = user

    destinatarios = []
    if datos.email_laura.strip():
        destinatarios.append(datos.email_laura.strip())
    if datos.email_tecnico.strip():
        destinatarios.append(datos.email_tecnico.strip())
    if datos.email_cliente.strip():
        destinatarios.append(datos.email_cliente.strip())

    if not destinatarios:
        return True, True, None

    msg['To'] = ", ".join(destinatarios)

    cuerpo = (
        f"Buenos días,\n\n"
        f"Desde Sugraf escribimos para confirmar que se ha cerrado el parte del aviso:\n"
        f"{datos.pdf_cliente} - {datos.pdf_maquina}\n\n"
        f"Se adjunta en este mensaje el parte firmado por el técnico y el cliente.\n\n"
        f"Un saludo."
    )

    msg.set_content(cuerpo)
    
    safe_cliente = str(datos.pdf_cliente).replace('/', '-').replace('\\', '-')
    safe_maquina = str(datos.pdf_maquina).replace('/', '-').replace('\\', '-')
    nombre_archivo = f'Parte Nº {num_parte} - {safe_cliente} - {safe_maquina}.pdf'
    
    msg.add_attachment(pdf_bytes, maintype='application', subtype='pdf', filename=nombre_archivo)

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(user, pwd)
            smtp.send_message(msg)
        return True, True, None
    except Exception as e:
        return True, False, str(e)

def enviar_email_venta(row_data):
    opcion_venta = str(row_data.get('OPCIÓN A VENTA', '')).strip().upper()
    if opcion_venta != 'SI':
        return False, False, None

    user = "sugraf.digitalhub@gmail.com"
    pwd = "dbsn dinz jakv vkay"

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
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(user, pwd)
            smtp.send_message(msg)
        return True, True, None
    except Exception as e:
        return True, False, str(e)

@router.get("/mis-avisos")
def get_mis_avisos(tecnico: str):
    try:
        drive = get_drive_service()
        fid_tra, df = get_excel(drive, 'Avisos Tratados.xlsx', COLS_TRA)

        df = df.fillna('').astype(str).replace('nan', '')

        avisos_mios = []
        for index, row in df.iterrows():
            asignado = row.get('ASIGNADO A', '').strip()
            if not asignado:
                asignado = 'Pendiente'

            estado = row.get('ESTADO', '').strip()
            if not estado:
                estado = 'Vacío'

            tipo_asis = row.get('TIPO ASISTENCIA', '').strip()
            if not tipo_asis:
                tipo_asis = 'Presencial'

            pirineos_val = row.get('PIRINEOS', '').strip()
            if not pirineos_val:
                pirineos_val = 'NO'

            row_dict = row.to_dict()
            row_dict['ASIGNADO A'] = asignado
            row_dict['ESTADO'] = estado
            row_dict['TIPO ASISTENCIA'] = tipo_asis
            row_dict['PIRINEOS'] = pirineos_val
            row_dict['OPCIÓN A VENTA'] = row.get('OPCIÓN A VENTA', '').strip() or 'NO'
            row_dict['DETALLE VENTA'] = row.get('DETALLE VENTA', '').strip()
            row_dict['ESTADO PIEZAS'] = row.get('ESTADO PIEZAS', '').strip()
            row_dict['HORAS TOTALES'] = row.get('HORAS TOTALES', '').strip()
            row_dict['Nº SERIE'] = row.get('Nº SERIE', '').strip()
            
            # Incorporar nuevas etiquetas
            row_dict['URGENTE'] = row.get('URGENTE', '').strip()
            row_dict['GARANTÍA'] = row.get('GARANTÍA', '').strip()
            row_dict['MANTENIMIENTO'] = row.get('MANTENIMIENTO', '').strip()
            row_dict['INSTALACIÓN'] = row.get('INSTALACIÓN', '').strip()
            row_dict['REVISAR'] = row.get('REVISAR', '').strip()
            row_dict['NUEVO'] = row.get('NUEVO', '').strip()
            row_dict['PARADO'] = row.get('PARADO', '').strip()
            row_dict['RECLAMA'] = row.get('RECLAMA', '').strip()
            row_dict['PRESUPUESTO'] = row.get('PRESUPUESTO', '').strip()
            row_dict['PIEZAS'] = row.get('PIEZAS', '').strip()
            
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

@router.post("/guardar-progreso")
def guardar_progreso(parte: UpdateParte):
    try:
        drive = get_drive_service()
        procesar_actualizacion_tratados(drive, parte, "Abierto")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/cerrar-parte-y-enviar")
def cerrar_parte_y_enviar(payload: CerrarYEnviarParte):
    try:
        drive = get_drive_service()
        
        # Procesamos el pdf y guardamos el original
        num_parte = get_and_increment_part_number(drive)
        pdf_bytes = generar_pdf_parte(payload, num_parte)

        attempted, sent, err = enviar_email_cierre(payload, pdf_bytes, num_parte)
        horas = procesar_actualizacion_tratados(drive, payload.base_parte, "Cerrado")

        # LOGICA DE REGENERADO DE AVISOS EN CASO DE QUEDAR PENDIENTE
        if payload.base_parte.resuelto_pendiente == "Pendiente":
            fid_sin, df_sin = get_excel(drive, 'Avisos Sin Tratar.xlsx', COLS_SIN)
            fid_tra, df_tra = get_excel(drive, 'Avisos Tratados.xlsx', COLS_TRA)
            orig_row = df_tra.iloc[int(payload.base_parte.aviso_index)].to_dict()
            
            fecha_ahora = datetime.now().strftime("%Y-%m-%d")

            new_row = {c: '' for c in COLS_SIN}
            new_row['F. ENTR.'] = fecha_ahora
            new_row['CLIENTE'] = payload.base_parte.cliente
            new_row['POBLACIÓN'] = payload.base_parte.poblacion
            new_row['MÁQUINA'] = payload.base_parte.maquina
            new_row['EQUIPO'] = orig_row.get('EQUIPO', '')
            new_row['MARCA'] = payload.pdf_marca
            new_row['MODELO'] = payload.pdf_modelo
            new_row['Nº SERIE'] = payload.pdf_n_serie
            new_row['F. GARANTÍA'] = orig_row.get('F. GARANTÍA', '')
            new_row['F. INSTALACIÓN'] = orig_row.get('F. INSTALACIÓN', '')
            
            # El motivo es la solución dada
            new_row['DESCRIPCIÓN'] = payload.base_parte.solucion
            
            new_row['ASIGNADO A'] = 'Pendiente' # Queda para que asigne Master
            new_row['PIRINEOS'] = 'NO'
            
            # Propagar las etiquetas preexistentes
            for label in ['URGENTE', 'GARANTÍA', 'MANTENIMIENTO', 'INSTALACIÓN', 'REVISAR', 'PARADO', 'RECLAMA', 'PRESUPUESTO']:
                new_row[label] = orig_row.get(label, 'NO')
            
            new_row['NUEVO'] = 'SI'
            
            if payload.base_parte.piezas and payload.base_parte.piezas.strip() != "Se necesitan piezas? NO":
                new_row['PIEZAS'] = 'SI'
                new_row['ESTADO PIEZAS'] = 'Pendiente'
            else:
                new_row['PIEZAS'] = 'NO'
                new_row['ESTADO PIEZAS'] = ''
                
            new_row['OBSERVACIONES'] = orig_row.get('OBSERVACIONES', '')

            df_sin = pd.concat([df_sin, pd.DataFrame([new_row])], ignore_index=True)
            save_excel(drive, fid_sin, 'Avisos Sin Tratar.xlsx', df_sin)

        return {
            "status": "ok",
            "email_sent": sent,
            "email_error": err
        }
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
