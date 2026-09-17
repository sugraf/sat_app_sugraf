# aviso_matcher.py
"""
Localiza la fila real de un aviso dentro de un DataFrame (Avisos Sin Tratar /
Avisos Tratados) aunque las filas se hayan desplazado entre el momento en que
el frontend cargó la lista y el momento en que envía una acción (otro usuario
puede haber creado, movido o borrado avisos mientras tanto).

En vez de fiarse a ciegas del "aviso_index" (la posición de la fila cuando se
cargó la lista), se comprueba que esa fila siga correspondiendo al mismo
aviso comparando CLIENTE + MÁQUINA + DESCRIPCIÓN + F. ENTR. Si no coincide,
se busca la fila que sí coincide con esos datos.
"""


def _norm(valor):
    return str(valor if valor is not None else '').strip().casefold()


def _coincide(row, cliente, maquina, descripcion, fecha_entr):
    return (
        _norm(row.get('CLIENTE')) == _norm(cliente)
        and _norm(row.get('MÁQUINA')) == _norm(maquina)
        and _norm(row.get('DESCRIPCIÓN')) == _norm(descripcion)
        and _norm(row.get('F. ENTR.')) == _norm(fecha_entr)
    )


def resolver_indice_aviso(df, aviso_index, cliente='', maquina='', descripcion='', fecha_entr=''):
    """Devuelve el índice real de la fila del aviso, o None si no se encuentra.

    Si no se aportan datos identificativos (llamadas antiguas que todavía no
    envíen estos campos), se mantiene el comportamiento anterior: se usa
    aviso_index tal cual si está dentro de rango.
    """
    aviso_index = int(aviso_index)
    tiene_criterios = any(_norm(v) for v in (cliente, maquina, descripcion, fecha_entr))

    if not tiene_criterios:
        return aviso_index if 0 <= aviso_index < len(df) else None

    if 0 <= aviso_index < len(df) and _coincide(df.iloc[aviso_index], cliente, maquina, descripcion, fecha_entr):
        return aviso_index

    for idx in df.index:
        if _coincide(df.loc[idx], cliente, maquina, descripcion, fecha_entr):
            return idx

    return None


def existe_aviso_duplicado(dfs, cliente, maquina, descripcion, fecha_entr, excluir=None):
    """Comprueba si ya existe otra fila con el mismo CLIENTE + MÁQUINA +
    DESCRIPCIÓN + F. ENTR. en alguno de los DataFrames dados.

    Se usa para avisar antes de crear o renombrar un aviso a una descripción
    que ya identifica a otro (lo que impediría distinguirlos por contenido).

    dfs: lista de DataFrames donde buscar (p. ej. Avisos Sin Tratar y Avisos Tratados).
    excluir: tupla opcional (df, idx) de la propia fila que se está editando,
    para no compararla consigo misma.
    """
    if not all(_norm(v) for v in (cliente, maquina, descripcion, fecha_entr)):
        return False

    for df in dfs:
        for idx in df.index:
            if excluir is not None and excluir[0] is df and excluir[1] == idx:
                continue
            if _coincide(df.loc[idx], cliente, maquina, descripcion, fecha_entr):
                return True

    return False
