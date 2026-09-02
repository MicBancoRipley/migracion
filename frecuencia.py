"""
=============================================================================
CLASIFICACIÓN DE FRECUENCIA (para ordenar las tandas de migración)
=============================================================================

El plan del jefe: migrar primero los de MAYOR frecuencia, luego los DIARIOS,
y al final los APAGADOS. Este módulo deriva la "categoría de frecuencia" del
cron de cada trigger para poder ordenarlos.

Categorías (de mayor a menor frecuencia):
    'alta'        -> corre varias veces al día (cada X min, cada hora, varias horas)
    'diaria'      -> corre 1 vez al día (minuto y hora fijos, todos los días)
    'infrecuente' -> semanal / mensual (día de semana o del mes específico)
    'apagado'     -> el trigger está DEACTIVATED (no importa el cron)
    'desconocida' -> no se pudo interpretar el cron

Formato de cron de EventBridge/Glue (6 campos):
    cron(minuto hora día-del-mes mes día-de-semana año)
    Ej: cron(0/15 * * * ? *)   -> cada 15 min           -> alta
        cron(00 13 * * ? *)    -> todos los días 13:00  -> diaria
        cron(0 13 1 * ? *)     -> día 1 de cada mes     -> infrecuente
        cron(00 8 ? * MON *)   -> los lunes             -> infrecuente

Uso:
    from frecuencia import clasificar_frecuencia
    cat = clasificar_frecuencia("cron(0/15 * * * ? *)", estado_glue="ACTIVATED")
    # -> 'alta'
"""

import re


# Orden de prioridad para migrar (menor número = se migra primero)
ORDEN = {
    'alta': 0,
    'diaria': 1,
    'infrecuente': 2,
    'apagado': 3,
    'desconocida': 4,
}


def _campos_cron(expr):
    """Devuelve los 6 campos del cron, o None si no parece un cron válido.

    cron(minuto hora dia-mes mes dia-semana anio)
    """
    if not expr:
        return None
    m = re.match(r'^\s*cron\((.+)\)\s*$', expr.strip(), re.IGNORECASE)
    if not m:
        return None
    campos = m.group(1).split()
    if len(campos) != 6:
        return None
    return campos  # [minuto, hora, dia_mes, mes, dia_sem, anio]


def _corre_varias_veces_al_dia(minuto, hora):
    """True si el minuto u hora implican múltiples ejecuciones diarias.

    Señales: '/', ',', '-' o '*' en minuto/hora significan repetición intradía.
    """
    for campo in (minuto, hora):
        if campo == '*':
            return True
        if '/' in campo or ',' in campo or '-' in campo:
            return True
    return False


def _dia_especifico(dia_mes, dia_sem):
    """True si el trigger corre solo ciertos días (semanal/mensual), no a diario.

    En cron de EventBridge uno de los dos días suele ser '?'. Si el otro no es
    '*' (ni '?'), entonces es un día específico -> infrecuente.
    """
    for campo in (dia_mes, dia_sem):
        if campo in ('*', '?'):
            continue
        # cualquier otra cosa (números, rangos, listas, nombres de día) = específico
        return True
    return False


def clasificar_frecuencia(cron_expr, estado_glue=None):
    """Devuelve la categoría de frecuencia.

    Si estado_glue == 'DEACTIVATED' -> 'apagado' (tiene prioridad, va al final).
    """
    if (estado_glue or '').upper() == 'DEACTIVATED':
        return 'apagado'

    campos = _campos_cron(cron_expr)
    if not campos:
        return 'desconocida'

    minuto, hora, dia_mes, mes, dia_sem, _anio = campos

    # 1. ¿Corre solo ciertos días (semanal/mensual)? -> infrecuente
    #    (aunque dentro de ese día corra varias veces, sigue siendo infrecuente
    #     en el sentido de "no todos los días").
    if _dia_especifico(dia_mes, dia_sem) or (mes not in ('*', '?')):
        return 'infrecuente'

    # 2. Corre todos los días. ¿Varias veces al día? -> alta ; si no -> diaria
    if _corre_varias_veces_al_dia(minuto, hora):
        return 'alta'
    return 'diaria'


def orden_migracion(cron_expr, estado_glue=None):
    """Número de orden para migrar (0 = primero). Útil para sort()."""
    return ORDEN.get(clasificar_frecuencia(cron_expr, estado_glue), 9)
