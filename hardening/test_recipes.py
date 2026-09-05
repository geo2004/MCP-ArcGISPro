"""Tests for the recipe orchestration logic. Mocked arcpy/map/proj, no ArcGIS Pro.
Run: python hardening/test_recipes.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
import recipes as R

_passed = _failed = 0


def check(label, cond):
    global _passed, _failed
    if cond:
        _passed += 1; print("  PASS:", label)
    else:
        _failed += 1; print("  FAIL:", label)


# ── fakes ─────────────────────────────────────────────────────────────────

class F:  # field
    def __init__(self, name, type_, nullable=True):
        self.name, self.type, self.isNullable = name, type_, nullable

class SR:
    def __init__(self, name, type_, wkid, unit=None):
        self.name, self.type, self.factoryCode, self.linearUnitName = name, type_, wkid, unit

class Desc:
    def __init__(self, shape, sr):
        self.shapeType, self.spatialReference = shape, sr

class CountRes:
    def __init__(self, n): self._n = n
    def getOutput(self, i): return str(self._n)

class Cur:
    def __init__(self, rows): self._rows = rows
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def __iter__(self): return iter(self._rows)

class Mgmt:
    def __init__(self, n): self._n = n
    def GetCount(self, lyr): return CountRes(self._n)

class DA:
    def __init__(self, table, shapes): self.table, self.shapes = table, shapes
    def SearchCursor(self, lyr, fields):
        if list(fields) == ["SHAPE@"]:
            return Cur([(v,) for v in self.shapes])
        return Cur([tuple(r.get(f) for f in fields) for r in self.table])

class FakeArcpy:
    def __init__(self, fields, desc, n, table, shapes):
        self._fields, self._desc = fields, desc
        self.management, self.da = Mgmt(n), DA(table, shapes)
    def Describe(self, lyr): return self._desc
    def ListFields(self, lyr): return self._fields

class Layer:
    def __init__(self, name, src): self.name, self.dataSource = name, src

class Map:
    def __init__(self, layers): self._l = layers
    def listLayers(self, name=None):
        return [x for x in self._l if name is None or x.name == name]

class Layout:
    def __init__(self, name, fail=False): self.name, self._fail = name, fail
    def exportToPDF(self, out):
        if self._fail: raise RuntimeError("export failed")
        open(out, "w").close()
    def exportToPNG(self, out, resolution=96): open(out, "w").close()

class Proj:
    def __init__(self, layouts): self._lay = layouts
    def listLayouts(self): return self._lay


# ── tests ───────────────────────────────────────────────────────────────────

def test_qa_layer():
    print("[qa_layer]")
    fields = [F("OBJECTID", "OID", False), F("Station", "String"), F("Shape", "Geometry")]
    # Geographic CRS + 1 null geometry out of 3 features
    arcpy = FakeArcpy(fields, Desc("Point", SR("GCS_WGS_1984", "Geographic", 4326)),
                      n=3, table=[], shapes=[object(), None, object()])
    m = Map([Layer("pts", r"C:\d.gdb\pts")])
    rep = R.qa_layer(arcpy, m, "pts")
    check("feature_count", rep["feature_count"] == 3)
    check("geometry_type", rep["geometry_type"] == "Point")
    check("crs reflected", rep["crs"]["wkid"] == 4326 and rep["crs"]["type"] == "Geographic")
    check("field_count", rep["field_count"] == 3)
    check("null geometry counted", rep["null_geometry"] == 1)
    check("geographic CRS warned", any("geographic" in i.lower() for i in rep["issues"]))
    check("null geometry warned", any("null" in i.lower() for i in rep["issues"]))
    check("ok=False when issues", rep["ok"] is False)

    raised = False
    try:
        R.qa_layer(arcpy, m, "missing")
    except ValueError:
        raised = True
    check("missing layer -> ValueError", raised)


def test_export_csv():
    print("[export_attributes_csv]")
    fields = [F("OBJECTID", "OID", False), F("Station", "String"), F("Shape", "Geometry")]
    table = [{"OBJECTID": 1, "Station": "P1"}, {"OBJECTID": 2, "Station": None}]
    arcpy = FakeArcpy(fields, Desc("Point", SR("x", "Projected", 32718, "Meter")), 2, table, [])
    m = Map([Layer("pts", "src")])
    d = tempfile.mkdtemp()
    out = os.path.join(d, "attrs.csv")
    res = R.export_attributes_csv(arcpy, m, "pts", out)
    check("excludes Geometry field", res["fields"] == ["OBJECTID", "Station"])
    check("row count", res["rows"] == 2)
    check("file exists", os.path.exists(out))
    with open(out, "r", encoding="utf-8-sig") as fh:
        text = fh.read()
    check("header written", text.splitlines()[0] == "OBJECTID,Station")
    check("None -> empty cell", text.splitlines()[2] == "2,")


def test_batch_export():
    print("[batch_export_layouts]")
    d = tempfile.mkdtemp()
    proj = Proj([Layout("Mapa A"), Layout("Plano B/2"), Layout("Bad", fail=True)])
    res = R.batch_export_layouts(proj, d, fmt="PDF")
    check("exported the 2 good layouts", res["count"] == 2)
    check("recorded 1 error", len(res["errors"]) == 1 and res["errors"][0]["layout"] == "Bad")
    check("total layouts", res["total_layouts"] == 3)
    check("filename sanitized", os.path.exists(os.path.join(d, "Plano_B_2.pdf")))
    check("pdf files exist", all(os.path.exists(e["output"]) for e in res["exported"]))

    bad = False
    try:
        R.batch_export_layouts(proj, d, fmt="DOCX")
    except ValueError:
        bad = True
    check("unsupported format -> ValueError", bad)


def test_field_stats():
    print("[field_stats]")
    fields = [F("P_mm", "Integer")]
    table = [{"P_mm": 8}, {"P_mm": 2}, {"P_mm": None}, {"P_mm": 5}]
    arcpy = FakeArcpy(fields, Desc("Point", SR("x", "Projected", 32718, "Meter")), 4, table, [])
    m = Map([Layer("pts", "src")])
    r = R.field_stats(arcpy, m, "pts", "P_mm")
    s = r["stats"]
    check("count excludes null", s["count"] == 3)
    check("nulls counted", s["nulls"] == 1)
    check("min", s["min"] == 2)
    check("max", s["max"] == 8)
    check("sum", s["sum"] == 15)
    check("mean", s["mean"] == 5.0)
    check("flagged numeric", r["numeric"] is True)


def test_value_counts():
    print("[value_counts]")
    fields = [F("ubic", "String")]
    table = [{"ubic": "LD"}, {"ubic": "LI"}, {"ubic": "LD"}, {"ubic": None}, {"ubic": "LD"}]
    arcpy = FakeArcpy(fields, Desc("Point", SR("x", "Projected", 32718, "Meter")), 5, table, [])
    m = Map([Layer("pts", "src")])
    r = R.value_counts(arcpy, m, "pts", "ubic")
    check("distinct count", r["distinct"] == 2)
    check("nulls counted", r["nulls"] == 1)
    check("top value is LD with 3", r["top"][0] == {"value": "LD", "count": 3})


if __name__ == "__main__":
    test_qa_layer()
    test_export_csv()
    test_batch_export()
    test_field_stats()
    test_value_counts()
    print("\n{} passed, {} failed".format(_passed, _failed))
    sys.exit(1 if _failed else 0)
