"""Central configuration for the MCP bridge (drop-in hardening layer).

Externalizes settings that are currently hardcoded (e.g. the 15 s timeout) and adds
safety toggles. Loaded from <ipc_dir>/config.json with sane, safe-by-default values.
No arcpy import -> importable and testable anywhere.
"""
import json
import os
from dataclasses import dataclass, asdict, field
from typing import List

DEFAULT_IPC = os.path.join(os.path.expanduser("~"), ".arcgis_mcp")


@dataclass
class BridgeConfig:
    # --- protocol / transport ---
    protocol_version: int = 1
    timeout_seconds: int = 60            # was hardcoded 15 (too short for big geoprocessing)
    poll_interval: float = 0.1

    # --- observability ---
    log_level: str = "INFO"
    audit: bool = True

    # --- behavior ---
    auto_create_map: bool = False        # was silently True -> surprising side effect

    # --- safety (defaults are conservative; relax explicitly) ---
    safe_mode: bool = True
    allow_execute_python: bool = False   # arbitrary code execution OFF by default
    allowed_gp_prefixes: List[str] = field(default_factory=lambda: [
        "analysis.", "management.", "conversion.", "cartography.",
        "sa.", "ddd.", "stats.", "ca.", "intelligence.",
    ])
    # Destructive tools blocked by default in safe mode (alias-normalized, so the
    # one-part forms like 'Delete_management' are blocked too). Override per deployment.
    blocked_gp_tools: List[str] = field(default_factory=lambda: [
        "management.Delete", "management.DeleteFeatures", "management.DeleteRows",
        "management.DeleteField", "management.DeleteIdentical", "management.TruncateTable",
    ])

    ipc_dir: str = DEFAULT_IPC

    @classmethod
    def load(cls, path: str = None) -> "BridgeConfig":
        cfg = cls()
        path = path or os.path.join(cfg.ipc_dir, "config.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for k, v in (data or {}).items():
                    if hasattr(cfg, k):
                        setattr(cfg, k, v)
            except (json.JSONDecodeError, OSError):
                pass  # fall back to defaults on a corrupt/locked config
        return cfg

    def save(self, path: str = None) -> str:
        path = path or os.path.join(self.ipc_dir, "config.json")
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2)
        return path
