# main.py_prueba_publico
import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from generador.api_generador import router as generador_router
from gestor.api_gestor import router as gestor_router
from adjudicados.api_adjudicados import router as adjudicados_router
from control.api_control import router as control_router

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(generador_router, prefix="/api/generador")
app.include_router(gestor_router, prefix="/api/gestor")
app.include_router(adjudicados_router, prefix="/api/adjudicados")
app.include_router(control_router, prefix="/api/control")

def get_html(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return f"Error: {path} no encontrado."

@app.get("/", response_class=HTMLResponse)
def home():
    return get_html(os.path.join(os.path.dirname(__file__), "index.html"))

@app.get("/vista/generador", response_class=HTMLResponse)
def view_generador():
    return get_html(os.path.join(os.path.dirname(__file__), "generador", "generador.html"))

@app.get("/vista/gestor", response_class=HTMLResponse)
def view_gestor():
    return get_html(os.path.join(os.path.dirname(__file__), "gestor", "gestor.html"))

@app.get("/vista/adjudicados", response_class=HTMLResponse)
def view_adjudicados():
    return get_html(os.path.join(os.path.dirname(__file__), "adjudicados", "adjudicados.html"))

@app.get("/vista/control", response_class=HTMLResponse)
def view_control():
    return get_html(os.path.join(os.path.dirname(__file__), "control", "control.html"))

@app.get("/{filename}.png")
def get_png(filename: str):
    ruta = os.path.join(os.path.dirname(__file__), f"{filename}.png")
    if os.path.exists(ruta): return FileResponse(ruta, media_type="image/png")
    raise HTTPException(status_code=404, detail="Not found")

@app.get("/{filename}.js")
def get_js(filename: str):
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=404, detail="Not found")
    ruta = os.path.join(os.path.dirname(__file__), f"{filename}.js")
    if os.path.exists(ruta): return FileResponse(ruta, media_type="application/javascript")
    raise HTTPException(status_code=404, detail="Not found")

@app.get("/{filename}.css")
def get_css(filename: str):
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=404, detail="Not found")
    ruta = os.path.join(os.path.dirname(__file__), f"{filename}.css")
    if os.path.exists(ruta): return FileResponse(ruta, media_type="text/css")
    raise HTTPException(status_code=404, detail="Not found")

@app.get("/manifest.json")
def get_manifest():
    return JSONResponse(content={
        "name": "Sugraf Digital Manager",
        "short_name": "Sugraf Hub",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#070e1c",
        "theme_color": "#ffffff",
        "icons": [{"src": "/logo_ejecutable.png", "sizes": "512x512", "type": "image/png"}]
    })
