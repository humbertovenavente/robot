"""LEGO NXT differential-drive implementation for QRNavigator.

Requires:  pip install nxt-python
Hardware:  NXT brick connected via USB (or Bluetooth if configured).

Motor layout (default, change in config if yours differ):
  Port A = left wheel
  Port C = right wheel

If one wheel spins backwards flip nav_invert_left / nav_invert_right in config.
"""
from __future__ import annotations
import logging
import threading
import time
from navigator import DriveInterface, DEFAULT_DRIVE_SPEED

log = logging.getLogger(__name__)

# Minimum ms between USB motor commands — NXT disconnects if flooded
_CMD_THROTTLE_MS = 180
# How often to send keep_alive to prevent NXT auto-sleep (seconds)
_KEEPALIVE_INTERVAL_S = 8

# Claw (Motor B) — flip signs if your claw moves the wrong way
_CLAW_PORT        = "B"
_CLAW_OPEN_POWER  =  70   # positive = open
_CLAW_CLOSE_POWER = -70   # negative = close
_CLAW_DURATION_S  =  0.6  # seconds to run motor before braking


class NXTDrive(DriveInterface):
    """Sends move/steer/stop commands to LEGO NXT motors via nxt-python v3."""

    def __init__(
        self,
        left_port: str = "A",
        right_port: str = "C",
        invert_left: bool = False,
        invert_right: bool = False,
    ):
        self._left_port_name  = left_port
        self._right_port_name = right_port
        self._invert_left     = invert_left
        self._invert_right    = invert_right
        self._brick       = None
        self._lock        = threading.Lock()   # one USB transfer at a time
        self._last_cmd_t  = 0.0               # timestamp of last command sent
        self._last_pl     = None              # last left power sent
        self._last_pr     = None              # last right power sent
        self._ka_thread: threading.Thread | None = None
        self._ka_stop     = threading.Event()
        self._connect()

    # ── connection ─────────────────────────────────────────────────────────────
    def _connect(self) -> bool:
        """Try to connect to NXT brick. Returns True on success, False on failure."""
        try:
            import nxt.locator
            log.info("NXTDrive: buscando brick NXT (USB)...")
            self._brick = nxt.locator.find()
            log.info(
                "NXTDrive: conectado  left=%s  right=%s",
                self._left_port_name, self._right_port_name,
            )
            self._start_keepalive()
            return True
        except Exception as exc:
            log.warning("NXTDrive: NXT no disponible (%s) — reintentando en comandos", exc)
            self._brick = None
            return False

    def _ensure_connected(self) -> bool:
        """Reconnect if brick is None. Returns True if connected."""
        if self._brick is not None:
            return True
        return self._connect()

    # ── keep-alive thread — prevents NXT auto-sleep ───────────────────────────
    def _start_keepalive(self) -> None:
        if self._ka_thread and self._ka_thread.is_alive():
            return
        self._ka_stop.clear()
        self._ka_thread = threading.Thread(
            target=self._keepalive_loop, daemon=True, name="nxt-keepalive"
        )
        self._ka_thread.start()
        log.info("NXTDrive: keepalive thread iniciado (cada %ds)", _KEEPALIVE_INTERVAL_S)

    def _keepalive_loop(self) -> None:
        while not self._ka_stop.wait(_KEEPALIVE_INTERVAL_S):
            if self._brick is None:
                continue
            with self._lock:
                try:
                    self._brick.keep_alive()
                    log.debug("NXTDrive: keep_alive enviado")
                except Exception as exc:
                    log.warning("NXTDrive: keep_alive falló (%s)", exc)
                    self._brick = None   # forzar reconexión en el próximo comando

    # ── internal ────────────────────────────────────────────────────────────────
    def _p(self, speed_0_100: int, invert: bool) -> int:
        """Map 0-100 speed to NXT power range -100..100."""
        power = max(-100, min(100, int(speed_0_100)))
        return -power if invert else power

    def _set_output(self, port_name: str, power: int, brake: bool = False) -> None:
        """Low-level set_output_state — compatible con nxt-python v3."""
        import nxt.motor as m
        port = m.Port[port_name]
        if brake:
            # Freno activo: mode=ON|BRAKE, run_state=RUNNING, power=0
            mode      = m.Mode.ON | m.Mode.BRAKE
            run_state = m.RunState.RUNNING
        else:
            # Coast (rueda libre): mode=ON, run_state=RUNNING, power=X
            mode      = m.Mode.ON
            run_state = m.RunState.RUNNING
        try:
            self._brick.set_output_state(
                port, power, mode, m.RegulationMode.IDLE, 0, run_state, 0
            )
        except (AttributeError, TypeError):
            # nxt-python v2 fallback
            mode_raw = 0x03 if brake else 0x01
            self._brick.set_output_state(port, power, mode_raw, 0x00, 0, 0x20, 0)

    # ── DriveInterface ──────────────────────────────────────────────────────────
    def _send(self, pl: int, pr: int, label: str) -> None:
        """Send L/R power to motors with throttle + lock to avoid NXT USB overflow."""
        now = time.monotonic() * 1000
        if pl == self._last_pl and pr == self._last_pr:
            if now - self._last_cmd_t < _CMD_THROTTLE_MS:
                return
        if now - self._last_cmd_t < _CMD_THROTTLE_MS / 2:
            return
        with self._lock:
            try:
                self._set_output(self._left_port_name,  pl)
                self._set_output(self._right_port_name, pr)
                self._last_pl    = pl
                self._last_pr    = pr
                self._last_cmd_t = time.monotonic() * 1000
                log.info("NXTDrive: %s  L=%d  R=%d", label, pl, pr)
            except Exception as exc:
                log.error("NXTDrive %s: %s", label, exc)
                self._brick = None   # forzar reconexión

    def drive(self, left: int, right: int) -> None:
        """Set left and right wheel speeds independently (-100..100)."""
        if not self._ensure_connected():
            log.warning("NXTDrive: brick no conectado")
            return
        self._send(
            self._p(left,  self._invert_left),
            self._p(right, self._invert_right),
            f"drive L={left} R={right}",
        )

    def stop_motors(self) -> None:
        if not self._ensure_connected():
            return
        now = time.monotonic() * 1000
        if now - self._last_cmd_t < _CMD_THROTTLE_MS / 2:
            return
        with self._lock:
            try:
                self._set_output(self._left_port_name,  0, brake=True)
                self._set_output(self._right_port_name, 0, brake=True)
                self._last_pl    = 0
                self._last_pr    = 0
                self._last_cmd_t = time.monotonic() * 1000
                log.info("NXTDrive: frenado")
            except Exception as exc:
                log.error("NXTDrive stop: %s", exc)
                self._brick = None


    # ── claw (Motor B) ──────────────────────────────────────────────────────────

    def open_claw(self) -> None:
        """Run Motor B to open claw. Flip _CLAW_OPEN_POWER sign if direction is wrong."""
        self._run_claw(_CLAW_OPEN_POWER, "open")

    def close_claw(self) -> None:
        self._run_claw(_CLAW_CLOSE_POWER, "close")

    def _run_claw(self, power: int, label: str) -> None:
        if not self._ensure_connected():
            log.warning("NXTDrive: brick not connected — skipping claw %s", label)
            return
        with self._lock:
            try:
                self._set_output(_CLAW_PORT, power)
            except Exception as exc:
                log.error("NXTDrive claw %s start: %s", label, exc)
                return
        time.sleep(_CLAW_DURATION_S)
        with self._lock:
            try:
                self._set_output(_CLAW_PORT, 0, brake=True)
                log.info("NXTDrive: claw %s done", label)
            except Exception as exc:
                log.error("NXTDrive claw %s stop: %s", label, exc)

    def disconnect(self) -> None:
        """Cerrar conexión USB limpiamente para permitir reconexión posterior."""
        self._ka_stop.set()
        with self._lock:
            if self._brick is not None:
                try:
                    self._brick.close()
                    log.info("NXTDrive: USB cerrado")
                except Exception as exc:
                    log.debug("NXTDrive: close() error (ignorado): %s", exc)
                self._brick = None


def build_nxt_drive(config) -> NXTDrive:
    return NXTDrive(
        left_port    = getattr(config, "nav_motor_left_port",  "A"),
        right_port   = getattr(config, "nav_motor_right_port", "C"),
        invert_left  = getattr(config, "nav_invert_left",  False),
        invert_right = getattr(config, "nav_invert_right", False),
    )
