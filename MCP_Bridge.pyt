# -*- coding: utf-8 -*-
"""
MCP Bridge — ArcGIS Pro Python Toolbox
======================================
One-click Start / Stop / Status for the ArcGIS Pro MCP bridge, so users no
longer have to paste `exec(open(...).read())` into the Python window every
session.

How it works
------------
ArcGIS Pro runs Python toolbox tools *in-process* (the same interpreter that
backs `arcpy.mp.ArcGISProject("CURRENT")`). The "Start" tool exec's the
existing ``pro_bridge.py`` (single source of truth for the 30 command handlers)
into a private namespace, which spins up the bridge's daemon polling thread.
Because that thread is a daemon living in the application's interpreter, it
keeps running after the tool finishes — until you press "Stop" or close Pro.

A handle to the bridge namespace is stashed on the ``arcpy`` module object
(``arcpy._mcp_bridge_ns``), which persists for the whole Pro session, so the
Stop / Status tools can find and control a bridge started by the Start tool.

Install
-------
1. Put this file next to ``pro_bridge.py`` (same folder).
2. In ArcGIS Pro: Catalog pane -> Toolboxes -> right-click -> Add Toolbox ->
   select ``MCP_Bridge.pyt``.
3. Open a project with a Map, then double-click "Start MCP Bridge" -> Run.
   (Right-click the tool -> Add To Favorites for one-click access.)
"""

import os
import arcpy


# --- shared helpers ---------------------------------------------------------

_HANDLE_ATTR = "_mcp_bridge_ns"   # stashed on the arcpy module for the session


def _get_handle():
    return getattr(arcpy, _HANDLE_ATTR, None)


def _is_running(ns):
    if not ns:
        return False
    t = ns.get("_thread")
    return bool(t is not None and t.is_alive() and ns.get("_bridge_active"))


# --- toolbox ----------------------------------------------------------------

class Toolbox(object):
    def __init__(self):
        self.label = "MCP Bridge"
        self.alias = "mcpbridge"
        self.tools = [StartBridge, StopBridge, BridgeStatus]


class StartBridge(object):
    def __init__(self):
        self.label = "Start MCP Bridge"
        self.description = ("Start the ArcGIS Pro MCP bridge so Claude (or any "
                            "MCP client) can drive this Pro session.")
        self.canRunInBackground = False

    def getParameterInfo(self):
        return []

    def isLicensed(self):
        return True

    def execute(self, parameters, messages):
        ns = _get_handle()
        if _is_running(ns):
            arcpy.AddWarning("MCP Bridge is already running.")
            arcpy.AddMessage("IPC dir: {}".format(ns.get("IPC_DIR")))
            return

        bridge_path = os.path.join(os.path.dirname(__file__), "pro_bridge.py")
        if not os.path.exists(bridge_path):
            arcpy.AddError("pro_bridge.py not found next to the toolbox:\n  {}"
                           .format(bridge_path))
            return

        try:
            with open(bridge_path, "r", encoding="utf-8") as f:
                code = f.read()
            ns = {"__file__": bridge_path, "__name__": "mcp_pro_bridge"}
            # Executing pro_bridge.py caches ArcGISProject("CURRENT") and starts
            # the daemon polling thread.
            exec(compile(code, bridge_path, "exec"), ns)
        except Exception as e:
            arcpy.AddError("Failed to start the bridge: {}".format(e))
            arcpy.AddError("Make sure a project with a Map/Scene/Globe is open.")
            return

        setattr(arcpy, _HANDLE_ATTR, ns)
        proj = ns.get("_proj")
        arcpy.AddMessage("✓ MCP Bridge started.")
        arcpy.AddMessage("Project : {}".format(
            (getattr(proj, "filePath", None) or "(unsaved)")))
        arcpy.AddMessage("IPC dir : {}".format(ns.get("IPC_DIR")))
        arcpy.AddMessage("The bridge keeps running in the background. "
                         "Press 'Stop MCP Bridge' or close ArcGIS Pro to end it.")


class StopBridge(object):
    def __init__(self):
        self.label = "Stop MCP Bridge"
        self.description = "Stop the running ArcGIS Pro MCP bridge."
        self.canRunInBackground = False

    def getParameterInfo(self):
        return []

    def isLicensed(self):
        return True

    def execute(self, parameters, messages):
        ns = _get_handle()
        if not ns:
            arcpy.AddWarning("MCP Bridge is not running (no handle found).")
            return
        ns["_bridge_active"] = False          # the poll loop checks this flag
        try:
            delattr(arcpy, _HANDLE_ATTR)
        except AttributeError:
            pass
        arcpy.AddMessage("✓ Stop signal sent. The bridge thread exits within ~0.2 s.")


class BridgeStatus(object):
    def __init__(self):
        self.label = "MCP Bridge Status"
        self.description = "Report whether the ArcGIS Pro MCP bridge is running."
        self.canRunInBackground = False

    def getParameterInfo(self):
        return []

    def isLicensed(self):
        return True

    def execute(self, parameters, messages):
        ns = _get_handle()
        if _is_running(ns):
            proj = ns.get("_proj")
            arcpy.AddMessage("MCP Bridge: RUNNING")
            arcpy.AddMessage("Project : {}".format(
                (getattr(proj, "filePath", None) or "(unsaved)")))
            arcpy.AddMessage("IPC dir : {}".format(ns.get("IPC_DIR")))
        else:
            arcpy.AddMessage("MCP Bridge: STOPPED")
