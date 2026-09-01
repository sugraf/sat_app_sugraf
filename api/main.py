import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from api.generador import router as generador_router
from api.gestor import router as gestor_router

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

@app.get("/", response_class=HTMLResponse)
def home():
    ruta = os.path.join(os.path.dirname(__file__), "..", "index.html")
    if os.path.exists(ruta):
        with open(ruta, "r", encoding="utf-8") as f:
            return f.read()
    return "Error: index.html not found."

@app.get("/{filename}.jpg")
def get_jpg(filename: str):
    ruta = os.path.join(os.path.dirname(__file__), "..", f"{filename}.jpg")
    if os.path.exists(ruta): return FileResponse(ruta, media_type="image/jpeg")
    raise HTTPException(status_code=404, detail="Not found")

@app.get("/{filename}.png")
def get_png(filename: str):
    ruta = os.path.join(os.path.dirname(__file__), "..", f"{filename}.png")
    if os.path.exists(ruta): return FileResponse(ruta, media_type="image/png")
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
