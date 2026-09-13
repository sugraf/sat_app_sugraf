import json
import os
from datetime import datetime

def registrar_movimiento(tipo: str, datos: dict):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    ruta_json = os.path.join(base_dir, "movimientos.json")
    
    registro = {"creacion": [], "borrado": [], "cierre": []}
    
    if os.path.exists(ruta_json):
        try:
            with open(ruta_json, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    data_leida = json.loads(content)
                    for k in registro:
                        if k in data_leida:
                            registro[k] = data_leida[k]
        except Exception:
            pass 
    
    nuevo_movimiento = {
        "fecha_accion": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "datos": datos
    }
    
    if tipo in registro:
        registro[tipo].append(nuevo_movimiento)
        
        if len(registro[tipo]) > 50:
            registro[tipo] = registro[tipo][-50:]
            
    with open(ruta_json, "w", encoding="utf-8") as f:
        json.dump(registro, f, indent=4, ensure_ascii=False)
