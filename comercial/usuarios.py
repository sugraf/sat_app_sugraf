# comercial/usuarios.py
"""Usuarios, roles, permisos y sesión del módulo Comercial.

Las contraseñas NO están en el código: se leen del fichero "contraseñas.txt" de la carpeta
Media de Drive (ver secretos.py), con una línea por usuario:

    COMERCIAL_ARTEMIO = ...
    COMERCIAL_SERGIO = ...
    COMERCIAL_HUGO = ...
    COMERCIAL_MASTER = ...


Al entrar, el servidor comprueba la contraseña y entrega un token firmado (30 días). Cada
llamada a /api/comercial/... lleva ese token, de modo que los permisos (por ejemplo, ver
costes y márgenes) se aplican en el servidor y no dependen de lo que muestre la pantalla.
"""
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import unicodedata

from fastapi import Header, HTTPException

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

DURACION_TOKEN = 30 * 24 * 3600

# clave -> datos. 'nombre' es el que se ve en la app (y el de la lista de acceso de la
# pantalla de inicio). 'pirineos' es como aparece el comercial en el maestro de clientes.
USUARIOS = {
    'artemio':  {'nombre': 'Artemio',  'rol': 'comercial', 'pirineos': 'ECHEVERRIBAR FLETA, ARTEMIO'},
    'sergio':   {'nombre': 'Sergio',   'rol': 'comercial', 'pirineos': 'DURBAN MONTANE, SERGIO'},
    'hugo':     {'nombre': 'Hugo',     'rol': 'comercial', 'pirineos': 'ERVITI BARCINA, HUGO'},
    'master':   {'nombre': 'Master',   'rol': 'master',    'pirineos': ''},
}

# Permisos por rol (configurables aquí). Los comerciales solo ven su propia actividad.
PERMISOS = {
    'comercial': {'registrar': True, 'ver_todos': False, 'ver_costes': False, 'editar_otros': False},
    'master':    {'registrar': True, 'ver_todos': True,  'ver_costes': True,  'editar_otros': True},
}


def normalizar(texto):
    texto = unicodedata.normalize('NFD', str(texto or ''))
    texto = ''.join(c for c in texto if unicodedata.category(c) != 'Mn')
    return ' '.join(texto.upper().split())


def clave_por_nombre(nombre):
    n = normalizar(nombre)
    for clave, u in USUARIOS.items():
        if normalizar(u['nombre']) == n or clave == n.lower():
            return clave
    return None


def comerciales():
    """Usuarios que registran actividad comercial (para filtros y listados)."""
    return [{'clave': k, 'nombre': u['nombre'], 'rol': u['rol']} for k, u in USUARIOS.items() if u['rol'] != 'master']


def _secreto_firma():
    propio = os.environ.get('COMERCIAL_TOKEN_SECRET')
    if propio:
        return propio.encode()
    try:
        clave_privada = json.loads(os.environ.get('GOOGLE_CREDENTIALS_JSON') or '{}').get('private_key', '')
    except Exception:
        clave_privada = ''
    if not clave_privada:
        raise HTTPException(status_code=500, detail='Falta configurar la firma de sesiones (GOOGLE_CREDENTIALS_JSON).')
    return hashlib.sha256(('sugraf-comercial|' + clave_privada).encode()).digest()


def _b64(datos):
    return base64.urlsafe_b64encode(datos).decode().rstrip('=')


def _de_b64(texto):
    return base64.urlsafe_b64decode(texto + '=' * (-len(texto) % 4))


def crear_token(clave):
    cuerpo = _b64(json.dumps({'u': clave, 'exp': int(time.time()) + DURACION_TOKEN}).encode())
    firma = _b64(hmac.new(_secreto_firma(), cuerpo.encode(), hashlib.sha256).digest())
    return f'{cuerpo}.{firma}'


def leer_token(token):
    try:
        cuerpo, firma = token.split('.', 1)
        esperada = _b64(hmac.new(_secreto_firma(), cuerpo.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(firma, esperada):
            return None
        datos = json.loads(_de_b64(cuerpo))
        if datos.get('exp', 0) < time.time() or datos.get('u') not in USUARIOS:
            return None
        return datos['u']
    except Exception:
        return None


def comprobar_clave(clave, contrasena):
    from secretos import obtener
    nombre_secreto = f'COMERCIAL_{clave.upper()}'
    try:
        correcta = obtener(nombre_secreto)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=(
            f"El acceso comercial de {USUARIOS[clave]['nombre']} aún no está configurado: añade la línea "
            f"'{nombre_secreto} = <contraseña>' al fichero contraseñas.txt de la carpeta Media de Drive."
        )) from e
    except Exception as e:
        raise HTTPException(status_code=503, detail=f'No se pudieron leer las contraseñas de Drive: {e}') from e
    return hmac.compare_digest(str(contrasena).encode(), str(correcta).encode())


class Sesion:
    def __init__(self, clave):
        self.clave = clave
        datos = USUARIOS[clave]
        self.nombre = datos['nombre']
        self.rol = datos['rol']
        self.pirineos = datos['pirineos']
        self.permisos = PERMISOS[self.rol]

    def puede(self, permiso):
        return bool(self.permisos.get(permiso))

    def publico(self):
        return {'clave': self.clave, 'nombre': self.nombre, 'rol': self.rol, 'permisos': self.permisos}


def sesion_actual(authorization: str = Header(default='')):
    """Dependencia de FastAPI: exige un token válido en 'Authorization: Bearer ...'."""
    token = authorization.split(' ', 1)[1] if authorization.lower().startswith('bearer ') else ''
    clave = leer_token(token) if token else None
    if not clave:
        raise HTTPException(status_code=401, detail='Sesión caducada o no válida. Vuelve a entrar.')
    return Sesion(clave)
