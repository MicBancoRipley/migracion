# Por qué NO se migraron: BI, Matinal y Workflows

Documento de justificación para presentar. Explica **por qué** ciertos triggers
quedaron fuera de la migración a EventBridge Scheduler, con fundamento técnico
y de negocio. No fue una omisión: fue una decisión consciente en cada caso.

---

## Resumen

| Grupo | Cantidad | Motivo (una línea) |
|-------|----------|--------------------|
| **Workflows (CONDITIONAL)** | 31 | No se disparan por horario, sino por dependencia entre jobs. EventBridge Scheduler solo hace cron. |
| **Matinal** | 20 | Proceso crítico encadenado; decisión de negocio de mantenerlo en Glue por ahora. |
| **BI** (`bigdata-bi-`) | 14 | Pertenece a otro equipo (BI); fuera del alcance de esta migración. |

---

## 1. Workflows / triggers CONDITIONAL — razón TÉCNICA

**Qué son:** un trigger `CONDITIONAL` se dispara **cuando otro job termina**
(ej. "cuando el job A termine con éxito, ejecuta el job B"). Son los eslabones de
un **Glue Workflow** (una cadena de pasos: step0 → step1 → step2 ...).

**Por qué NO se pueden migrar a EventBridge Scheduler:**
EventBridge Scheduler **solo dispara por horario (cron)**. No sabe "esperar a que
otro job termine". Si migráramos un paso intermedio de un workflow a un schedule
por horario:

- Se rompería la cadena de dependencias (el paso correría a una hora fija en vez
  de cuando su paso previo termina).
- Podría ejecutarse con datos incompletos (antes de que el paso anterior termine).

**Caso especial — los "step0" (inicio de workflow):** aunque el step0 SÍ es
SCHEDULED (arranca por cron), **no se migra** porque es la **cabeza** de un
workflow. Migrar solo el step0 dejaría la cabeza en EventBridge y el resto de la
cadena en Glue → dependencia rota. El workflow completo debe quedar junto en Glue
(o rehacerse con Step Functions, que es otro proyecto).

**Conclusión:** los workflows requieren un servicio de orquestación con
dependencias (AWS Step Functions / Glue Workflows), no un scheduler por horario.
Migrarlos habría roto procesos productivos.

---

## 2. Matinal — razón de NEGOCIO + técnica

**Qué es:** el "proceso matinal" es un conjunto de jobs encadenados que preparan
datos temprano en el día (vistas, calidad de datos, enriquecimiento). Varios de
sus triggers son además CONDITIONAL (mismo caso que los workflows).

**Por qué se excluyó (decisión del equipo):**
- Es un **proceso crítico y encadenado**: un error en la migración afectaría la
  disponibilidad de datos de toda la mañana.
- Buena parte de sus triggers son CONDITIONAL (dependen unos de otros) → mismo
  impedimento técnico que los workflows.
- El equipo decidió **mantenerlo estable en Glue por ahora** y tratarlo como un
  proyecto aparte si se migra en el futuro.

**Regla aplicada:** se excluye cualquier trigger que contenga la palabra
`matinal` en su nombre **o** en el nombre de su job (para atrapar también los que
disparan un job matinal sin decirlo en su propio nombre).

---

## 3. BI (`bigdata-bi-`) — razón de PROPIEDAD / alcance

**Qué es:** triggers del **equipo de Business Intelligence**, identificables por
el segmento `bigdata-bi-` en el nombre del trigger o del job (ej.
`sdlf-bigdata-bi-redshift-segmentation-schedule-glue-job`).

**Por qué se excluyó:**
- **No son de nuestro equipo.** Pertenecen a BI, que gestiona sus propios
  procesos y ventanas de ejecución.
- Migrarlos sin coordinación con BI podría afectar sus reportes.
- Quedan **fuera del alcance** de esta migración; si BI quiere migrarlos, se hace
  con su equipo y su visto bueno.

**Detalle de la regla (importante):** se excluye por el **segmento
`bigdata-bi-`**, NO por las letras "bi" sueltas. Esto es clave porque el prefijo
común de TODOS los triggers es `sdlf-bigdata`, que contiene "bi". Filtrar por "bi"
suelto habría excluido los 600+ triggers por error. Por eso el marcador es el
segmento exacto `bigdata-bi-`, que solo matchea los del equipo BI (y no
`billeteras`, `bigdata` genérico, etc.).

---

## Cómo está implementada la exclusión (para auditar)

En `reglas_exclusion.py`:

```python
MARCADOR_BI = 'bigdata-bi-'      # segmento del equipo BI (no "bi" suelto)
MARCADOR_MATINAL = 'matinal'     # palabra en cualquier parte
```

La función `motivo_exclusion(nombre_trigger, nombre_job)` revisa **ambos** (el
nombre del trigger y el del job) para BI y matinal. Los CONDITIONAL se detectan
por el campo `Type` del trigger (no es SCHEDULED → no migrable a un schedule).

---

## Mensaje para la presentación (resumen)

> No migramos BI, matinal ni los workflows por razones distintas pero todas
> justificadas:
> - **Workflows/CONDITIONAL:** técnicamente no se pueden — se disparan por
>   dependencia entre jobs, no por horario; EventBridge Scheduler solo hace cron.
> - **Matinal:** proceso crítico encadenado; decisión de mantenerlo estable en
>   Glue (además muchos son CONDITIONAL).
> - **BI:** es de otro equipo; fuera de nuestro alcance, requiere su coordinación.
>
> En los tres casos, migrarlos habría roto procesos o invadido territorio de otro
> equipo. Se dejaron documentados para que quien corresponda los retome si aplica.
