"""Safety checks for the two dangerous operations: run_geoprocessing and execute_python.

In safe mode (default), execute_python is disabled and geoprocessing is limited to an
allowlist of toolbox prefixes, with an optional explicit blocklist. No arcpy import.
"""


class SafetyError(Exception):
    """Raised when a command is rejected by policy."""


def check_gp_tool(tool: str, cfg) -> None:
    """Validate a dotted GP tool name (e.g. 'analysis.Buffer') against policy."""
    if not tool or "." not in tool and tool not in getattr(cfg, "blocked_gp_tools", []):
        # single-segment tool names are allowed through prefix logic below
        pass
    if not getattr(cfg, "safe_mode", True):
        return
    if tool in getattr(cfg, "blocked_gp_tools", []):
        raise SafetyError("GP tool '{}' is blocked by policy.".format(tool))
    prefixes = getattr(cfg, "allowed_gp_prefixes", []) or []
    if prefixes and not any(tool.startswith(p) for p in prefixes):
        raise SafetyError(
            "GP tool '{}' is not in the allowed toolboxes {}. "
            "Add its prefix to allowed_gp_prefixes or disable safe_mode.".format(tool, prefixes)
        )


def check_execute_python(cfg) -> None:
    """Gate arbitrary Python execution."""
    if getattr(cfg, "safe_mode", True) and not getattr(cfg, "allow_execute_python", False):
        raise SafetyError(
            "execute_python is disabled in safe mode. "
            "Set allow_execute_python=true in config.json to enable it."
        )
