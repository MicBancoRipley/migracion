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


def nombre_schedule_desde_trigger(trigger_name):
    """Mismo criterio que el resto del proyecto: -trigger -> -schedule."""
    if trigger_name.endswith('-trigger'):
        return trigger_name[:-len('-trigger')] + '-schedule'
    return trigger_name + '-schedule'


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

    # LONGITUD: el nombre del schedule no puede pasar de 64 chars (EventBridge).
    # Si excede, create_schedule falla -> apartar para renombrar a mano.
    nombre_sched = nombre_schedule_desde_trigger(nombre_trigger or '')
    if len(nombre_sched) > MAX_LEN_SCHEDULE:
        return f'nombre schedule >{MAX_LEN_SCHEDULE} chars ({len(nombre_sched)}): renombrar a mano'

    return None


def debe_excluirse(nombre_trigger, nombre_job=''):
    """True si el trigger cae en la regla de exclusión (BI o matinal)."""
    return motivo_exclusion(nombre_trigger, nombre_job) is not None
