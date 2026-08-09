"""Observability for the deterministic CLI: a 24h-TTL debug log + a live stderr heartbeat.

The CLI is single-shot, but several of its verbs run for MINUTES with no output — transcription
(`mlx_whisper` subprocess), dubbing (`dub run` loops over hundreds of per-cue TTS subprocess
calls, then ffmpeg concat/loudnorm), and the ffmpeg mux/concat/slice ops. Run externally, that is
completely opaque, and there is no record to debug a failure from. This module fixes both:

  * **Debug log** — one ndjson file per line under ``<root>/.claude/logs/vid_cli.ndjson`` (house
    style, same as ``events.ndjson`` / ``hook-audit.ndjson``), size-rotated (a new file when it
    grows big: ``vid_cli.ndjson.1``, ``.2``, …) and pruned to a 24h TTL on each init. Every CLI
    invocation logs a start + end (or exception) record.

  * **Heartbeat** — a daemon thread that prints ONE liveness line to **stderr** every ~30s while a
    long command runs (``vid_cli ⏳ <cmd> <project> — <phase> (<detail>, <elapsed>)``). Long-running
    call sites update a shared phase/progress via ``phase()`` / ``progress()``; the thread reads it.

Design rules honored:
  * stdout stays PURE JSON — everything here goes to stderr and the log file, never stdout (callers
    like ``run_pipeline.sh`` parse stdout as JSON).
  * stdlib only (``logging`` / ``threading`` / ``time`` / ``os``) — no new deps.
  * Pure instrumentation: no state / artifact / approval writes. Observability NEVER crashes a real
    run — every path here suppresses its own errors.
  * ``phase()`` / ``progress()`` are safe NO-OPS when no heartbeat is active (unit tests, or
    ``VIDTRANS_NO_HEARTBEAT=1``), so deep call sites can call them unconditionally.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

# --- tunables (env-overridable) ----------------------------------------------
_LOG_TTL_SECONDS = 24 * 60 * 60          # prune log files older than 24h (by mtime)
_LOG_MAX_BYTES = 5 * 1024 * 1024         # rotate the active log at ~5 MB (new file when big)
_LOG_BACKUP_COUNT = 10                   # keep up to 10 rotated backups
_DEFAULT_HEARTBEAT_SECS = 30.0


def _log_dir(root: Path) -> Path:
    return Path(root) / ".claude" / "logs"


def logs_path(root: Path) -> Path:
    """Absolute path of the active debug log (``<root>/.claude/logs/vid_cli.ndjson``)."""
    return _log_dir(root) / "vid_cli.ndjson"


# --- ndjson file logger ------------------------------------------------------

_LOGGER_NAME = "vid_cli.obs"
_logger: logging.Logger | None = None


class _NdjsonFormatter(logging.Formatter):
    """Render each record as one JSON object per line.

    The structured fields live on ``record.__dict__['fields']`` (a dict set by the log helpers);
    the human message is ``record.getMessage()``. Timestamps are ISO-8601 UTC to match the rest of
    the repo (util.utc_now)."""

    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created))
        payload: dict[str, Any] = {
            "ts": ts,
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            for key, value in fields.items():
                if key not in payload:
                    payload[key] = value
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _prune_old_logs(directory: Path) -> None:
    """Delete any ``vid_cli.ndjson*`` whose mtime is older than the 24h TTL.

    Runs once per CLI invocation at logger init. Combined with size rotation this bounds the total
    log footprint: a rotated backup is removed 24h after its last write. Suppresses all errors —
    pruning must never break a real run."""
    now = time.time()
    with contextlib.suppress(OSError):
        for child in directory.glob("vid_cli.ndjson*"):
            with contextlib.suppress(OSError):
                if now - child.stat().st_mtime > _LOG_TTL_SECONDS:
                    child.unlink()


def get_logger(root: Path) -> logging.Logger:
    """Idempotently init the ndjson debug logger for ``root`` (prunes old logs first).

    Safe to call repeatedly — the file handler is attached once. Never raises: if the log dir can't
    be created (read-only fs, etc.) it returns a logger with no handlers, so callers can log freely
    and it's simply a no-op."""
    global _logger
    if _logger is not None:
        return _logger
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False  # never bubble to the root logger / stderr
    try:
        directory = _log_dir(root)
        directory.mkdir(parents=True, exist_ok=True)
        _prune_old_logs(directory)
        handler = RotatingFileHandler(
            logs_path(root), maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(_NdjsonFormatter())
        logger.addHandler(handler)
    except OSError:
        # No writable log dir — degrade to a handler-less logger (all logs become no-ops).
        pass
    _logger = logger
    return logger


def _log(level: int, msg: str, **fields: Any) -> None:
    """Emit one structured record. A no-op if the logger was never initialized."""
    if _logger is None:
        return
    with contextlib.suppress(Exception):
        _logger.log(level, msg, extra={"fields": fields})


def log_info(msg: str, **fields: Any) -> None:
    _log(logging.INFO, msg, **fields)


def log_warn(msg: str, **fields: Any) -> None:
    _log(logging.WARNING, msg, **fields)


def log_error(msg: str, **fields: Any) -> None:
    _log(logging.ERROR, msg, **fields)


# --- heartbeat ---------------------------------------------------------------

def _fmt_elapsed(seconds: float) -> str:
    total = int(seconds)
    m, s = divmod(total, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}h{m:02d}m{s:02d}s"
    return f"{m}m{s:02d}s"


class Heartbeat:
    """A daemon thread that prints a liveness line to stderr every ``interval`` seconds.

    The main thread updates ``phase`` / progress via ``set_phase`` / ``set_progress`` (cheap,
    lock-guarded); the thread only reads them and renders one line. Because the CLI's long ops sit
    in a blocking ``subprocess.run`` on the main thread, the heartbeat MUST be its own thread to
    keep ticking. Every line is also mirrored into the debug log so ``tail``-ing it shows liveness.

    Use as a context manager: ``with Heartbeat(cmd, pid): ...`` — it starts on enter and is joined
    on exit. All internals suppress errors; observability never breaks a real run.
    """

    def __init__(
        self,
        cmd: str,
        project_id: str | None,
        *,
        interval: float = _DEFAULT_HEARTBEAT_SECS,
        stream: Any = None,
    ) -> None:
        self._cmd = cmd
        self._project_id = project_id
        self._interval = max(1.0, interval)
        self._stream = stream if stream is not None else sys.stderr
        self._lock = threading.Lock()
        self._phase: str | None = None
        self._progress: tuple[int, int, str] | None = None  # (done, total, unit)
        self._start = time.monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- shared-state updates (main thread) --
    def set_phase(self, phase: str | None) -> None:
        with self._lock:
            self._phase = phase
            self._progress = None  # a new phase clears any stale per-item count

    def set_progress(self, done: int, total: int, unit: str = "item") -> None:
        with self._lock:
            self._progress = (int(done), int(total), unit)

    # -- lifecycle --
    def start(self) -> Heartbeat:
        self._start = time.monotonic()
        self._thread = threading.Thread(target=self._run, name="vid_cli-heartbeat", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            with contextlib.suppress(Exception):
                thread.join(timeout=2.0)

    def __enter__(self) -> Heartbeat:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- the thread --
    def _render(self) -> str:
        with self._lock:
            phase = self._phase
            progress = self._progress
        elapsed = _fmt_elapsed(time.monotonic() - self._start)
        head = f"vid_cli ⏳ {self._cmd}"
        if self._project_id:
            head += f" {self._project_id}"
        bits: list[str] = []
        if phase:
            bits.append(phase)
        detail = f"{elapsed} elapsed"
        if progress:
            done, total, unit = progress
            detail = f"{unit} {done}/{total}, {elapsed} elapsed"
        tail = f"({detail})" if not bits else f"— {'; '.join(bits)} ({detail})"
        return f"{head} {tail}"

    def _run(self) -> None:
        # Wait first so a fast command never prints a heartbeat at all.
        while not self._stop.wait(self._interval):
            line = self._render()
            with contextlib.suppress(Exception):
                print(line, file=self._stream, flush=True)
            # Mirror liveness into the debug log too.
            with self._lock:
                progress = self._progress
                phase = self._phase
            fields: dict[str, Any] = {"cmd": self._cmd, "kind": "heartbeat"}
            if self._project_id:
                fields["project_id"] = self._project_id
            if phase:
                fields["phase"] = phase
            if progress:
                fields["done"], fields["total"], fields["unit"] = progress
            log_info("heartbeat", **fields)


# --- module-level "current heartbeat" so deep call sites need no plumbing -----
# cli.main installs the active heartbeat here; run_dub / transcript / media call the free
# functions below unconditionally. When nothing is installed (unit tests, quiet mode) they are
# safe no-ops, so no other module has to know whether a heartbeat exists.

_current: Heartbeat | None = None


def set_current(heartbeat: Heartbeat | None) -> None:
    global _current
    _current = heartbeat


def current() -> Heartbeat | None:
    return _current


def phase(label: str | None) -> None:
    """Set the current heartbeat phase label (no-op if no heartbeat is active)."""
    hb = _current
    if hb is not None:
        with contextlib.suppress(Exception):
            hb.set_phase(label)


def progress(done: int, total: int, unit: str = "item") -> None:
    """Report per-item progress to the current heartbeat (no-op if none active)."""
    hb = _current
    if hb is not None:
        with contextlib.suppress(Exception):
            hb.set_progress(done, total, unit)


def heartbeat_enabled() -> bool:
    """Whether a live stderr heartbeat should run. On unless ``VIDTRANS_NO_HEARTBEAT=1``.

    Deliberately stays ON for non-TTY stderr too, so a redirected ``run_pipeline.sh`` run still
    records liveness (the operator watches the redirected file / log)."""
    return os.environ.get("VIDTRANS_NO_HEARTBEAT") not in ("1", "true", "yes")


def heartbeat_interval() -> float:
    """Heartbeat cadence in seconds (``VIDTRANS_HEARTBEAT_SECS``, default 30)."""
    raw = os.environ.get("VIDTRANS_HEARTBEAT_SECS")
    if raw:
        with contextlib.suppress(ValueError):
            return max(1.0, float(raw))
    return _DEFAULT_HEARTBEAT_SECS
