import io
import json
import os
import re
import unicodedata

from fastapi import APIRouter, HTTPException
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
from pydantic import BaseModel
import pandas as pd

router = APIRouter()

EQUIPOS_FILE_ID = "1mNdXqH6RLwXSIXxd9i3eexOMAkaYJKWN"
FOLDER_ID_DEFAULT = "1nK7_foRIcGb9oasij7spOn0kOLQHmVYb"


def get_drive_service():
    creds_raw = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    creds_dict = json.loads(creds_raw)
    creds = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    return build("drive", "v3", credentials=creds)


def clean_text(value):
    """Limpia valores de Excel sin convertir vacíos en 'nan'."""
    if value is None:
        return ""

    if isinstance(value, float) and pd.isna(value):
        return ""

    text = str(value).replace("\u00a0", " ")
    text = re.sub(r"\s+", " ", text).strip()

    if text.lower() in {"nan", "nat", "none"}:
        return ""

    return text


def normalize_key(value):
    """
    Normaliza un texto para poder comparar clientes de forma robusta:
    - elimina espacios sobrantes
    - ignora mayúsculas/minúsculas
    - ignora acentos
    """
    text = clean_text(value)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.casefold()


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
    garantia: bool
    mantenimiento: bool
    instalacion: bool
    revisar: bool


@router.get("/clientes-maquinas")
def listar_clientes_maquinas():
    try:
        drive = get_drive_service()

        try:
            file_metadata = (
                drive.files()
                .get(fileId=EQUIPOS_FILE_ID, fields="mimeType")
                .execute()
            )
        except Exception:
            return {"error": "Error al acceder a Google Drive"}

        mime_type = file_metadata.get("mimeType")

        if mime_type == "application/vnd.google-apps.spreadsheet":
            request = drive.files().export_media(
                fileId=EQUIPOS_FILE_ID,
                mimeType=(
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet"
                ),
            )
        else:
            request = drive.files().get_media(fileId=EQUIPOS_FILE_ID)

        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False

        while not done:
            _, done = downloader.next_chunk()

        fh.seek(0)

        # El Excel de equipos tiene los encabezados en la fila 5.
        df = pd.read_excel(fh, header=4)

        # Normalizamos nombres de columnas para evitar fallos por espacios.
        df.columns = [clean_text(col) for col in df.columns]

        # Comprobación útil para detectar cambios en el Excel.
        required_columns = ["CLIENTE", "POBLACIÓN"]
        missing_columns = [
            col for col in required_columns if col not in df.columns
        ]
        if missing_columns:
            return {
                "error": (
                    "Faltan columnas obligatorias en el Excel: "
                    + ", ".join(missing_columns)
                )
            }

        clientes_map = {}

        for col in ["F.GARAN.", "F. INSTALACION"]:
            if col in df.columns:
                df[col] = df[col].apply(clean_text)

        for _, row in df.iterrows():
            cliente_original = clean_text(row.get("CLIENTE", ""))
            if not cliente_original:
                continue

            cliente_key = normalize_key(cliente_original)

            nombre = clean_text(row.get("NOMBRE", ""))
            equipo = clean_text(row.get("EQUIPO", ""))
            marca = clean_text(row.get("MARCA", ""))
            modelo = clean_text(row.get("MODELO", ""))
            f_garan = clean_text(row.get("F.GARAN.", ""))
            f_instal = clean_text(row.get("F. INSTALACION", ""))
            poblacion = clean_text(row.get("POBLACIÓN", ""))

            if not nombre:
                nombre = f"{equipo} {marca} {modelo}".strip()

            # Si el mismo cliente aparece varias veces en Excel con pequeñas
            # diferencias de formato, se agrupa usando cliente_key.
            if cliente_key not in clientes_map:
                clientes_map[cliente_key] = {
                    "cliente": cliente_original,
                    "poblacion": poblacion,
                    "maquinas": [],
                }
            else:
                # Solo sustituimos la población si todavía no había ninguna.
                if poblacion and not clientes_map[cliente_key]["poblacion"]:
                    clientes_map[cliente_key]["poblacion"] = poblacion

                # Preferimos conservar como nombre visible el primero no vacío.
                if not clientes_map[cliente_key]["cliente"]:
                    clientes_map[cliente_key]["cliente"] = cliente_original

            if nombre:
                maquina_existente = any(
                    m["nombre"] == nombre
                    for m in clientes_map[cliente_key]["maquinas"]
                )

                if not maquina_existente:
                    clientes_map[cliente_key]["maquinas"].append(
                        {
                            "nombre": nombre,
                            "equipo": equipo,
                            "marca": marca,
                            "modelo": modelo,
                            "f_garan": f_garan[:10] if f_garan else "",
                            "f_instal": f_instal[:10] if f_instal else "",
                        }
                    )

        # Para no romper el frontend, devolvemos un objeto cuya clave visible
        # sigue siendo el nombre del cliente. Como los nombres normalizados
        # son solo claves internas, aquí usamos los nombres originales.
        result = {}

        for data in clientes_map.values():
            nombre_cliente = data["cliente"]

            # Si por algún motivo hubiera una colisión exacta, fusionamos.
            if nombre_cliente not in result:
                result[nombre_cliente] = {
                    "poblacion": data["poblacion"],
                    "maquinas": data["maquinas"],
                }
            else:
                if (
                    data["poblacion"]
                    and not result[nombre_cliente]["poblacion"]
                ):
                    result[nombre_cliente]["poblacion"] = data["poblacion"]

                existing_names = {
                    m["nombre"] for m in result[nombre_cliente]["maquinas"]
                }
                for maquina in data["maquinas"]:
                    if maquina["nombre"] not in existing_names:
                        result[nombre_cliente]["maquinas"].append(maquina)

        return result

    except Exception as e:
        return {"error": str(e)}


@router.post("/avisos")
def crear_aviso(aviso: NuevoAviso):
    try:
        drive = get_drive_service()

        query = (
            f"'{FOLDER_ID_DEFAULT}' in parents "
            "and name = 'Avisos Sin Tratar.xlsx' "
            "and trashed = false"
        )

        res = drive.files().list(q=query, fields="files(id)").execute()
        archivos = res.get("files", [])

        cols = [
            "F. ENTR.",
            "CLIENTE",
            "POBLACIÓN",
            "MÁQUINA",
            "EQUIPO",
            "MARCA",
            "MODELO",
            "F. GARANTÍA",
            "F. INSTALACIÓN",
            "DESCRIPCIÓN",
            "REALIZADO POR",
            "URGENTE",
            "PIRINEOS",
            "GARANTÍA",
            "MANTENIMIENTO",
            "INSTALACIÓN",
            "REVISAR",
        ]

        if archivos:
            file_id = archivos[0]["id"]

            request = drive.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False

            while not done:
                _, done = downloader.next_chunk()

            fh.seek(0)
            df = pd.read_excel(fh)
        else:
            file_id = None
            df = pd.DataFrame(columns=cols)

        if "PIRINEOS" not in df.columns:
            df["PIRINEOS"] = "NO"
        if "REALIZADO POR" not in df.columns:
            df["REALIZADO POR"] = "Pendiente"
        if "GARANTÍA" not in df.columns:
            df["GARANTÍA"] = "NO"
        if "MANTENIMIENTO" not in df.columns:
            df["MANTENIMIENTO"] = "NO"
        if "INSTALACIÓN" not in df.columns:
            df["INSTALACIÓN"] = "NO"
        if "REVISAR" not in df.columns:
            df["REVISAR"] = "NO"

        fila_excel = {
            "F. ENTR.": aviso.fecha_entrada,
            "CLIENTE": aviso.cliente,
            "POBLACIÓN": aviso.poblacion,
            "MÁQUINA": aviso.maquina,
            "EQUIPO": aviso.equipo,
            "MARCA": aviso.marca,
            "MODELO": aviso.modelo,
            "F. GARANTÍA": aviso.f_garan,
            "F. INSTALACIÓN": aviso.f_instal,
            "DESCRIPCIÓN": aviso.descripcion,
            "REALIZADO POR": "Pendiente",
            "PIRINEOS": "NO",
            "URGENTE": "SI" if aviso.urgente else "NO",
            "GARANTÍA": "SI" if aviso.garantia else "NO",
            "MANTENIMIENTO": "SI" if aviso.mantenimiento else "NO",
            "INSTALACIÓN": "SI" if aviso.instalacion else "NO",
            "REVISAR": "SI" if aviso.revisar else "NO",
        }

        df = pd.concat(
            [df, pd.DataFrame([fila_excel])],
            ignore_index=True,
        )

        output = io.BytesIO()

        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            df.to_excel(writer, index=False)

        output.seek(0)

        media = MediaIoBaseUpload(
            io.BytesIO(output.getvalue()),
            mimetype=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
        )

        if file_id:
            drive.files().update(
                fileId=file_id,
                media_body=media,
            ).execute()
        else:
            drive.files().create(
                body={
                    "name": "Avisos Sin Tratar.xlsx",
                    "parents": [FOLDER_ID_DEFAULT],
                },
                media_body=media,
            ).execute()

        return {"status": "ok"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
