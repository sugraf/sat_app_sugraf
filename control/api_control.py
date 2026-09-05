# control/api_control.py
import io
import json
import os
import traceback
import logging
import time
from fastapi import APIRouter, HTTPException
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from pydantic import BaseModel
import pandas as pd
from openai import OpenAI, APIError, AuthenticationError, RateLimitError, APIConnectionError

# ==========================================
# CONFIGURACIÓN DE LOGGING ESTRUCTURADO
# ==========================================
logger = logging.getLogger("api_control")
logger.setLevel(logging.INFO)
if not logger.handlers:
    ch = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s | %(levelname)s | [%(funcName)s] | %(message)s')
    ch.setFormatter(formatter)
    logger.addHandler(ch)

# ==========================================
# VARIABLES GLOBALES
# ==========================================
router = APIRouter()
FOLDER_ID = "1nK7_foRIcGb9oasij7spOn0kOLQHmVYb"
GROQ_API_KEY = "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"
# MODELO ACTUALIZADO (El anterior fue dado de baja por Groq)
MODEL_NAME = "llama-3.1-8b-instant" 

COLS_SIN = ['F. ENTR.', 'CLIENTE', 'POBLACIÓN', 'MÁQUINA', 'EQUIPO', 'MARCA', 'MODELO', 'F. GARANTÍA', 'F. INSTALACIÓN', 'DESCRIPCIÓN', 'REALIZADO POR', 'URGENTE', 'PIRINEOS', 'GARANTÍA', 'MANTENIMIENTO', 'INSTALACIÓN', 'REVISAR']
COLS_TRA = COLS_SIN + ['FECHA REALIZACIÓN', 'HORA ENTRADA', 'HORA SALIDA', 'HORAS TOTALES', 'RESUELTO O PENDIENTE', 'PIEZAS NECESARIAS', 'SOLUCIÓN', 'ESTADO', 'TIPO ASISTENCIA']

masked_api_key = f"{GROQ_API_KEY[:4]}...{GROQ_API_KEY[-4:]}" if len(GROQ_API_KEY) > 8 else "INVALID_KEY"
logger.info(f"Inicializando cliente OpenAI (Groq). API Key usada: {masked_api_key}")

client = OpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1"
)

# ==========================================
# FUNCIONES AUXILIARES
# ==========================================
def get_drive_service():
    logger.info("Iniciando validación de credenciales de Google Drive...")
    creds_raw = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    
    if not creds_raw:
        logger.error("La variable de entorno GOOGLE_CREDENTIALS_JSON no existe o está vacía.")
        raise ValueError("Faltan las credenciales de Google Drive (GOOGLE_CREDENTIALS_JSON).")
    
    try:
        creds_dict = json.loads(creds_raw)
        logger.info("Credenciales JSON decodificadas correctamente (secretos ocultos).")
    except json.JSONDecodeError as e:
        logger.error(f"Error decodificando GOOGLE_CREDENTIALS_JSON: {str(e)}")
        raise ValueError("El formato de GOOGLE_CREDENTIALS_JSON es inválido.")
        
    try:
        service = build("drive", "v3", credentials=service_account.Credentials.from_service_account_info(
            creds_dict, scopes=["https://www.googleapis.com/auth/drive"]
        ))
        logger.info("Servicio de Google Drive (API v3) creado con éxito.")
        return service
    except Exception as e:
        logger.error(f"Fallo al construir el servicio de Drive: {str(e)}")
        raise Exception(f"GoogleDriveAuthError: {str(e)}")


def get_excel(drive, name, cols):
    logger.info(f"Buscando archivo en Drive: '{name}' en carpeta '{FOLDER_ID}'...")
    query = f"'{FOLDER_ID}' in parents and name = '{name}' and trashed = false"
    
    try:
        res = drive.files().list(q=query, fields="files(id, mimeType)").execute()
    except Exception as e:
        logger.error(f"Fallo en la API de Drive al buscar '{name}': {str(e)}")
        raise Exception(f"DriveSearchError: No se pudo buscar el archivo {name}. Detalle: {str(e)}")

    if not res.get("files"):
        logger.error(f"Archivo '{name}' no encontrado en la carpeta '{FOLDER_ID}'.")
        raise Exception(f"FileNotFoundError: El archivo {name} no existe en Drive.")

    file_info = res.get("files")[0]
    file_id = file_info['id']
    mime_type = file_info.get('mimeType', '')
    logger.info(f"Archivo '{name}' encontrado. ID: {file_id} | MimeType: {mime_type}")
    
    try:
        logger.info(f"Iniciando descarga de '{name}'...")
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
        logger.info(f"Descarga de '{name}' finalizada. Tamaño en memoria: {len(fh.getvalue())} bytes.")
    except Exception as e:
        logger.error(f"Error durante la descarga de '{name}': {str(e)}")
        raise Exception(f"DriveDownloadError: Fallo al descargar {name}. Detalle: {str(e)}")

    try:
        logger.info(f"Procesando '{name}' con pandas...")
        df = pd.read_excel(fh, engine='openpyxl', dtype=str).fillna("")
        logger.info(f"Excel '{name}' procesado con éxito. Filas: {len(df)}, Columnas: {len(df.columns)}")
        
        missing_cols = [c for c in cols if c not in df.columns]
        if missing_cols:
            logger.info(f"Añadiendo {len(missing_cols)} columnas faltantes a '{name}'.")
            for c in missing_cols:
                df[c] = ""
        return df
    except Exception as e:
        logger.error(f"Error de pandas leyendo '{name}': {str(e)}")
        raise Exception(f"PandasError: El archivo {name} está corrupto o no es legible. Detalle: {str(e)}")


# ==========================================
# RUTAS DE LA APLICACIÓN
# ==========================================
class ChatRequest(BaseModel):
    query: str
    mode: str

@router.post("/chat")
def chat_ia(req: ChatRequest):
    start_time = time.time()
    logger.info(f"=== INICIO PETICIÓN /chat | Mode: '{req.mode}' | Query length: {len(req.query)} ===")
    
    try:
        drive = get_drive_service()
        
        if req.mode == "abiertos":
            df_sin = get_excel(drive, 'Avisos Sin Tratar.xlsx', COLS_SIN)
            df_tra = get_excel(drive, 'Avisos Tratados.xlsx', COLS_TRA)
            
            logger.info("Concatenando DataFrames (Avisos Sin Tratar + Avisos Tratados)...")
            df_combined = pd.concat([df_sin, df_tra], ignore_index=True)
            df_combined = df_combined.fillna("")
            context_text = df_combined.to_csv(index=False)
            logger.info(f"Contexto generado. Filas totales: {len(df_combined)}")
        else:
            df_fin = get_excel(drive, 'Partes Finalizados.xlsx', COLS_TRA)
            context_text = df_fin.to_csv(index=False)
            logger.info(f"Contexto generado. Filas totales: {len(df_fin)}")
        
        context_len = len(context_text)
        logger.info(f"Longitud del contexto CSV en texto: {context_len} caracteres.")
        
        if context_len > 20000:
            logger.warning(f"El contexto supera los 20000 caracteres ({context_len}). Truncando datos para evitar error de tokens.")
            context_text = context_text[:20000] + "\n...[TRUNCATED]"

        system_prompt = (
            "You are an expert technical service manager assistant. "
            "Answer the user's question using ONLY the provided context data. "
            "When routing or assigning technicians to locations, NEVER prioritize older tickets or give preference based on antiquity. "
            "Answer in the same language the user asks in."
        )
        
        user_prompt = f"Context Data (CSV Format):\n{context_text}\n\nQuestion: {req.query}"
        
        logger.info(f"Llamando a Groq API con el modelo '{MODEL_NAME}'...")
        groq_start = time.time()
        
        try:
            response = client.chat.completions.create(
                model=MODEL_NAME, 
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.1
            )
            groq_time = round(time.time() - groq_start, 2)
            logger.info(f"Respuesta de Groq recibida con éxito en {groq_time}s.")
            
        except AuthenticationError as e:
            logger.error("Fallo de autenticación con Groq. La API Key puede ser inválida.")
            raise Exception(f"GroqAuthError: Verifica tu API Key. {str(e)}")
        except RateLimitError as e:
            logger.error("Límite de peticiones de Groq superado.")
            raise Exception(f"GroqRateLimitError: Has alcanzado el límite de uso de Groq. {str(e)}")
        except APIConnectionError as e:
            logger.error("Error de conexión a la API de Groq.")
            raise Exception(f"GroqConnectionError: Fallo de red hacia Groq. {str(e)}")
        except APIError as e:
            logger.error(f"Error devuelto por la API de Groq: {str(e)}")
            if "model" in str(e).lower() or "does not exist" in str(e).lower() or "decommissioned" in str(e).lower():
                 raise Exception(f"GroqModelError: El modelo '{MODEL_NAME}' ha sido dado de baja o no existe. {str(e)}")
            raise Exception(f"GroqAPIError: {str(e)}")
            
        total_time = round(time.time() - start_time, 2)
        logger.info(f"=== FIN PETICIÓN /chat | Tiempo total: {total_time}s ===")
        
        return {"reply": response.choices[0].message.content}
        
    except Exception as e:
        total_time = round(time.time() - start_time, 2)
        error_trace = traceback.format_exc()
        logger.error(f"!!! CRASH EN /chat ({total_time}s) !!!\n{error_trace}")
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================
# ENDPOINTS DE DIAGNÓSTICO
# ==========================================

@router.get("/test-groq")
def test_groq():
    logger.info("=== INICIO /test-groq ===")
    try:
        res = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": "Say 'hello world' and nothing else."}],
            max_tokens=10
        )
        logger.info("Groq ha respondido correctamente.")
        return {
            "success": True,
            "stage": "Groq API Connectivity",
            "model_tested": MODEL_NAME,
            "response": res.choices[0].message.content,
            "message": "La API Key y el modelo funcionan perfectamente."
        }
    except Exception as e:
        logger.error(f"Fallo en /test-groq: {str(e)}")
        return {
            "success": False,
            "stage": "Groq API Connectivity",
            "model_tested": MODEL_NAME,
            "error_type": type(e).__name__,
            "message": str(e)
        }

@router.get("/test-drive")
def test_drive():
    logger.info("=== INICIO /test-drive ===")
    try:
        drive = get_drive_service()
        query = f"'{FOLDER_ID}' in parents and trashed = false"
        res = drive.files().list(q=query, fields="files(id, name, mimeType)").execute()
        files = res.get("files", [])
        
        logger.info(f"Se han encontrado {len(files)} archivos en la carpeta.")
        return {
            "success": True,
            "stage": "Google Drive Connectivity",
            "folder_id": FOLDER_ID,
            "files_found": len(files),
            "file_list": [{"name": f["name"], "type": f["mimeType"]} for f in files],
            "message": "Credenciales correctas y acceso a la carpeta confirmado."
        }
    except Exception as e:
        logger.error(f"Fallo en /test-drive: {str(e)}")
        return {
            "success": False,
            "stage": "Google Drive Connectivity",
            "folder_id": FOLDER_ID,
            "error_type": type(e).__name__,
            "message": str(e)
        }
