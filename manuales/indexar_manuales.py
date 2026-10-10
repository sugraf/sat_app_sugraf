# manuales/indexar_manuales.py
"""Genera los índices del asistente para consultar los manuales (uno por máquina).

Se ejecuta en un ordenador con Google Drive para escritorio:

    python manuales/indexar_manuales.py                      # lo nuevo o cambiado
    python manuales/indexar_manuales.py --solo "Roland/VG3-640"
    python manuales/indexar_manuales.py --forzar             # rehace todos

Lee los PDF de "H:\\Mi unidad\\Manuales tecnicos" y deja los índices en su
subcarpeta "_indice_manuales", que Drive sincroniza y la app lee. Solo procesa
los PDF nuevos o modificados; conviene ejecutarlo después de subir manuales.
Con PyMuPDF instalado (pip install pymupdf) es mucho más rápido; si no, usa pypdf.
"""
import argparse
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)
from manuales.rag_manuales import (CARPETA_INDICES, INDICE_VERSION, escribir_indice, indexar_pdf,
                                   leer_indice, nombre_indice)

RAIZ_POR_DEFECTO = r"H:\Mi unidad\Manuales tecnicos"
FICHEROS_IGNORADOS = {'desktop.ini', 'thumbs.db', '.ds_store'}


def _es_pdf(nombre):
    n = nombre.lower()
    return n.endswith('.pdf') and n not in FICHEROS_IGNORADOS and not n.startswith(('~$', '.'))


def carpetas_modelo(raiz):
    """(marca, modelo, [(ruta dentro de la carpeta, ruta completa)]) como las ve la app:
    Marca/Modelo con sus subcarpetas, y los PDF sueltos de una marca como "General"."""
    for marca in sorted(os.listdir(raiz)):
        dir_marca = os.path.join(raiz, marca)
        if not os.path.isdir(dir_marca) or marca.startswith('_'):
            continue
        sueltos = [(f, os.path.join(dir_marca, f)) for f in sorted(os.listdir(dir_marca))
                   if os.path.isfile(os.path.join(dir_marca, f)) and _es_pdf(f)]
        if sueltos:
            yield marca.strip(), 'General', sueltos
        for modelo in sorted(os.listdir(dir_marca)):
            dir_modelo = os.path.join(dir_marca, modelo)
            if not os.path.isdir(dir_modelo):
                continue
            pdfs = []
            for base, subcarpetas, ficheros in os.walk(dir_modelo):
                subcarpetas.sort()
                for f in sorted(ficheros):
                    if _es_pdf(f):
                        completa = os.path.join(base, f)
                        pdfs.append((os.path.relpath(completa, dir_modelo).replace(os.sep, '/'), completa))
            yield marca.strip(), modelo.strip(), pdfs


def _leer_existente(fichero):
    try:
        with open(fichero, 'rb') as f:
            return leer_indice(f.read())
    except (OSError, ValueError):
        return None


def _guardar(fichero, datos):
    temporal = fichero + '.tmp'
    with open(temporal, 'wb') as f:
        f.write(escribir_indice(datos))
    os.replace(temporal, fichero)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--raiz', default=RAIZ_POR_DEFECTO, help='Carpeta "Manuales tecnicos" sincronizada')
    parser.add_argument('--solo', help='Solo una máquina, como "Marca/Modelo"')
    parser.add_argument('--forzar', action='store_true', help='Rehace los índices aunque estén al día')
    parser.add_argument('--procesos', type=int, default=max(1, min(6, (os.cpu_count() or 2) - 1)))
    args = parser.parse_args()

    if not os.path.isdir(args.raiz):
        sys.exit(f'No se encuentra la carpeta {args.raiz}')
    destino = os.path.join(args.raiz, CARPETA_INDICES)
    os.makedirs(destino, exist_ok=True)

    # Qué hay que hacer en cada máquina.
    trabajos = []
    for marca, modelo, pdfs in carpetas_modelo(args.raiz):
        if args.solo and f'{marca}/{modelo}'.casefold() != args.solo.strip('/').casefold():
            continue
        fichero = os.path.join(destino, nombre_indice(marca, modelo))
        actuales = [(ruta, completa, os.path.getsize(completa)) for ruta, completa in pdfs]
        existente = None if args.forzar else _leer_existente(fichero)
        hechos = {(m['ruta'], m['tamano']) for m in (existente or {}).get('manuales', [])}
        faltan = [a for a in actuales if (a[0], a[2]) not in hechos]
        sobran = hechos - {(a[0], a[2]) for a in actuales}
        if not actuales and not os.path.exists(fichero):
            continue
        if faltan or sobran or existente is None:
            trabajos.append({'marca': marca, 'modelo': modelo, 'fichero': fichero, 'actuales': actuales,
                             'existente': existente, 'faltan': faltan, 'nuevos': {}})

    total = sum(len(t['faltan']) for t in trabajos)
    print(f'{len(trabajos)} máquina(s) por actualizar, {total} PDF por leer, con {args.procesos} proceso(s).')
    if not trabajos:
        return

    def terminar(trabajo):
        """Monta el índice de la máquina con lo que ya estaba y lo nuevo, y lo guarda."""
        viejo = trabajo['existente'] or {'manuales': [], 'trozos': []}
        por_clave = {(m['ruta'], m['tamano']): i for i, m in enumerate(viejo['manuales'])}
        manuales, trozos = [], []
        for ruta, _, tamano in trabajo['actuales']:
            numero = len(manuales)
            if (ruta, tamano) in trabajo['nuevos']:
                paginas, estado, nuevos = trabajo['nuevos'][(ruta, tamano)]
                trozos += [[numero, p, t] for p, t in nuevos]
            else:
                anterior = por_clave[(ruta, tamano)]
                m = viejo['manuales'][anterior]
                paginas, estado = m['paginas'], m['estado']
                trozos += [[numero, p, t] for n, p, t in viejo['trozos'] if n == anterior]
            manuales.append({'ruta': ruta, 'tamano': tamano, 'paginas': paginas, 'estado': estado})
        _guardar(trabajo['fichero'], {
            'version': INDICE_VERSION, 'marca': trabajo['marca'], 'modelo': trabajo['modelo'],
            'generado': datetime.now().isoformat(timespec='seconds'), 'manuales': manuales, 'trozos': trozos,
        })
        print(f"   -> índice guardado: {trabajo['marca']}/{trabajo['modelo']} ({len(manuales)} manuales, {len(trozos)} fragmentos)")

    inicio, hechos, problemas = time.time(), 0, []
    for t in trabajos:
        if not t['faltan']:
            terminar(t)  # solo se han quitado manuales
    with ProcessPoolExecutor(max_workers=args.procesos) as pool:
        tareas = {pool.submit(indexar_pdf, completa): (t, ruta, tamano)
                  for t in trabajos for ruta, completa, tamano in t['faltan']}
        for tarea in as_completed(tareas):
            t, ruta, tamano = tareas[tarea]
            try:
                resultado = tarea.result()
            except Exception as e:
                resultado = (0, f'error: {e}', [])
            t['nuevos'][(ruta, tamano)] = resultado
            hechos += 1
            paginas, estado, trozos = resultado
            if estado != 'ok':
                problemas.append(f"{t['marca']}/{t['modelo']}/{ruta}: {estado}")
            print(f"[{hechos}/{total}] {t['marca']}/{t['modelo']}/{ruta}: {estado}, {paginas} pág., "
                  f"{len(trozos)} fragmentos ({round(time.time() - inicio)} s)")
            if len(t['nuevos']) == len(t['faltan']):
                terminar(t)

    print(f'\nHecho en {round(time.time() - inicio)} s.')
    if problemas:
        print('PDF que el asistente no podrá consultar (escaneados sin texto o dañados):')
        for p in problemas:
            print(f'  - {p}')


if __name__ == '__main__':
    main()
