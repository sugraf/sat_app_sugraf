# Control_IA/rag.py
"""Recuperación de partes del histórico para el asistente IA (RAG).

En vez de mandar el histórico entero al modelo, se indexan anomalía y solución
con BM25 y en cada pregunta solo viajan los partes más parecidos, priorizando
la misma unidad, el mismo modelo y el mismo cliente. Usa el índice que carga
historicos/api_historicos.py (cargar_historico).
"""
import math
import re

from historicos.api_historicos import (
    _alfanum, _sin_acentos, _texto, clave_cliente, clave_modelo, clave_serie,
)

PALABRAS_VACIAS = {
    'DE', 'LA', 'EL', 'LOS', 'LAS', 'UN', 'UNA', 'UNOS', 'UNAS', 'Y', 'O', 'U', 'E', 'A', 'EN', 'CON', 'SIN',
    'POR', 'PARA', 'DEL', 'AL', 'SE', 'SU', 'SUS', 'LE', 'LES', 'LO', 'ME', 'TE', 'NOS', 'MI', 'TU', 'QUE',
    'ES', 'SON', 'HA', 'HAN', 'HAY', 'NO', 'SI', 'YA', 'MAS', 'MUY', 'PERO', 'COMO', 'CUAL', 'CUALES',
    'ESTE', 'ESTA', 'ESTO', 'ESE', 'ESA', 'ESO', 'DONDE', 'CUANDO', 'QUIEN', 'TIENE', 'TIENEN', 'TENIDO',
    'HACE', 'HECHO', 'SIDO', 'ESTAN', 'ALGO', 'ALGUN', 'ALGUNA', 'OTRO', 'OTRA', 'OTROS', 'TODO',
    'TODOS', 'TODAS', 'SOBRE', 'ENTRE', 'HASTA', 'DESDE', 'PUEDE', 'PUEDES', 'DIME', 'DECIR', 'SABER',
    'QUIERO', 'NECESITO', 'BUSCA', 'BUSCAR', 'HISTORICO', 'HISTORICOS', 'PARTE', 'PARTES', 'CLIENTE',
    'MAQUINA', 'SIMILAR', 'SIMILARES', 'PARECIDO', 'PARECIDA', 'ANTES', 'VEZ', 'VECES', 'FALLO', 'FALLA',
    # Vocabulario de la propia aplicación (preguntas de uso, no de averías).
    'AVISO', 'AVISOS', 'TECNICO', 'TECNICOS', 'ASIGNAR', 'ASIGNO', 'ASIGNADO', 'ASIGNADOS', 'URGENTE',
    'URGENTES', 'MODULO', 'APLICACION', 'APP', 'PANTALLA', 'CREAR', 'CREO', 'USAR', 'USO', 'CUANTOS',
    'CUANTAS', 'CLIENTES', 'MAQUINAS', 'REPARTO', 'PROPON', 'RESUME', 'RESUMEN', 'HISTORIAL', 'ULTIMA',
    'ULTIMO', 'REPETIDAS', 'AVERIAS', 'AVERIA',
}
# Palabras que aparecen en muchos nombres de cliente y no sirven para identificarlo.
PALABRAS_CLIENTE_GENERICAS = {
    'GRAFICAS', 'GRAFICA', 'ARTES', 'IMPRENTA', 'DIGITAL', 'INDUSTRIAS', 'INDUSTRIAL', 'INDUSTRIALES',
    'SERVICIOS', 'COMERCIAL', 'EDITORIAL', 'DISTRIBUCIONES', 'ROTULOS', 'ROTULACION', 'IMPRESION',
    'IMPRESIONES', 'SUMINISTROS', 'PUBLICIDAD', 'COMUNICACION', 'DISENO', 'FUNDACION', 'GRUPO',
    'SOLUCIONES', 'TALLERES', 'ZARAGOZA', 'HUESCA', 'TERUEL', 'ARAGON', 'SUGRAF', 'TECNICO', 'TECNICA',
    'SERVICIO', 'MAQUINARIA', 'REPUESTOS', 'CENTRO', 'TALLER', 'NUEVO', 'NUEVA', 'AVISO', 'EMPRESA',
}


def _raiz(palabra):
    """Quita el plural para que 'CABEZALES' y 'CABEZAL' coincidan."""
    if len(palabra) > 5 and palabra.endswith('ES'):
        return palabra[:-2]
    if len(palabra) > 4 and palabra.endswith('S'):
        return palabra[:-1]
    return palabra


def tokens_busqueda(texto):
    return [_raiz(p) for p in re.findall(r'[A-Z0-9]+', _sin_acentos(texto))
            if len(p) > 1 and p not in PALABRAS_VACIAS]


class _IndiceRAG:
    K1, B = 1.4, 0.75

    def __init__(self, indice):
        registros = indice.registros
        # --- Índice invertido BM25 sobre anomalía + solución + equipo/modelo ---
        self.postings = {}
        self.longitudes = []
        for i, r in enumerate(registros):
            conteo = {}
            for t in tokens_busqueda(f"{r['anomalia']} {r['solucion']} {r['equipo']} {r['modelo']}"):
                conteo[t] = conteo.get(t, 0) + 1
            self.longitudes.append(sum(conteo.values()) or 1)
            for t, n in conteo.items():
                self.postings.setdefault(t, []).append((i, n))
        self.n = len(registros)
        self.media = sum(self.longitudes) / max(self.n, 1)

        # --- Diccionarios para detectar cliente / modelo / serie citados en una pregunta ---
        self.clientes_por_palabra = {}
        for r in registros:
            for nombre in (r['cliente'], r['nombre_comercial']):
                for p in clave_cliente(nombre).split():
                    if len(p) >= 4 and p not in PALABRAS_CLIENTE_GENERICAS and p not in PALABRAS_VACIAS:
                        self.clientes_por_palabra.setdefault(p, set()).add(r['cliente'])
        self.modelo_original = {}
        for r, k in zip(registros, indice.claves):
            if k['modelo'] in indice.modelos:
                self.modelo_original.setdefault(k['modelo'], r['modelo'])
        self.series = {k['serie']: r['n_serie'] for r, k in zip(registros, indice.claves) if len(k['serie']) >= 5}

    def puntuar(self, tokens):
        puntos = {}
        for t in set(tokens):
            post = self.postings.get(t)
            if not post:
                continue
            idf = math.log(1 + (self.n - len(post) + 0.5) / (len(post) + 0.5))
            for i, tf in post:
                norm = tf + self.K1 * (1 - self.B + self.B * self.longitudes[i] / self.media)
                puntos[i] = puntos.get(i, 0.0) + idf * tf * (self.K1 + 1) / norm
        return puntos


def _rag(indice):
    if getattr(indice, '_rag', None) is None:
        indice._rag = _IndiceRAG(indice)
    return indice._rag


def detectar_entidades(indice, texto):
    """Cliente, modelo y nº de serie mencionados en una pregunta libre."""
    rag = _rag(indice)
    palabras = set(re.findall(r'[A-Z0-9]+', _sin_acentos(texto)))
    candidatos = {}
    for p in palabras:
        clientes = rag.clientes_por_palabra.get(p, ())
        if 0 < len(clientes) <= 3:
            for c in clientes:
                candidatos[c] = candidatos.get(c, 0) + 1
    cliente = max(candidatos, key=lambda c: (candidatos[c], -len(c))) if candidatos else ''

    texto_alf = _alfanum(texto)
    modelos = [m for m in rag.modelo_original
               if m in texto_alf and (len(m) >= 6 or (len(m) >= 4 and re.search(r'\d', m) and re.search(r'[A-Z]', m)))]
    series = [s for s in rag.series if s in texto_alf]
    return {
        'cliente': cliente,
        'modelo': rag.modelo_original[max(modelos, key=len)] if modelos else '',
        'n_serie': rag.series[max(series, key=len)] if series else '',
    }


def similares(indice, consulta, cliente='', modelo='', n_serie='', desde='', hasta='', k=8, recientes=4):
    """Partes del histórico más relevantes para 'consulta' dentro del ámbito dado.

    Devuelve {'similares': [...], 'recientes': [...]} donde 'recientes' son los
    últimos partes del ámbito (unidad > modelo > cliente) aunque no coincidan en texto.
    """
    rag = _rag(indice)
    registros, claves = indice.registros, indice.claves
    claves_cli = {clave_cliente(p) for p in [cliente, *_texto(cliente).split('/')]} - {''}
    serie = clave_serie(n_serie)
    mod = clave_modelo(modelo)
    if mod in indice.modelos_genericos:
        mod = ''

    def en_fechas(i):
        f = registros[i]['fecha']
        return (not desde or f >= desde) and (not hasta or f <= hasta)

    def coincidencias(i):
        k_ = claves[i]
        motivos = []
        if serie and k_['serie'] == serie:
            motivos.append('misma unidad')
        elif mod and k_['modelo'] == mod:
            motivos.append('mismo modelo')
        if claves_cli and k_['clientes'] & claves_cli:
            motivos.append('mismo cliente')
        return motivos

    factores = {'misma unidad': 3.0, 'mismo modelo': 2.5, 'mismo cliente': 1.3}
    MIN_PUNTOS = 2.5  # por debajo, la coincidencia es solo de palabras muy comunes
    puntos = rag.puntuar(tokens_busqueda(consulta))
    ranking = []
    for i, p in puntos.items():
        if p < MIN_PUNTOS or not en_fechas(i):
            continue
        for m in coincidencias(i):
            p *= factores[m]
        ranking.append((p, i))
    ranking.sort(reverse=True)

    def mejores(lista, n):
        if not lista:
            return []
        corte = lista[0][0] * 0.25  # descarta coincidencias residuales
        return [i for p, i in lista if p >= corte][:n]

    # Hasta la mitad de los resultados se reservan para el ámbito de la pantalla
    # (unidad / modelo / cliente): si no, partes de otras marcas con más palabras
    # en común taparían a los de la propia máquina.
    elegidos = mejores([(p, i) for p, i in ranking if coincidencias(i)], k // 2)
    resto = mejores(ranking, k)
    elegidos += [i for i in resto if i not in elegidos][:k - len(elegidos)]
    puntos_finales = {i: p for p, i in ranking}
    elegidos.sort(key=lambda i: (not coincidencias(i), -puntos_finales[i]))

    # Últimos partes del ámbito más concreto disponible.
    # Se usa el primero que tenga partes: muchos partes no tienen modelo o serie rellenos.
    criterios = []
    if serie:
        criterios.append(lambda k_: k_['serie'] == serie)
    if mod and claves_cli:
        criterios.append(lambda k_: k_['modelo'] == mod and bool(k_['clientes'] & claves_cli))
    if claves_cli:
        criterios.append(lambda k_: bool(k_['clientes'] & claves_cli))
    if mod:
        criterios.append(lambda k_: k_['modelo'] == mod)
    ambito = []
    for criterio in criterios:
        ambito = [i for i, k_ in enumerate(claves) if criterio(k_)]
        if ambito:
            break
    ambito = sorted((i for i in ambito if en_fechas(i) and i not in elegidos),
                    key=lambda i: registros[i]['fecha'], reverse=True)[:recientes]

    def con_motivo(i):
        return {**registros[i], 'coincide': ', '.join(coincidencias(i))}

    return {
        'similares': [con_motivo(i) for i in elegidos],
        'recientes': [con_motivo(i) for i in ambito],
    }


def resumen_ambito(indice, cliente='', modelo='', n_serie=''):
    """Una o dos líneas con el tamaño del histórico del ámbito (cliente / modelo / unidad)."""
    registros, claves = indice.registros, indice.claves
    lineas = []
    claves_cli = {clave_cliente(p) for p in [cliente, *_texto(cliente).split('/')]} - {''}
    if claves_cli:
        pos = [i for i, k in enumerate(claves) if k['clientes'] & claves_cli]
        if pos:
            fechas = sorted(registros[i]['fecha'] for i in pos)
            maquinas = {}
            for i in pos:
                m = registros[i]['modelo'] or registros[i]['equipo'] or 'sin modelo'
                maquinas[m] = maquinas.get(m, 0) + 1
            top = ', '.join(f'{m} ({n})' for m, n in sorted(maquinas.items(), key=lambda x: -x[1])[:10])
            lineas.append(f"Cliente {registros[pos[0]]['cliente']}: {len(pos)} partes entre {fechas[0]} y {fechas[-1]}. "
                          f"Máquinas con más partes: {top}.")
        else:
            lineas.append(f"Cliente {cliente}: sin partes en el histórico.")
    serie = clave_serie(n_serie)
    if serie:
        pos = [i for i, k in enumerate(claves) if k['serie'] == serie]
        lineas.append(f"Unidad S/N {n_serie}: {len(pos)} partes en el histórico.")
    mod = clave_modelo(modelo)
    if mod:
        pos = [i for i, k in enumerate(claves) if k['modelo'] == mod]
        n_cli = len({registros[i]['cliente'] for i in pos})
        lineas.append(f"Modelo {modelo}: {len(pos)} partes en {n_cli} clientes.")
    return '\n'.join(lineas)


def texto_registro(r, max_txt=300):
    """Formato compacto de un parte para el contexto del modelo."""
    def corto(t):
        t = ' '.join(_texto(t).split())
        return t if len(t) <= max_txt else t[:max_txt] + '…'
    maquina = ' '.join(x for x in (r['equipo'], r['marca'], r['modelo']) if x)
    if r['n_serie']:
        maquina += f" S/N {r['n_serie']}"
    extra = f" [{r['coincide']}]" if r.get('coincide') else ''
    return (f"- Parte {r['n_parte']} · {r['fecha']} · {r['cliente']} · {maquina or 'sin máquina'} · "
            f"{r['tecnico']}{extra}\n  Anomalía: {corto(r['anomalia'])}\n  Solución: {corto(r['solucion'])}")
