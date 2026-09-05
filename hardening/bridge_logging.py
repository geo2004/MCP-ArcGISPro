"""Structured + audit logging for the MCP bridge (observability for support).

Writes a rotating log to <ipc_dir>/bridge.log. Every command is audited with op,
outcome and duration so you can diagnose a customer's session after the fact.
Only stdlib — no arcpy.
"""
import json
import logging
import os
from logging.handlers import RotatingFileHandler

_LOGGER_NAME = "mcp_bridge"


def get_logger(ipc_dir, level="INFO"):
    logger = logging.getLogger(_LOGGER_NAME)
    if logger.handlers:                     # idempotent across re-launches
        return logger
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    os.makedirs(ipc_dir, exist_ok=True)
    handler = RotatingFileHandler(
        os.path.join(ipc_dir, "bridge.log"),
        maxBytes=1_000_000, backupCount=3, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def audit(logger, op, args, ok_flag, elapsed_ms):
    """One audit line per command. Args are truncated so big code/payloads don't bloat the log."""
    try:
        a = json.dumps(args, default=str, ensure_ascii=False)
    except Exception:
        a = repr(args)
    if len(a) > 500:
        a = a[:500] + "...(truncated {} chars)".format(len(a) - 500)
    logger.info("AUDIT op=%s ok=%s ms=%d args=%s", op, ok_flag, int(elapsed_ms), a)
