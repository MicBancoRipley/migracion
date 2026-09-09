"""
=============================================================================
REGLAS DE MIGRACION (decisiones que se aplican AL CREAR cada schedule)
=============================================================================

Por que existe este modulo (LECCION APRENDIDA):
  En la primera migracion creamos los schedules "tal cual" (mismo cron UTC,
  sin grupo) y DESPUES tuvimos que reparar todo a mano:
    - retimezone masivo   (UTC -> America/Santiago)
    - corregir 486 crons desfasados por el cambio de timezone
    - mover 521 schedules del grupo 'default' a su grupo real
  Fue tedioso y arriesgado. La solucion de fondo es DECIDIR estas 3 cosas
  ANTES de migrar y aplicarlas al momento de CREAR, para que cada schedule
  nazca correcto y no haya nada que reparar.

Este modulo centraliza esas reglas para que las usen migrar_seguro.py y
migrar_lote.py (y quien sea) SIN duplicar logica:

  1. TIMEZONE  -> America/Santiago (ajusta verano/invierno solo)
  2. OFFSET    -> resta el desfase horario al cron (verano=3, invierno=4)
                  con la politica A1 (ver convertir_cron_para_crear)
  3. GRUPO     -> grupo real de destino segun el job (regla de Bastian)

Todo se configura por variables de entorno (las escribe la opcion
"Decisiones previas" del menu en el .env):

    TIMEZONE_MIGRACION   (default America/Santiago)
    OFFSET_CRON          (entero; 3=verano Chile, 4=invierno; 0 = no tocar cron)
    GRUPO_JOB_PRINCIPAL  (default datamanagement_stored_procedures)
    GRUPO_RESTO          (default sdlf_bigdata_glue_jobs)
    JOB_PRINCIPAL        (default sdlf-bigdata-redshift-segmentation-schedule-glue-job)

=============================================================================
"""
import os
import re


# ---------------------------------------------------------------------------
# TIMEZONE
# ---------------------------------------------------------------------------
def timezone_objetivo():
    """Timezone con el que nacen los schedules. America/Santiago ajusta el
    horario de verano/invierno automaticamente (evita el desfase que tuvimos)."""
    return os.environ.get('TIMEZONE_MIGRACION', 'America/Santiago').strip() or 'America/Santiago'


# ---------------------------------------------------------------------------
# OFFSET DE CRON
# ---------------------------------------------------------------------------
def offset_configurado():
    """Horas a restar al cron al crear. 3=verano Chile (UTC-3),
    4=invierno (UTC-4), 0 = no tocar el cron (crear tal cual)."""
    try:
        return int(os.environ.get('OFFSET_CRON', '0').strip() or '0')
    except ValueError:
        return 0


def _parse_cron(expr):
    """Devuelve los 6 campos de un cron(...) de EventBridge, o None."""
    m = re.match(r'^cron\((.+)\)$', (expr or '').strip())
    if not m:
        return None
    campos = m.group(1).split()
    return campos if len(campos) == 6 else None


def convertir_cron_para_crear(expr, offset):
    """Aplica la POLITICA A1 al cron ANTES de crear el schedule.

    Devuelve (cron_nuevo, revisar, motivo):
      - cron_nuevo : el cron ya convertido (o el original si no se toca)
      - revisar    : True si el caso necesita criterio humano (no se auto-convierte)
      - motivo     : explicacion corta (para la nota del control / log)

    POLITICA A1 (segura):
      * offset == 0                      -> no tocar (cron_nuevo=expr, revisar=False)
      * cron no parseable / no 6 campos  -> no tocar, revisar=False (rate/at, etc.)
      * hora SIMPLE (ej. '12'):
          - diario (dia_mes y dia_sem son * o ?):
              underflow (cruza medianoche) -> se convierte igual (wrap inofensivo,
              corre todos los dias)         -> revisar=False
          - dia ESPECIFICO (viernes, dia 12, MON-FRI):
              si NO hay underflow           -> se convierte, revisar=False
              si HAY underflow (cambia dia) -> NO se convierte, revisar=True
                (requiere OK de negocio; el wrap moveria el dia)
      * hora LISTA (ej. '1,14,19')        -> resta offset a cada hora (con wrap) -> revisar=False
      * hora RANGO/STEP (ej. '12-0','*/3','*') -> ALTA FRECUENCIA: no se toca, revisar=False
        (el timezone no las afecta: corren igual sin importar la hora)
    """
    if offset == 0:
        return expr, False, 'offset=0 (no se toca)'

    campos = _parse_cron(expr)
    if not campos:
        return expr, False, 'no-cron (rate/at u otro): no se toca'

    hora = campos[1]
    dia_mes, dia_sem = campos[2], campos[4]
    corre_todos_los_dias = dia_mes in ('*', '?') and dia_sem in ('*', '?')

    # --- hora SIMPLE ---
    if re.match(r'^\d{1,2}$', hora):
        nueva = int(hora) - offset
        if nueva < 0:
            if corre_todos_los_dias:
                nueva += 24  # wrap inofensivo: corre igual cada dia
            else:
                # dia especifico + underflow -> el wrap cambiaria el dia
                return expr, True, f'REVISAR: dia-especifico con wrap (H={hora}-{offset}) requiere OK negocio'
        if nueva > 23:
            return expr, True, f'REVISAR: overflow (H={nueva})'
        campos[1] = str(nueva)
        return f"cron({' '.join(campos)})", False, f'hora simple {hora}->{campos[1]}'

    # --- hora LISTA (1,14,19) ---
    if re.match(r'^\d{1,2}(,\d{1,2})+$', hora):
        horas = [int(h) for h in hora.split(',')]
        nuevas = [(h - offset) % 24 for h in horas]  # wrap por hora (alta frec. relativa)
        campos[1] = ','.join(str(h) for h in sorted(set(nuevas)))
        return f"cron({' '.join(campos)})", False, f'lista horas {hora}->{campos[1]}'

    # --- hora RANGO / STEP / '*' -> ALTA FRECUENCIA ---
    # (12-0, */3, *, etc.) el timezone no las afecta: no se tocan.
    return expr, False, f'alta frecuencia (hora={hora}): no se toca'


# ---------------------------------------------------------------------------
# GRUPO DESTINO
# ---------------------------------------------------------------------------
def grupo_del_job_principal():
    return os.environ.get('GRUPO_JOB_PRINCIPAL', 'datamanagement_stored_procedures').strip() \
        or 'datamanagement_stored_procedures'


def grupo_resto():
    return os.environ.get('GRUPO_RESTO', 'sdlf_bigdata_glue_jobs').strip() \
        or 'sdlf_bigdata_glue_jobs'


def nombre_job_principal():
    return os.environ.get('JOB_PRINCIPAL',
                          'sdlf-bigdata-redshift-segmentation-schedule-glue-job').strip() \
        or 'sdlf-bigdata-redshift-segmentation-schedule-glue-job'


def grupo_destino(job_name):
    """Regla de Bastian: SOLO el job principal exacto va al grupo de stored
    procedures; cualquier otro job va al grupo general de glue jobs.

    Si no hay grupos configurados (todo vacio) se puede devolver 'default'
    dejando GRUPO_JOB_PRINCIPAL/GRUPO_RESTO en blanco -> ver grupo_configurado().
    """
    principal = nombre_job_principal()
    if (job_name or '').strip() == principal:
        return grupo_del_job_principal()
    return grupo_resto()


def grupos_configurados():
    """True si el usuario definio grupos reales (no vacios). Si es False, la
    migracion crea en 'default' (comportamiento antiguo) y avisa."""
    return bool(os.environ.get('GRUPO_JOB_PRINCIPAL', '').strip()
                or os.environ.get('GRUPO_RESTO', '').strip())


# ---------------------------------------------------------------------------
# ESTADO / RESUMEN de las decisiones previas (para el menu)
# ---------------------------------------------------------------------------
def resumen_decisiones():
    """Texto legible del estado actual de las decisiones previas."""
    off = offset_configurado()
    off_txt = {0: '0 (NO convierte cron)', 3: '3 (verano Chile, UTC-3)',
               4: '4 (invierno Chile, UTC-4)'}.get(off, str(off))
    lineas = [
        f"  Timezone objetivo : {timezone_objetivo()}",
        f"  Offset de cron    : {off_txt}",
        f"  Job principal     : {nombre_job_principal()}",
        f"  Grupo principal   : {grupo_del_job_principal()}",
        f"  Grupo resto       : {grupo_resto()}",
    ]
    return "\n".join(lineas)


def decisiones_completas():
    """True si hay lo minimo para que la migracion aplique las 3 reglas bien:
    un offset != 0 y grupos configurados. (El timezone siempre tiene default)."""
    return offset_configurado() != 0 and grupos_configurados()
