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
# Quién recibe los pedidos que mandan los comerciales desde la pestaña «Pedidos».
DESTINOS_PEDIDOS = ['carolina@sugraf.es']


def _eur(n):
    try:
        return f'{float(n):,.2f} €'.replace(',', 'X').replace('.', ',').replace('X', '.')
    except (TypeError, ValueError):
        return '—'


def enviar(destinos, asunto, cuerpo, adjuntos=None):
    """(ok, error). adjuntos: [(nombre, bytes, 'image/jpeg'), ...]. En pruebas locales (COMERCIAL_LOCAL_DIR) no envía nada."""
    if os.environ.get('COMERCIAL_LOCAL_DIR'):
        logger.info('[pruebas] correo a %s: %s\n%s\nAdjuntos: %s', destinos, asunto, cuerpo,
                    [(n, len(b)) for n, b, _ in (adjuntos or [])])
        return True, None
    try:
        from secretos import obtener
        clave = obtener('GMAIL_PASSWORD')
    except Exception as e:
        return False, f'No se pudo leer la contraseña del correo: {e}'
    msg = EmailMessage()
    msg['Subject'], msg['From'], msg['To'] = asunto, f'Sugraf Comercial <{REMITENTE}>', ', '.join(destinos)
    msg.set_content(cuerpo)
    for nombre, datos, tipo in adjuntos or []:
        principal, sub_tipo = tipo.split('/', 1)
        msg.add_attachment(datos, maintype=principal, subtype=sub_tipo, filename=nombre)
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


def _cantidad(n):
    try:
        n = float(n)
        return str(int(n)) if n.is_integer() else f'{n:g}'.replace('.', ',')
    except (TypeError, ValueError):
        return str(n or '')


def pedido(p, fotos=None):
    """Correo a la oficina con un pedido de la app comercial (p: el pedido ya validado). fotos: adjuntos
    [(nombre, bytes, tipo)] hechos con el móvil al añadir las piezas (etiquetas, referencias...)."""
    n = len(p['lineas'])
    asunto = f"Pedido de {p['tipo'].lower()} · {p['cliente']} · {n} línea{'s' if n != 1 else ''} ({p['comercial']})"
    lineas = [
        f"{p['comercial']} ha enviado un pedido desde la app comercial.",
        '',
        f"Cliente:       {p['cliente']}" + (f" (código {p['cod_cliente']})" if p.get('cod_cliente') else ' (sin código: no está en Pirineos)'),
        f"Tipo:          {p['tipo']}",
    ]
    lineas += ['', f"{'PIEZAS' if p['tipo'] == 'Piezas' else 'MÁQUINAS'}:"]
    for i, l in enumerate(p['lineas'], 1):
        art = ' · '.join(x for x in (l.get('referencia'), l.get('descripcion')) if x)
        if not art:
            lineas.append(f"  {i}. {_cantidad(l['cantidad'])} × (sin referencia: ver la foto {l.get('foto', '')})")
        else:
            lineas.append(f"  {i}. {_cantidad(l['cantidad'])} × {art}" + ('' if l.get('referencia') else '   (sin referencia de Pirineos)'))
        if l.get('nota'):
            lineas.append(f"     Nota: {l['nota']}")
        if l.get('foto') and art:
            lineas.append(f"     Foto adjunta: {l['foto']}")
    if p.get('observaciones'):
        lineas += ['', 'Comentarios:', p['observaciones']]
    if p.get('contacto'):
        lineas += ['', f"Persona de contacto: {p['contacto']}"]
    lineas += ['', f"Enviado: {p['creado_txt']} · Nº {p['id']}"]
    return enviar(DESTINOS_PEDIDOS, asunto, '\n'.join(lineas), fotos)
