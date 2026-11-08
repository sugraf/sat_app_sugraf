# comercial/analisis.py
"""Análisis comercial a partir de las ventas de Pirineos (año en curso + 2 anteriores).

- perfil / estado de cada cliente: activo, en riesgo, dormido o sin compras en el periodo.
- productos que un cliente está dejando de comprar (referencias habituales que ya
  deberían haberse repetido) y familias en las que ha bajado mucho.
- sugerencias de venta cruzada, prudentes y siempre con su motivo:
    a) por máquina: lo que compra la mayoría de clientes con su mismo modelo de máquina;
    b) por familias: familias que casi siempre van juntas en otros clientes.
  Solo se sugiere con un mínimo de clientes de referencia y nunca algo que el cliente ya
  compra (ni otro artículo de la misma subfamilia, que sería un sustituto).

Todo se calcula en memoria y se guarda mientras no cambien los ficheros de ventas.
"""
import statistics
import threading
from collections import Counter, defaultdict
from datetime import date, timedelta

from comercial import pirineos

# Grupos que no son consumo recurrente ni tiene sentido sugerir.
GRUPOS_EXCLUIDOS = {
    'CONTABILIDAD', 'SERVICIO TECNICO', 'SERVICIOS', 'PEQUEÑA MAQUINARIA', 'DIVISION DIGITAL 2 - MAQ DIGITALES',
    'INFORMATICA', 'DIVISION 3D', 'FABRICA',
}
# Familias que dependen de tener una máquina concreta: no se sugieren "por familia"
# (sí por máquina, a quien tiene ese mismo modelo).
GRUPOS_SOLO_POR_MAQUINA = {'RECAMBIOS'}
PALABRAS_SOLO_POR_MAQUINA = ('MAQUINARIA', 'PIEZAS', 'RECAMBIO', 'CABEZAL', 'REPUESTO')
# Máquinas que no se proponen para renovar (accesorios, software...)
NO_RENOVAR = ('CABEZAL', 'COMPRESOR', 'SOFTWARE', 'RIP', 'LICENCIA', 'MUEBLE', 'MESA DE', 'SOPORTE')
ANIOS_RENOVACION = 8

# Criterios (ajustables)
DORMIDO_MIN_DIAS = 120         # además de 3 veces su intervalo habitual
RIESGO_MIN_DIAS = 60           # además de 2 veces su intervalo habitual
OCASIONAL_DORMIDO_DIAS = 365   # clientes con pocas compras: dormido si no compra en un año
DEJA_MIN_COMPRAS = 3           # compras (fechas distintas) para considerar un producto habitual
DEJA_MARGEN_DIAS = 45
CAIDA_MIN_IMPORTE = 300.0
CAIDA_MIN_PCT = 50
SUG_MIN_CLIENTES_MAQUINA = 3
SUG_MIN_PROPORCION_MAQUINA = 0.4
SUG_MIN_SOPORTE_FAMILIA = 8
SUG_MIN_CONFIANZA = 0.5
SUG_MIN_LIFT = 1.5
MAX_SUGERENCIAS = 5

_lock = threading.Lock()
_cache = {}


def hoy():
    return date.today()


def _d(iso):
    try:
        return date.fromisoformat(iso[:10])
    except Exception:
        return None


# Trabajos puntuales (reparaciones, portes...): cuentan como compra, pero no como producto
# "habitual" ni se sugieren.
INICIOS_PUNTUALES = ('REPARACION', 'REPARACIÓN', 'REP.', 'REPASAR', 'PORTES', 'PORTE ', 'TRANSPORTE', 'DESPLAZAMIENTO',
                     'MANO DE OBRA', 'MONTAJE', 'INSTALACION', 'INSTALACIÓN', 'ENVIO', 'ENVÍO', 'GASTOS')


def es_puntual(l):
    return l['descripcion'].upper().lstrip(' .-').startswith(INICIOS_PUNTUALES)


def lineas_utiles(lineas):
    return [l for l in lineas if l['tipo'] != 'SAT' and l['grupo'].upper() not in GRUPOS_EXCLUIDOS
            and l['cantidad'] > 0 and l['referencia'] and l['fecha']]


def _intervalo_mediano(fechas):
    fechas = sorted(set(fechas))
    if len(fechas) < 2:
        return None
    difs = [(b - a).days for a, b in zip(fechas, fechas[1:]) if (b - a).days > 0]
    return statistics.median(difs) if difs else None


# ---------------------------------------------------------------- perfil del cliente
def perfil(lineas, ref=None):
    ref = ref or hoy()
    utiles = lineas_utiles(lineas)
    fechas = sorted({_d(l['fecha']) for l in utiles if _d(l['fecha'])})
    p = {'compras': len(fechas), 'ultima': fechas[-1].isoformat() if fechas else '', 'dias': None,
         'intervalo': None, 'ult12': 0.0, 'prev12': 0.0, 'estado': 'sin_compras', 'motivo': ''}
    hace12, hace24 = ref - timedelta(days=365), ref - timedelta(days=730)
    for l in utiles:
        f = _d(l['fecha'])
        if f and f > hace12:
            p['ult12'] += l['importe']
        elif f and f > hace24:
            p['prev12'] += l['importe']
    p['ult12'], p['prev12'] = round(p['ult12'], 2), round(p['prev12'], 2)
    if not fechas:
        p['motivo'] = 'Sin compras en el histórico cargado (desde enero de hace 2 años).'
        return p
    dias = (ref - fechas[-1]).days
    p['dias'] = dias
    intervalo = _intervalo_mediano(fechas)
    p['intervalo'] = round(intervalo) if intervalo else None
    if len(fechas) >= 4 and intervalo:
        if dias > max(3 * intervalo, DORMIDO_MIN_DIAS):
            p['estado'], p['motivo'] = 'dormido', f'Compraba cada ~{round(intervalo)} días y lleva {dias} sin comprar.'
        elif dias > max(2 * intervalo, RIESGO_MIN_DIAS):
            p['estado'], p['motivo'] = 'en_riesgo', f'Compra cada ~{round(intervalo)} días y lleva {dias} sin comprar.'
        else:
            p['estado'] = 'activo'
    elif dias > OCASIONAL_DORMIDO_DIAS:
        p['estado'], p['motivo'] = 'dormido', f'Cliente ocasional ({len(fechas)} compras); lleva {dias} días sin comprar.'
    else:
        p['estado'] = 'activo'
    return p


def perfiles_todos():
    """{codigo: perfil} de todos los clientes con ventas en el periodo (en caché)."""
    ventas, version = pirineos.todas_las_ventas()
    clave = (version, hoy().isoformat())
    with _lock:
        if _cache.get('perfiles', (None,))[0] == clave:
            return _cache['perfiles'][1]
    resultado = {cod: perfil(lineas) for cod, lineas in ventas.items()}
    with _lock:
        _cache['perfiles'] = (clave, resultado)
    return resultado


# ---------------------------------------------------------------- deja de comprar
def dejando_de_comprar(lineas, ref=None):
    """Referencias habituales que ya tendría que haber vuelto a comprar."""
    ref = ref or hoy()
    utiles = [l for l in lineas_utiles(lineas) if not es_puntual(l)]
    por_ref = defaultdict(list)
    for l in utiles:
        por_ref[l['referencia']].append(l)
    def grupo_sustitucion(l):
        # Un sustituto es algo de la misma subfamilia o, si no hay subfamilia, de la misma
        # familia y con las dos primeras palabras de la descripción iguales.
        if l.get('subfamilia'):
            return (l['familia'], l['subfamilia'])
        return (l['familia'], ' '.join(l['descripcion'].split()[:2]))

    ultimas_por_subfamilia = defaultdict(list)
    for l in utiles:
        ultimas_por_subfamilia[grupo_sustitucion(l)].append(l)

    avisos = []
    for referencia, ls in por_ref.items():
        fechas = sorted({_d(l['fecha']) for l in ls if _d(l['fecha'])})
        if len(fechas) < DEJA_MIN_COMPRAS or (fechas[-1] - fechas[0]).days < 60:
            continue
        intervalo = _intervalo_mediano(fechas)
        dias = (ref - fechas[-1]).days
        if not intervalo or dias <= max(2 * intervalo, intervalo + DEJA_MARGEN_DIAS) or dias > 730:
            continue
        ultima = max(ls, key=lambda l: l['fecha'])
        # ¿Ha comprado después otro artículo de la misma familia? Puede ser un sustituto.
        sustituto = None
        posteriores = [l for l in ultimas_por_subfamilia[grupo_sustitucion(ultima)]
                       if l['referencia'] != referencia and l['fecha'] > ultima['fecha']]
        if posteriores:
            c = Counter((l['referencia'], l['descripcion']) for l in posteriores).most_common(1)[0][0]
            sustituto = {'referencia': c[0], 'descripcion': c[1]}
        anual = sum(l['importe'] for l in ls if _d(l['fecha']) and (ref - _d(l['fecha'])).days <= 730) / 2
        avisos.append({
            'referencia': referencia, 'descripcion': ultima['descripcion'], 'familia': ultima['familia'],
            'compras': len(fechas), 'cada_dias': round(intervalo), 'ultima': fechas[-1].isoformat(),
            'dias_sin_comprar': dias, 'retraso_dias': round(dias - intervalo),
            'cantidad_habitual': statistics.median([l['cantidad'] for l in ls]),
            'ultimo_precio': ultima['precio'], 'importe_anual': round(anual, 2), 'posible_sustituto': sustituto,
        })
    # Primero lo que más importa: importe anual, y los que tienen sustituto al final.
    avisos.sort(key=lambda a: (a['posible_sustituto'] is not None, -a['importe_anual']))
    return avisos


def familias_en_caida(lineas, ref=None):
    ref = ref or hoy()
    hace12, hace24 = ref - timedelta(days=365), ref - timedelta(days=730)
    ult, prev = defaultdict(float), defaultdict(float)
    for l in lineas_utiles(lineas):
        f = _d(l['fecha'])
        if not f:
            continue
        if f > hace12:
            ult[l['familia']] += l['importe']
        elif f > hace24:
            prev[l['familia']] += l['importe']
    resultado = []
    for fam, antes in prev.items():
        ahora = ult.get(fam, 0.0)
        if antes >= CAIDA_MIN_IMPORTE and ahora <= antes * (1 - CAIDA_MIN_PCT / 100):
            resultado.append({'familia': fam, 'ultimos_12_meses': round(ahora, 2), 'anteriores_12_meses': round(antes, 2),
                              'variacion_pct': round((ahora - antes) / antes * 100)})
    return sorted(resultado, key=lambda r: r['ultimos_12_meses'] - r['anteriores_12_meses'])


# ---------------------------------------------------------------- venta cruzada
def equivalente(l):
    """Artículos "equivalentes" (sustitutos entre sí): misma subfamilia o, si no la hay,
    misma familia y mismas dos primeras palabras de la descripción."""
    if l.get('subfamilia'):
        return (l['familia'], l['subfamilia'])
    return (l['familia'], ' '.join(l['descripcion'].split()[:2]))


def _datos_cruzada():
    """Estructuras comunes (en caché): familias y referencias por cliente, reglas entre familias."""
    ventas, version = pirineos.todas_las_ventas()
    maquinas = pirineos.maquinas()
    clave = (version, len(maquinas), hoy().isoformat())
    with _lock:
        if _cache.get('cruzada', (None,))[0] == clave:
            return _cache['cruzada'][1]
    corte = (hoy() - timedelta(days=730)).isoformat()
    familias_cli, refs_cli, subfam_cli, nombre_ref, info_ref = {}, {}, {}, {}, {}
    grupo_familia = defaultdict(Counter)
    equiv_ref = {}
    for cod, lineas in ventas.items():
        if pirineos.principal(cod) != cod:  # un cliente con varios códigos cuenta una vez
            continue
        utiles = [l for l in lineas_utiles(lineas) if l['fecha'] >= corte and not es_puntual(l)]
        if not utiles:
            continue
        familias_cli[cod] = {l['familia'] for l in utiles if l['familia']}
        refs_cli[cod] = Counter(l['referencia'] for l in utiles)
        subfam_cli[cod] = {equivalente(l) for l in utiles}
        for l in utiles:
            nombre_ref[l['referencia']] = l['descripcion']
            info_ref[l['referencia']] = (l['grupo'], l['familia'])
            equiv_ref[l['referencia']] = equivalente(l)
            grupo_familia[l['familia']][l['grupo']] += 1
    n = len(familias_cli) or 1
    soporte = Counter(f for fams in familias_cli.values() for f in fams)
    pares = Counter()
    for fams in familias_cli.values():
        fams = sorted(fams)
        for i, a in enumerate(fams):
            for b in fams[i + 1:]:
                pares[(a, b)] += 1
                pares[(b, a)] += 1
    def depende_de_maquina(familia):
        grupo = grupo_familia[familia].most_common(1)[0][0] if grupo_familia[familia] else ''
        generica = familia.upper() in ('', 'VARIOS', 'OTROS', '01', '00') or len(familia) <= 2
        return generica or grupo in GRUPOS_SOLO_POR_MAQUINA or any(p in familia.upper() for p in PALABRAS_SOLO_POR_MAQUINA)

    reglas = {}
    for (a, b), juntos in pares.items():
        if juntos < SUG_MIN_SOPORTE_FAMILIA or depende_de_maquina(a) or depende_de_maquina(b):
            continue
        confianza = juntos / soporte[a]
        lift = confianza / (soporte[b] / n)
        if confianza >= SUG_MIN_CONFIANZA and lift >= SUG_MIN_LIFT:
            reglas.setdefault(a, []).append({'familia': b, 'confianza': confianza, 'lift': lift, 'juntos': juntos})
    # Máquinas activas por modelo, y marcas de máquina de cada cliente
    por_modelo, marcas_cli, marcas = defaultdict(set), defaultdict(set), set()
    for m in maquinas:
        k = pirineos.clave_modelo(m)
        if m['codigo'] and not m['baja'] and len(k) >= 3:
            por_modelo[k].add(m['codigo'])
        for w in pirineos.clave_nombre(m['marca']).split():
            if len(w) >= 3 and w.isalpha():
                marcas.add(w)
                if m['codigo'] and not m['baja']:
                    marcas_cli[m['codigo']].add(w)
    datos = {'marcas': marcas, 'marcas_cli': marcas_cli, 'familias': familias_cli, 'refs': refs_cli, 'subfam': subfam_cli, 'reglas': reglas, 'equiv_ref': equiv_ref,
             'nombre_ref': nombre_ref, 'info_ref': info_ref, 'por_modelo': por_modelo}
    with _lock:
        _cache['cruzada'] = (clave, datos)
    return datos


def sugerencias(codigo):
    codigo = str(codigo)
    D = _datos_cruzada()
    articulos = pirineos.articulos()
    # Lo que ya compra la empresa con cualquiera de sus códigos de Pirineos.
    hermanos = set(pirineos.codigos_hermanos(codigo))
    ya_refs, ya_subfam, ya_familias = Counter(), set(), set()
    for h in hermanos:
        ya_refs.update(D['refs'].get(h, Counter()))
        ya_subfam |= D['subfam'].get(h, set())
        ya_familias |= D['familias'].get(h, set())
    resultado, vistas = [], set()

    def disponible(referencia):
        a = articulos.get(referencia)
        return a and (any(a['tarifas'].values()) or a['stock'] > 0)

    # a) por máquina
    for m in pirineos.maquinas_cliente(codigo):
        if m['baja']:
            continue
        k = pirineos.clave_modelo(m)
        otros = D['por_modelo'].get(k, set()) - hermanos
        otros = {c for c in otros if c in D['refs']}
        if len(otros) < SUG_MIN_CLIENTES_MAQUINA:
            continue
        cuenta = Counter(r for c in otros for r in D['refs'][c])
        por_linea = {}  # variantes de una misma línea (p. ej. los 4 colores de una tinta) -> una sugerencia
        for referencia, num in cuenta.most_common(60):
            if num < SUG_MIN_CLIENTES_MAQUINA or num / len(otros) < SUG_MIN_PROPORCION_MAQUINA:
                break
            equiv = D['equiv_ref'].get(referencia)
            if referencia in ya_refs or referencia in vistas or equiv in ya_subfam or not disponible(referencia):
                continue
            if equiv in por_linea:
                por_linea[equiv]['variantes'] += 1
                continue
            vistas.add(referencia)
            por_linea[equiv] = {
                'tipo': 'maquina', 'referencia': referencia, 'descripcion': D['nombre_ref'].get(referencia, ''),
                'familia': D['info_ref'].get(referencia, ('', ''))[1], 'variantes': 0,
                'motivo': f"{num} de {len(otros)} clientes con {m['modelo'] or m['maquina']} lo compran y este cliente no.",
                'peso': num / len(otros) + 1,
            }
        resultado.extend(por_linea.values())
    # b) por familias que suelen ir juntas
    modelos_cliente = {k for k, cods in D['por_modelo'].items() if cods & hermanos}
    modelos_de = defaultdict(set)
    for k, cods in D['por_modelo'].items():
        for c in cods:
            modelos_de[c].add(k)

    palabras_modelo = {w for k in D['por_modelo'] for w in k.split() if len(w) >= 4 and w.isalpha()} | D['marcas']

    def ligado_a_otra_maquina(referencia):
        """True si el artículo lo compran sobre todo clientes con un modelo de máquina que este no tiene
        (p. ej. cartuchos de una impresora concreta): entonces no tiene sentido sugerirlo por familia."""
        desc = set(pirineos.clave_nombre(D['nombre_ref'].get(referencia, '')).split())
        # Si nombra una marca o modelo de máquina (p. ej. "PACKET", "ROLAND VS") es un consumible
        # para una máquina concreta: eso ya lo cubren las sugerencias por máquina.
        if desc & palabras_modelo:
            return True
        compradores = [c for c, refs in D['refs'].items() if referencia in refs]
        if len(compradores) < 3:
            return False
        cuenta_modelos = Counter(k for c in compradores for k in modelos_de.get(c, ()))
        return any(n / len(compradores) >= 0.5 and k not in modelos_cliente for k, n in cuenta_modelos.items())

    mejores = {}
    for a in ya_familias:
        for r in D['reglas'].get(a, []):
            b = r['familia']
            if b in ya_familias:
                continue
            if b not in mejores or r['confianza'] * r['lift'] > mejores[b]['confianza'] * mejores[b]['lift']:
                mejores[b] = {**r, 'desde': a}
    for b, r in sorted(mejores.items(), key=lambda x: -x[1]['confianza'] * x[1]['lift'])[:4]:
        # Artículo representativo de la familia B entre los clientes que compran A y B.
        clientes_ab = [c for c, fams in D['familias'].items() if r['desde'] in fams and b in fams and c not in hermanos]
        cuenta = Counter()
        for c in clientes_ab:
            for referencia in D['refs'][c]:
                if D['info_ref'].get(referencia, ('', ''))[1] == b:
                    cuenta[referencia] += 1
        referencia = next((x for x, _ in cuenta.most_common(15)
                           if disponible(x) and x not in vistas and not ligado_a_otra_maquina(x)), None)
        if not referencia:
            continue
        vistas.add(referencia)
        resultado.append({
            'tipo': 'familia', 'referencia': referencia, 'descripcion': D['nombre_ref'].get(referencia, ''), 'familia': b,
            'motivo': (f"El {round(r['confianza'] * 100)} % de los clientes que compran {r['desde']} también compran {b} "
                       f"({r['juntos']} clientes); este cliente nunca ha comprado {b}."),
            'peso': r['confianza'],
        })
    resultado.sort(key=lambda s: -s['peso'])
    for s in resultado:
        s.pop('peso', None)
        if s.get('variantes'):
            s['motivo'] += f" (y {s['variantes']} artículo(s) más de la misma línea)"
    return resultado[:MAX_SUGERENCIAS]


# ---------------------------------------------------------------- máquinas: avisos comerciales
def avisos_maquinas(lista, ref=None):
    ref = ref or hoy()
    avisos = []
    for m in lista:
        if m['baja']:
            continue
        nombre = ' '.join(x for x in (m['marca'], m['modelo'] or m['maquina']) if x) or m['equipo']
        texto_maquina = ' '.join((m['maquina'], m['equipo'], m['modelo'])).upper()
        g = _d(m['garantia']) if m['garantia'] else None
        if g and ref <= g <= ref + timedelta(days=90):
            avisos.append({'tipo': 'garantia', 'texto': f"La garantía de {nombre} vence el {g.strftime('%d/%m/%Y')}: ofrecer mantenimiento."})
        i = _d(m['instalacion']) if m['instalacion'] else None
        if i and (ref - i).days >= ANIOS_RENOVACION * 365 and not any(p in texto_maquina for p in NO_RENOVAR):
            avisos.append({'tipo': 'antigua', 'texto': f"{nombre} lleva {(ref - i).days // 365} años instalada: candidata a renovación."})
    return avisos


# ---------------------------------------------------------------- recuperados (para el resumen)
def recuperados(desde, hasta):
    """Clientes que han vuelto a comprar entre desde y hasta después de un parón largo."""
    ventas, _ = pirineos.todas_las_ventas()
    resultado = []
    for cod, lineas in ventas.items():
        if pirineos.principal(cod) != cod:
            continue
        fechas = sorted({_d(l['fecha']) for l in lineas_utiles(lineas) if _d(l['fecha'])})
        en_rango = [f for f in fechas if desde <= f.isoformat() <= hasta]
        if not en_rango:
            continue
        anteriores = [f for f in fechas if f < en_rango[0]]
        if not anteriores:
            continue
        hueco = (en_rango[0] - anteriores[-1]).days
        intervalo = _intervalo_mediano(anteriores) or 30
        if hueco > max(3 * intervalo, DORMIDO_MIN_DIAS):
            resultado.append({'codigo': cod, 'fecha': en_rango[0].isoformat(), 'dias_parado': hueco})
    return resultado
