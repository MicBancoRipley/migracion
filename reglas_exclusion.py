"""
=============================================================================
REGLAS DE EXCLUSIÓN (qué triggers NO se migran, por política del equipo)
=============================================================================

Regla pedida por el equipo:
  Excluir de la migración automática los triggers de:
    - BI      -> segmento "bigdata-bi-" en el nombre (equipo BI)
    - MATINAL -> la palabra "matinal" en cualquier parte

El chequeo se hace tanto en el NOMBRE del trigger como en el NOMBRE del job,
para asegurarnos (un trigger cuyo nombre no dice "matinal" pero que dispara un
job matinal también queda excluido).

⚠️ Nota importante sobre "bi":
    NO se puede excluir por las letras "bi" sueltas, porque "sdlf-bigdata"
    contiene "bi" -> excluiría TODOS los triggers. Por eso el criterio de BI es
    el segmento "bigdata-bi-" (el equipo BI), no cualquier "bi".
    Así 'billeteras', 'bigdata' genérico, etc. NO se excluyen por error.

Uso:
    from reglas_exclusion import debe_excluirse, motivo_exclusion
    if debe_excluirse(nombre_trigger, nombre_job):
        ...  # no migrar
=============================================================================
"""

# Marcador del equipo BI dentro del nombre (seguro, no atrapa 'billeteras' ni 'bigdata')
MARCADOR_BI = 'bigdata-bi-'
# Palabra que marca procesos matinal (literal, en cualquier parte)
MARCADOR_MATINAL = 'matinal'
# Límite de longitud del nombre de un EventBridge Scheduler schedule.
# Si el schedule resultante excede esto, create_schedule falla con
# ValidationException -> hay que apartarlo para renombrar a mano.
MAX_LEN_SCHEDULE = 64


# Regla de acortado (definida por el equipo) para nombres que exceden 64 chars:
# quitar el prefijo 'sdlf-bigdata-' del inicio y el segmento '-glue' del final.
PREFIJO_ACORTAR = 'sdlf-bigdata-'


def _base_schedule(trigger_name):
    """Nombre 'normal' del schedule: -trigger -> -schedule."""
    if trigger_name.endswith('-trigger'):
        return trigger_name[:-len('-trigger')] + '-schedule'
    return trigger_name + '-schedule'


def _acortar_nombre(nombre_schedule):
    """Aplica la regla del equipo: quita prefijo 'sdlf-bigdata-' y el '-glue'
    que antecede a '-schedule'. Solo se usa cuando el nombre supera 64 chars."""
    n = nombre_schedule
    if n.startswith(PREFIJO_ACORTAR):
        n = n[len(PREFIJO_ACORTAR):]
    if n.endswith('-glue-schedule'):
        n = n[:-len('-glue-schedule')] + '-schedule'
    return n


def nombre_schedule_desde_trigger(trigger_name):
    """Nombre del schedule a partir del trigger.

    Regla base: -trigger -> -schedule.
    Si el nombre resultante supera MAX_LEN_SCHEDULE (64), se aplica la regla de
    acortado acordada con el equipo (quitar 'sdlf-bigdata-' y '-glue'), que deja
    todos los casos conocidos por debajo de 64.
    """
    base = _base_schedule(trigger_name)
    if len(base) <= MAX_LEN_SCHEDULE:
        return base
    return _acortar_nombre(base)


def _norm(texto):
    return (texto or '').strip().lower()


def motivo_exclusion(nombre_trigger, nombre_job=''):
    """Devuelve el motivo de exclusión (str) o None si NO debe excluirse.

    Revisa tanto el nombre del trigger como el del job.
    """
    t = _norm(nombre_trigger)
    j = _norm(nombre_job)

    # BI: segmento 'bigdata-bi-' en el nombre del trigger o del job
    if MARCADOR_BI in t or MARCADOR_BI in j:
        return 'excluido: equipo BI (bigdata-bi-)'

    # MATINAL: palabra 'matinal' en el nombre del trigger o del job
    if MARCADOR_MATINAL in t or MARCADOR_MATINAL in j:
        return 'excluido: proceso matinal'

    # LONGITUD: nombre_schedule_desde_trigger ya aplica la regla de acortado
    # del equipo (quita 'sdlf-bigdata-' y '-glue') cuando supera 64. Solo se
    # excluye si NI SIQUIERA acortado cabe -> caso raro que requiere mano.
    nombre_sched = nombre_schedule_desde_trigger(nombre_trigger or '')
    if len(nombre_sched) > MAX_LEN_SCHEDULE:
        return f'nombre schedule >{MAX_LEN_SCHEDULE} chars aun acortado ({len(nombre_sched)}): renombrar a mano'

    return None


def debe_excluirse(nombre_trigger, nombre_job=''):
    """True si el trigger cae en la regla de exclusión (BI o matinal)."""
    return motivo_exclusion(nombre_trigger, nombre_job) is not None
