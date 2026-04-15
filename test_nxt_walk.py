"""Script de prueba: verifica conexión NXT y camina con puertos A y C."""
import time
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

POWER  = 85    # 0-100  (subido para vencer inercia)
WALK_S = 3.0   # segundos caminando


def _set_motor(brick, port, power):
    """Comando directo de bajo nivel — funciona en todas las versiones de nxt-python."""
    import nxt.motor as m
    # RunState.RUNNING = 0x20, Mode.ON = 0x01, RegulationMode.IDLE = 0x00
    try:
        # nxt-python v3 API
        brick.set_output_state(
            port,
            power,
            m.Mode.ON,
            m.RegulationMode.IDLE,
            0,
            m.RunState.RUNNING,
            0,
        )
    except AttributeError:
        # nxt-python v2 fallback
        brick.set_output_state(port, power, 0x01, 0x00, 0, 0x20, 0)


def _stop_motor(brick, port):
    """Frena el motor (coast)."""
    import nxt.motor as m
    try:
        brick.set_output_state(port, 0, 0x00, m.RegulationMode.IDLE, 0, m.RunState.IDLE, 0)
    except AttributeError:
        brick.set_output_state(port, 0, 0x00, 0x00, 0, 0x00, 0)


def main():
    # ── 1. Importar ───────────────────────────────────────────────────────────
    try:
        import nxt.locator
        import nxt.motor
    except ImportError:
        log.error("nxt-python no instalado.  pip install nxt-python")
        return

    # ── 2. Conectar ───────────────────────────────────────────────────────────
    log.info("Buscando brick NXT (USB)...")
    try:
        brick = nxt.locator.find()
    except Exception as exc:
        log.error("NXT no encontrado: %s", exc)
        return

    try:
        name, host, *_ = brick.get_device_info()
        log.info("Brick conectado: '%s'  (%s)", name, host)
    except Exception as exc:
        log.warning("No se pudo leer info: %s", exc)

    # ── 3. Puertos ────────────────────────────────────────────────────────────
    port_a = nxt.motor.Port.A
    port_c = nxt.motor.Port.C

    # ── 4. ADELANTE ───────────────────────────────────────────────────────────
    log.info("Avanzando  A=%d  C=%d  durante %.1f s...", POWER, POWER, WALK_S)
    _set_motor(brick, port_a, POWER)
    _set_motor(brick, port_c, POWER)
    time.sleep(WALK_S)

    # ── 5. STOP ───────────────────────────────────────────────────────────────
    log.info("Deteniendo...")
    _stop_motor(brick, port_a)
    _stop_motor(brick, port_c)
    log.info("Listo.")


if __name__ == "__main__":
    main()
