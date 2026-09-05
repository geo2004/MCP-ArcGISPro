"""Unit tests for the hardening layer. Pure Python, no arcpy required.
Run: python hardening/test_hardening.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))

import bridge_config as bc
import bridge_safety as bs
import bridge_helpers as bh


# --- minimal fakes for resolve_dataset (no arcpy) ---------------------------

class FakeArcpy:
    def __init__(self, existing_paths):
        self._existing = set(existing_paths)
    def Exists(self, p):
        return p in self._existing

class FakeLayer:
    def __init__(self, name, data_source):
        self.name = name
        self.dataSource = data_source

class FakeMap:
    def __init__(self, layers):
        self._layers = layers
    def listLayers(self, name=None):
        if name is None:
            return list(self._layers)
        return [l for l in self._layers if l.name == name]


_passed = 0
_failed = 0

def check(label, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print("  PASS:", label)
    else:
        _failed += 1
        print("  FAIL:", label)


def test_config_roundtrip():
    print("[config]")
    cfg = bc.BridgeConfig()
    check("default timeout is 60 (was 15)", cfg.timeout_seconds == 60)
    check("safe_mode default True", cfg.safe_mode is True)
    check("auto_create_map default False", cfg.auto_create_map is False)
    check("execute_python off by default", cfg.allow_execute_python is False)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "config.json")
        cfg.timeout_seconds = 120
        cfg.save(p)
        cfg2 = bc.BridgeConfig.load(p)
        check("round-trip persists timeout", cfg2.timeout_seconds == 120)
        # unknown keys ignored, missing file -> defaults
        check("missing file -> defaults", bc.BridgeConfig.load(os.path.join(d, "nope.json")).timeout_seconds == 60)


def test_safety():
    print("[safety]")
    cfg = bc.BridgeConfig()  # safe_mode True
    # destructive tools blocked by DEFAULT (both alias forms)
    def _isblk(t):
        try:
            bs.check_gp_tool(t, bc.BridgeConfig()); return False
        except bs.SafetyError:
            return True
    check("management.Delete blocked by default", _isblk("management.Delete"))
    check("Delete_management blocked by default (alias)", _isblk("Delete_management"))
    # execute_python blocked in safe mode
    blocked = False
    try:
        bs.check_execute_python(cfg)
    except bs.SafetyError:
        blocked = True
    check("execute_python blocked in safe mode", blocked)

    cfg.allow_execute_python = True
    ok_now = True
    try:
        bs.check_execute_python(cfg)
    except bs.SafetyError:
        ok_now = False
    check("execute_python allowed when opted in", ok_now)

    # allowed prefix passes, unknown prefix blocked
    passed = True
    try:
        bs.check_gp_tool("analysis.Buffer", cfg)
    except bs.SafetyError:
        passed = False
    check("analysis.Buffer allowed", passed)

    denied = False
    try:
        bs.check_gp_tool("evil.DoBadThing", cfg)
    except bs.SafetyError:
        denied = True
    check("unknown toolbox blocked in safe mode", denied)

    # legacy one-part names: 'Buffer_analysis' allowed, 'DoBad_evil' blocked
    def _blk(t):
        try:
            bs.check_gp_tool(t, cfg); return False
        except bs.SafetyError:
            return True
    check("one-part Buffer_analysis allowed", not _blk("Buffer_analysis"))
    check("one-part CopyFeatures_management allowed", not _blk("CopyFeatures_management"))
    check("one-part DoBad_evil blocked", _blk("DoBad_evil"))

    # blocklist wins
    cfg.blocked_gp_tools = ["management.Delete"]
    blk = False
    try:
        bs.check_gp_tool("management.Delete", cfg)
    except bs.SafetyError:
        blk = True
    check("blocklisted tool rejected", blk)
    # blocklist can't be bypassed via the equivalent one-part form
    bypass_blocked = False
    try:
        bs.check_gp_tool("Delete_management", cfg)
    except bs.SafetyError:
        bypass_blocked = True
    check("blocklist not bypassable via alias form", bypass_blocked)
    # blocklist can't be bypassed via case (management.delete vs blocked management.Delete)
    check("blocklist not bypassable via lowercase", _blk("management.delete"))
    check("blocklist not bypassable via uppercase", _blk("MANAGEMENT.DELETE"))
    check("blocklist not bypassable via mixed-case alias form", _blk("delete_MANAGEMENT"))

    # safe_mode off -> anything passes
    cfg.safe_mode = False
    anyok = True
    try:
        bs.check_gp_tool("evil.DoBadThing", cfg)
        bs.check_execute_python(cfg)
    except bs.SafetyError:
        anyok = False
    check("safe_mode off allows all", anyok)


def test_resolve_dataset():
    print("[resolve_dataset]")
    gdb = r"C:\proj\data.gdb"
    src = gdb + r"\buffer50"
    arcpy = FakeArcpy(existing_paths={src})
    m = FakeMap([FakeLayer("Buffer 50m", src)])

    # 1) existing path returned unchanged
    check("path passthrough", bh.resolve_dataset(arcpy, m, src) == src)
    # 2) layer name -> dataSource
    check("layer name -> dataSource", bh.resolve_dataset(arcpy, m, "Buffer 50m") == src)
    # 3) unknown -> ValueError
    raised = False
    try:
        bh.resolve_dataset(arcpy, m, "does not exist")
    except ValueError:
        raised = True
    check("unknown raises ValueError", raised)

    # resolve_param (soft): layer name -> source; non-dataset params pass through
    check("resolve_param layer name -> source", bh.resolve_param(arcpy, m, "Buffer 50m") == src)
    check("resolve_param path passthrough", bh.resolve_param(arcpy, m, src) == src)
    check("resolve_param distance passthrough", bh.resolve_param(arcpy, m, "10 Meters") == "10 Meters")
    check("resolve_param number passthrough", bh.resolve_param(arcpy, m, 500) == 500)
    check("resolve_param unknown passthrough (no raise)", bh.resolve_param(arcpy, m, "nope") == "nope")


def test_recipe_gate():
    print("[recipe_gate]")
    import recipes as rc

    # 1) every recipe declares gp_tools (a list) so handle_recipe can gate it
    all_declared = all(isinstance(getattr(fn, "gp_tools", None), list)
                       for fn in rc.RECIPES.values())
    check("every recipe declares a gp_tools list", all_declared)

    # 2) the gate handle_recipe applies: check_gp_tool over each declared tool.
    #    qa_layer declares management.GetCount -> allowed by default policy...
    cfg = bc.BridgeConfig()  # safe_mode True, default allow/blocklists

    def _gate(fn):
        """Mirror handle_recipe's gate: raises SafetyError if any declared tool blocked."""
        for t in (getattr(fn, "gp_tools", []) or []):
            bs.check_gp_tool(t, cfg)

    def _blocked(fn):
        try:
            _gate(fn); return False
        except bs.SafetyError:
            return True

    check("qa_layer passes the gate under default policy", not _blocked(rc.RECIPES["qa_layer"]))
    check("read-only recipe (value_counts) passes the gate", not _blocked(rc.RECIPES["value_counts"]))

    # 3) ...but if the recipe's declared GP tool is blocklisted, the gate rejects it.
    cfg.blocked_gp_tools = ["management.GetCount"]
    check("recipe blocked when its declared GP tool is blocklisted", _blocked(rc.RECIPES["qa_layer"]))

    # 4) a hypothetical destructive recipe declaring a default-blocked tool is caught
    def _fake_destructive_recipe(**_):
        return {}
    _fake_destructive_recipe.gp_tools = ["management.DeleteFeatures"]
    cfg2 = bc.BridgeConfig()  # default blocklist includes DeleteFeatures
    caught = False
    try:
        for t in _fake_destructive_recipe.gp_tools:
            bs.check_gp_tool(t, cfg2)
    except bs.SafetyError:
        caught = True
    check("destructive recipe caught by default blocklist", caught)


def test_envelopes():
    print("[envelopes]")
    o = bh.ok({"x": 1})
    check("ok envelope shape", o["ok"] and o["data"] == {"x": 1} and o["protocol"] == bh.PROTOCOL_VERSION)
    e = bh.err("boom", code="TIMEOUT")
    check("err keeps string error (compat)", e["error"] == "boom" and e["ok"] is False)
    check("err adds structured code", e["error_code"] == "TIMEOUT")


if __name__ == "__main__":
    test_config_roundtrip()
    test_safety()
    test_resolve_dataset()
    test_recipe_gate()
    test_envelopes()
    print("\n{} passed, {} failed".format(_passed, _failed))
    sys.exit(1 if _failed else 0)
