import io
import json
import os
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from pydantic import BaseModel

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FOLDER_ID_DEFAULT = "1nK7_foRIcGb9oasij7spOn0kOLQHmVYb"

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <meta name="theme-color" content="#2563eb">
  
  <!-- Configuración PWA / Modo App Nativa -->
  <link rel="manifest" href="/manifest.json">
  <meta name="mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta name="apple-mobile-web-app-title" content="SAT App">
  
  <title>Servicio Técnico</title>
  <style>
    * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 0; }
    body { background: #f1f5f9; color: #1e293b; padding-bottom: 40px; }
    header { background: #2563eb; color: white; padding: 16px; text-align: center; position: sticky; top: 0; z-index: 10; box-shadow: 0 2px 6px rgba(0,0,0,0.1); }
    h1 { font-size: 1.15rem; font-weight: 700; }
    .container { max-width: 480px; margin: 0 auto; padding: 14px; }
    .card { background: white; border-radius: 12px; padding: 18px; box-shadow: 0 2px 4px rgba(0,0,0,0.04); margin-bottom: 14px; }
    label { display: block; font-size: 0.8rem; font-weight: 700; color: #475569; margin-bottom: 5px; text-transform: uppercase; }
    input, textarea, select { width: 100%; padding: 12px; border: 1px solid #cbd5e1; border-radius: 8px; font-size: 0.95rem; margin-bottom: 14px; background: #fff; }
    input:focus, textarea:focus, select:focus { outline: 2px solid #2563eb; border-color: transparent; }
    button { width: 100%; background: #2563eb; color: white; font-weight: 700; border: none; padding: 14px; border-radius: 8px; font-size: 1rem; cursor: pointer; }
    button:active { background: #1d4ed8; }
    button:disabled { background: #94a3b8; }
    #alerta { display: none; padding: 12px; border-radius: 8px; margin-top: 14px; text-align: center; font-weight: 600; font-size: 0.9rem; }
    .success { background: #dcfce7; color: #15803d; }
    .error { background: #fee2e2; color: #b91c1c; }
    .nav-tabs { display: flex; gap: 8px; margin-bottom: 14px; }
    .tab-btn { flex: 1; padding: 10px; background: #e2e8f0; color: #475569; border-radius: 8px; font-size: 0.9rem; text-align: center; font-weight: 600; cursor: pointer; }
    .tab-btn.active { background: #2563eb; color: white; }
    .hidden { display: none; }
    .file-item { padding: 10px; border-bottom: 1px solid #e2e8f0; font-size: 0.85rem; }
    .file-item:last-child { border-bottom: none; }
  </style>
</head>
<body>

  <header>
    <h1>Servicio Técnico</h1>
  </header>

  <div class="container">
    <div class="nav-tabs">
      <div class="tab-btn active" id="tabCrear" onclick="cambiarVista('crear')">Nuevo Parte</div>
      <div class="tab-btn" id="tabVer" onclick="cambiarVista('ver')">Ver Partes</div>
    </div>

    <div id="vistaCrear" class="card">
      <form id="parteForm">
        <label>Nombre del Cliente / Empresa</label>
        <input type="text" id="cliente" required placeholder="Ej. Restaurante El Molino">

        <label>Teléfono de Contacto</label>
        <input type="tel" id="telefono" placeholder="Ej. 600123456">

        <label>Técnico Responsable</label>
        <input type="text" id="tecnico" required placeholder="Ej. Juan Pérez">

        <label>Estado del Servicio</label>
        <select id="estado">
          <option value="En Curso">En Curso</option>
          <option value="Pendiente Repuesto">Pendiente Repuesto</option>
          <option value="Finalizado">Finalizado</option>
        </select>

        <label>Descripción de la Avería</label>
        <textarea id="averia" rows="3" required placeholder="Detalles de la avería..."></textarea>

        <label>Trabajo Realizado / Solución</label>
        <textarea id="solucion" rows="3" placeholder="Piezas cambiadas, intervención..."></textarea>

        <button type="submit" id="btnGuardar">Guardar Parte en Drive</button>
      </form>

      <div id="alerta"></div>
    </div>

    <div id="vistaVer" class="card hidden">
      <h3 style="margin-bottom: 12px; font-size: 1rem;">Últimos partes en Drive</h3>
      <div id="listaArchivos">Cargando archivos...</div>
    </div>
  </div>

  <script>
    const FOLDER_ID = "1nK7_foRIcGb9oasij7spOn0kOLQHmVYb";

    function cambiarVista(vista) {
      if (vista === 'crear') {
        document.getElementById('vistaCrear').classList.remove('hidden');
        document.getElementById('vistaVer').classList.add('hidden');
        document.getElementById('tabCrear').classList.add('active');
        document.getElementById('tabVer').classList.remove('active');
      } else {
        document.getElementById('vistaCrear').classList.add('hidden');
        document.getElementById('vistaVer').classList.remove('hidden');
        document.getElementById('tabCrear').classList.remove('active');
        document.getElementById('tabVer').classList.add('active');
        cargarPartes();
      }
    }

    document.getElementById('parteForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = document.getElementById('btnGuardar');
      const alerta = document.getElementById('alerta');
      
      btn.disabled = true;
      btn.innerText = "Guardando en Drive...";
      alerta.style.display = 'none';

      const payload = {
        cliente: document.getElementById('cliente').value,
        telefono: document.getElementById('telefono').value,
        tecnico: document.getElementById('tecnico').value,
        estado: document.getElementById('estado').value,
        averia: document.getElementById('averia').value,
        solucion: document.getElementById('solucion').value,
        folder_id: FOLDER_ID
      };

      try {
        const response = await fetch('/api/guardar-parte', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });

        const data = await response.json();

        if (response.ok && data.status === 'ok') {
          alerta.className = "success";
          alerta.innerText = "✓ Guardado con éxito en Google Drive";
          alerta.style.display = 'block';
          document.getElementById('parteForm').reset();
        } else {
          throw new Error(data.detail || data.error || "Error en el servidor");
        }
      } catch (err) {
        alerta.className = "error";
        alerta.innerText = "Error: " + err.message;
        alerta.style.display = 'block';
      } finally {
        btn.disabled = false;
        btn.innerText = "Guardar Parte en Drive";
      }
    });

    async function cargarPartes() {
      const contenedor = document.getElementById('listaArchivos');
      contenedor.innerHTML = "Cargando...";

      try {
        const res = await fetch(`/api/listar-partes?folder_id=${FOLDER_ID}`);
        const data = await res.json();

        if (data.archivos && data.archivos.length > 0) {
          contenedor.innerHTML = data.archivos.map(f => `
            <div class="file-item">
              <strong>${f.name}</strong><br>
              <span style="color:#64748b; font-size:0.75rem;">${f.createdTime ? f.createdTime.split('T')[0] : ''}</span>
            </div>
          `).join('');
        } else {
          contenedor.innerHTML = "<p style='color:#64748b;'>No hay partes guardados aún.</p>";
        }
      } catch (e) {
        contenedor.innerHTML = "<p style='color:#b91c1c;'>Error al cargar los partes.</p>";
      }
    }
  </script>
</body>
</html>"""

@app.get("/", response_class=HTMLResponse)
def home():
    return HTML_TEMPLATE

# Ruta para servir el manifest directamente
@app.get("/manifest.json")
def get_manifest():
    manifest_data = {
        "name": "Servicio Técnico App",
        "short_name": "SAT App",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#ffffff",
        "theme_color": "#2563eb",
        "icons": [
            {
                "src": "https://cdn-icons-png.flaticon.com/512/942/942748.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any maskable"
            }
        ]
    }
    return JSONResponse(content=manifest_data)

def get_drive_service():
    creds_raw = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_raw:
        raise HTTPException(
            status_code=500,
            detail="Variable GOOGLE_CREDENTIALS_JSON no configurada en Vercel."
        )
    try:
        creds_dict = json.loads(creds_raw)
        creds = service_account.Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/drive"]
        )
        return build("drive", "v3", credentials=creds)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error con credenciales: {str(e)}")

class ParteTrabajo(BaseModel):
    cliente: str
    tecnico: str
    telefono: str
    estado: str
    averia: str
    solucion: str
    folder_id: str

@app.post("/api/guardar-parte")
@app.post("/guardar-parte")
def guardar_parte(parte: ParteTrabajo):
    try:
        drive = get_drive_service()
        fecha_actual = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        contenido = (
            f"=========================================\n"
            f"       PARTE DE SERVICIO TÉCNICO         \n"
            f"=========================================\n"
            f"Fecha de registro: {fecha_actual}\n"
            f"Estado:            {parte.estado.upper()}\n"
            f"Técnico asignado:  {parte.tecnico}\n"
            f"-----------------------------------------\n"
            f"DATOS DEL CLIENTE\n"
            f"Nombre / Empresa:  {parte.cliente}\n"
            f"Teléfono contacto: {parte.telefono}\n"
            f"-----------------------------------------\n"
            f"DESCRIPCIÓN DE LA AVERÍA:\n"
            f"{parte.averia}\n"
            f"-----------------------------------------\n"
            f"TRABAJO REALIZADO / SOLUCIÓN:\n"
            f"{parte.solucion}\n"
            f"=========================================\n"
        )

        nombre_archivo = f"Parte_{parte.cliente.strip().replace(' ', '_')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"

        file_metadata = {
            "name": nombre_archivo,
            "parents": [parte.folder_id or FOLDER_ID_DEFAULT]
        }

        media = MediaIoBaseUpload(
            io.BytesIO(contenido.encode("utf-8")),
            mimetype="text/plain"
        )

        archivo = drive.files().create(
            body=file_metadata,
            media_body=media,
            fields="id, name"
        ).execute()

        return {"status": "ok", "archivo_id": archivo.get("id"), "nombre": archivo.get("name")}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/listar-partes")
@app.get("/listar-partes")
def listar_partes(folder_id: str = FOLDER_ID_DEFAULT):
    try:
        drive = get_drive_service()
        query = f"'{folder_id}' in parents and trashed = false"
        results = drive.files().list(
            q=query,
            fields="files(id, name, createdTime)",
            orderBy="createdTime desc",
            pageSize=20
        ).execute()

        return {"status": "ok", "archivos": results.get("files", [])}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
