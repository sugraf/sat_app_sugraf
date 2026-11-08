# comercial/correo.py
"""Avisos por correo del módulo Comercial.

Usa la misma cuenta de Gmail que los partes del SAT (sugraf.digitalhub@gmail.com, contraseña de
aplicación GMAIL_PASSWORD en contraseñas.txt). Si el correo falla, lo apuntado se guarda igual.
"""
import logging
import os
import smtplib
from email.message import EmailMessage

logger = logging.getLogger('correo_comercial')

REMITENTE = 'sugraf.digitalhub@gmail.com'
# Quién recibe un aviso cada vez que un comercial apunta un precio dado.
DESTINOS_PRECIOS = ['lucia@sugraf.es', 'carolina@sugraf.es']


def _eur(n):
    try:
        return f'{float(n):,.2f} €'.replace(',', 'X').replace('.', ',').replace('X', '.')
    except (TypeError, ValueError):
        return '—'


def enviar(destinos, asunto, cuerpo):
    """(ok, error). En pruebas locales (COMERCIAL_LOCAL_DIR) no envía nada."""
    if os.environ.get('COMERCIAL_LOCAL_DIR'):
        logger.info('[pruebas] correo a %s: %s\n%s', destinos, asunto, cuerpo)
        return True, None
    try:
        from secretos import obtener
        clave = obtener('GMAIL_PASSWORD')
    except Exception as e:
        return False, f'No se pudo leer la contraseña del correo: {e}'
    msg = EmailMessage()
    msg['Subject'], msg['From'], msg['To'] = asunto, f'Sugraf Comercial <{REMITENTE}>', ', '.join(destinos)
    msg.set_content(cuerpo)
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=15) as smtp:
            smtp.login(REMITENTE, clave)
            smtp.send_message(msg)
        return True, None
    except Exception as e:
        logger.exception('No se ha podido enviar el correo')
        return False, str(e)


def aviso_precio_dado(comercial, cliente, cod_cliente, referencia, descripcion, precio, nota, momento, precio_cliente=None, otros_precios=None,
                      precio_bajo=None):
    asunto = ('⚠ PRECIO BAJO · ' if precio_bajo else '') + f'Precio dado · {cliente} · {referencia} a {_eur(precio)} ({comercial})'
    lineas = [
        f'{comercial} ha apuntado un precio dado en la app comercial.',
        '',
        f'Cliente:      {cliente}' + (f' (código {cod_cliente})' if cod_cliente else ''),
        f'Artículo:     {referencia} · {descripcion}',
        f'Precio dado:  {_eur(precio)}',
    ]
    if precio_cliente:
        lineas.append(f'Precio que le corresponde: {_eur(precio_cliente["precio"])} ({precio_cliente["texto"]})')
        if precio_cliente['precio']:
            dif = (float(precio) - precio_cliente['precio']) / precio_cliente['precio'] * 100
            lineas.append(f'Diferencia:   {dif:+.1f} %'.replace('.', ','))
    if precio_bajo:
        lineas.append(f"ATENCIÓN:     margen del {precio_bajo['margen']:g} %, por debajo del mínimo del {precio_bajo['minimo']:g} %".replace('.', ','))
    if nota:
        lineas.append(f'Nota:         {nota}')
    if otros_precios:
        lineas += ['', 'Precios dados antes a este cliente para esta referencia:'] + [f'  · {x}' for x in otros_precios]
    lineas += ['', f'Fecha: {momento}', '', 'Lo tienes en la ficha del cliente, pestaña «Precios dados».']
    return enviar(DESTINOS_PRECIOS, asunto, '\n'.join(lineas))
