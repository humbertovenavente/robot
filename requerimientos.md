# Requerimiento del proyecto

**Proyecto:** Sistema automático de clasificación de paquetes con LEGO MINDSTORMS y YOLO
**Curso:** Robótica / Visión Artificial / Automatización
**Modalidad:** Proyecto por equipos, coordinado en una práctica simultánea de laboratorio

## 1. Descripción general

Se requiere desarrollar un sistema automatizado, inspirado en el video de referencia, capaz de identificar un paquete mediante visión artificial, interpretar su código o clasificación, determinar el destino correcto y accionar un robot LEGO MINDSTORMS para trasladarlo o desviarlo hacia el "agujero" o contenedor correspondiente. Luego de depositarlo, el robot deberá regresar automáticamente a su posición inicial para atender el siguiente paquete.

El sistema deberá operar de manera orquestada, de forma que varios equipos o estaciones puedan trabajar al mismo tiempo dentro del aula o laboratorio, bajo una lógica central de coordinación y monitoreo.

## 2. Objetivo general

Diseñar e implementar un sistema mecatrónico-robótico con visión artificial que clasifique paquetes de manera autónoma, precisa, repetible y sincronizada entre múltiples estaciones de trabajo.

## 3. Objetivos específicos

- Detectar cada paquete mediante cámara.
- Leer su código, etiqueta o clase asignada.
- Asociar cada código con un destino de descarga.
- Mover el paquete al agujero o contenedor correcto.
- Regresar el robot a posición de espera.
- Registrar cada clasificación realizada.
- Permitir que varias estaciones funcionen de manera simultánea bajo una misma orquestación.

## 4. Alcance del proyecto

El proyecto incluye:

- Un prototipo físico basado en LEGO MINDSTORMS.
- Un módulo de visión artificial con YOLO para detección/clasificación.
- Un sistema lógico de decisión para asignar destino.
- Un mecanismo de transporte o desvío del paquete.
- Un módulo de retorno automático a estado inicial.
- Un sistema coordinador para múltiples robots/equipos.
- Una interfaz básica de supervisión de estado.

No incluye, en esta fase:

- Integración industrial real con PLC.
- Lectura de códigos industriales de alta velocidad.
- Carga pesada o paquetes fuera del rango físico del prototipo LEGO.
- Operación 24/7.

## 5. Planteamiento del problema

En sistemas logísticos modernos, la clasificación de paquetes exige rapidez, exactitud, trazabilidad y coordinación entre múltiples unidades. El reto académico consiste en reproducir este concepto en un entorno didáctico usando LEGO MINDSTORMS como plataforma robótica y YOLO como motor de visión, logrando que el sistema:

- reconozca el paquete,
- decida el destino,
- ejecute el movimiento correcto,
- y sincronice múltiples estaciones al mismo tiempo.

## 6. Arquitectura propuesta

El sistema deberá dividirse en 4 capas:

### Capa 1. Captura
Cámara fija o cámara por estación para observar el área de entrada del paquete.

### Capa 2. Inteligencia artificial
Modelo YOLO entrenado para detectar el paquete y/o clasificar la etiqueta, color, símbolo o código visual.

### Capa 3. Control lógico
Módulo software que traduzca la detección en una orden concreta:
- "paquete tipo A → agujero 1",
- "paquete tipo B → agujero 2", etc.

### Capa 4. Ejecución robótica
Robot LEGO MINDSTORMS que:
- tome o empuje el paquete,
- lo lleve al destino correcto,
- lo libere,
- y regrese a home.

## 7. Requerimientos funcionales

- **RF-01.** El sistema deberá detectar la presencia de un paquete en la zona de ingreso.
- **RF-02.** El sistema deberá identificar el código visual del paquete.
- **RF-03.** El sistema deberá relacionar el código detectado con un destino predefinido.
- **RF-04.** El robot deberá ejecutar el movimiento hacia el agujero correcto.
- **RF-05.** El robot deberá depositar o dejar caer el paquete en el destino asignado.
- **RF-06.** El robot deberá regresar automáticamente a su posición inicial.
- **RF-07.** El sistema deberá impedir un nuevo ciclo mientras el robot esté ocupado.
- **RF-08.** El sistema deberá registrar cada evento: detección, clasificación, destino y tiempo de ciclo.
- **RF-09.** El sistema deberá mostrar el estado de cada estación: libre, procesando, error, completado.
- **RF-10.** El sistema deberá permitir operación simultánea de varias estaciones.
- **RF-11.** El coordinador deberá distribuir y monitorear el trabajo de todas las estaciones activas.
- **RF-12.** El sistema deberá manejar errores básicos: paquete no reconocido, paquete mal posicionado, destino bloqueado, fallo de retorno.

## 8. Requerimientos no funcionales

- **RNF-01.** El sistema deberá ser seguro para uso en aula.
- **RNF-02.** La estructura deberá ser modular y fácil de desmontar.
- **RNF-03.** La precisión mínima deseada de clasificación deberá definirse y medirse experimentalmente.
- **RNF-04.** El tiempo de ciclo por paquete deberá ser consistente.
- **RNF-05.** El software deberá estar documentado.
- **RNF-06.** El sistema deberá permitir calibración rápida antes de cada práctica.
- **RNF-07.** La solución deberá poder ampliarse a más estaciones sin rediseño completo.
- **RNF-08.** La interfaz deberá ser comprensible para estudiantes y docente.

## 9. Componentes mínimos sugeridos

A nivel académico, cada estación debería incluir:

- 1 robot LEGO MINDSTORMS
- 1 cámara
- 1 área de carga de paquetes
- 1 mecanismo de clasificación
- 3 a 5 agujeros o destinos
- 1 computadora o nodo de procesamiento
- 1 módulo de comunicación con el coordinador central

## 10. Lógica de operación esperada

1. El paquete entra a la estación.
2. La cámara captura la imagen.
3. YOLO detecta/clasifica el paquete.
4. El sistema consulta la tabla de destinos.
5. El coordinador valida que la estación esté libre.
6. El robot ejecuta la rutina de traslado.
7. El paquete cae en el agujero correcto.
8. El robot regresa a home.
9. El evento se registra.
10. La estación queda lista para el siguiente paquete.

## 11. Orquestación simultánea

Como se pidió que todos trabajen al mismo tiempo, el proyecto debe contemplar una capa de coordinación central.

### Funciones del orquestador

- Registrar estaciones activas.
- Saber qué estación está libre o ocupada.
- Recibir resultados de visión por cada estación.
- Autorizar la ejecución del movimiento.
- Llevar conteo global de paquetes procesados.
- Mostrar incidencias en tiempo real.
- Evitar conflictos de arranque o doble procesamiento.

### Resultado esperado

Varios equipos pueden demostrar sus estaciones de forma concurrente, mientras el docente observa en una sola interfaz:

- estación 1: procesando,
- estación 2: libre,
- estación 3: error de lectura,
- estación 4: ciclo completado.

## 12. Propuesta de organización por equipos

- **Equipo 1:** Diseño mecánico del robot y sistema de caída.
- **Equipo 2:** Visión artificial con YOLO.
- **Equipo 3:** Integración software y reglas de decisión.
- **Equipo 4:** Orquestación, comunicación y dashboard.
- **Equipo 5:** Pruebas, métricas, documentación y validación.

Si la clase es pequeña, también puede hacerse por estaciones completas, donde cada equipo construye una estación y todas se integran al final.

## 13. Entregables del proyecto

- Documento de requerimientos.
- Diagrama general de arquitectura.
- Diseño mecánico del prototipo.
- Dataset de entrenamiento.
- Modelo YOLO entrenado.
- Código de control del robot.
- Módulo de orquestación.
- Dashboard o interfaz de monitoreo.
- Video de demostración.
- Informe final con métricas y conclusiones.

## 14. Criterios de aceptación

El proyecto se considerará aceptado si:

- identifica correctamente los tipos de paquete definidos,
- deposita cada paquete en el agujero correcto,
- regresa a home después de cada ciclo,
- opera sin intervención manual en condiciones normales,
- registra los eventos de ejecución,
- y demuestra operación simultánea de varias estaciones.

## 15. Métricas sugeridas para evaluación

- Exactitud de clasificación.
- Tiempo promedio de ciclo.
- Tasa de error.
- Número de paquetes procesados por minuto.
- Tiempo de retorno a home.
- Disponibilidad de la estación.
- Porcentaje de ciclos completados sin intervención.

## 16. Riesgos del proyecto

- Iluminación deficiente para visión.
- Dataset insuficiente.
- Mala alineación entre cámara y zona de captura.
- Deslizamiento mecánico del robot.
- Desincronización entre visión y movimiento.
- Latencia de comunicación entre estaciones.
- Sobrecarga del coordinador central.

## 17. Tecnologías sugeridas

YOLO es adecuado para detección sobre imagen y video, y su documentación oficial también contempla seguimiento en tiempo real y uso con datasets personalizados. LEGO EV3 dispone de brick programable, motores y sensores, además de recursos educativos oficiales de clasificación por color y soporte para Python, lo que lo vuelve una base didáctica razonable para este tipo de prototipo.

## 18. Redacción breve del requerimiento principal

El sistema deberá clasificar paquetes automáticamente mediante visión artificial y robótica educativa, identificando el código visual de cada paquete, asignando su destino, depositándolo en el agujero correspondiente y regresando el robot a su posición inicial, permitiendo además la operación coordinada y simultánea de múltiples estaciones.
