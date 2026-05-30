"""Core VIN decoder — port of NHTSA vPIC spvindecode to Python/SQLite."""

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .check_digit import compute_check_digit, validate_vin_chars
from .lookups import LookupResolver


@dataclass
class DecItem:
    decoding_id: int
    created_on: str | None
    pattern_id: int | None
    keys: str
    vinschema_id: int | None
    wmi_id: int | None
    element_id: int
    attribute_id: str
    value: str
    source: str
    priority: int
    tobe_qced: bool | None = None
    return_code: str = ""


@dataclass
class DecodeResult:
    vin: str
    results: dict[str, str] = field(default_factory=dict)
    error_code: str = ""
    error_text: str = ""


GROUP_ORDER = {
    "": 0, "General": 1, "Exterior / Body": 2, "Exterior / Dimension": 3,
    "Exterior / Truck": 4, "Exterior / Trailer": 5, "Exterior / Wheel tire": 6,
    "Exterior / Motorcycle": 7, "Exterior / Bus": 8, "Interior": 9,
    "Interior / Seat": 10, "Mechanical / Transmission": 11,
    "Mechanical / Drivetrain": 12, "Mechanical / Brake": 13,
    "Mechanical / Battery": 14, "Mechanical / Battery / Charger": 15,
    "Engine": 16, "Passive Safety System": 17,
    "Passive Safety System / Air Bag Location": 18,
    "Active Safety System": 19,
    "Active Safety System / Maintaining Safe Distance": 20,
    "Active Safety System / Forward Collision Prevention": 21,
    "Active Safety System / Lane and Side Assist": 22,
    "Active Safety System / Backing Up and Parking": 23,
    "Active Safety System / 911 Notification": 24,
    "Active Safety System / Lighting Technologies": 25,
    "Internal": 26,
}

# Elements that allow multiple values (don't deduplicate)
MULTI_VALUE_ELEMENTS = {121, 129, 150, 154, 155, 114, 169, 186}

# Year code mapping: VIN position 10 → possible years
YEAR_CODES = {}
_chars = "ABCDEFGHJKLMNPRSTVWXY123456789"
for _i, _c in enumerate(_chars):
    _y = 1980 + _i
    YEAR_CODES[_c] = _y
    if _y + 30 <= datetime.now().year + 2:
        YEAR_CODES.setdefault(_c, _y)


def _sqlwild_to_regex(pattern: str) -> str:
    """Convert SQL wildcard pattern to Python regex (port of vpic.sqlwild_to_regex)."""
    out = ""
    for ch in pattern:
        if ch == "*":
            out += "."
        elif ch in "[]":
            out += ch
        elif ch == "|":
            out += r"\|"
        elif ch in r"\\.^$+?{}()":
            out += "\\" + ch
        else:
            out += ch
    out = out.replace("1-A", "1A")
    return "^" + out + ".*"


def _vin_descriptor(vin: str) -> str:
    """Extract VIN descriptor: positions 1-8 + 10 (skip check digit at 9)."""
    if len(vin) < 10:
        return vin
    return vin[:8] + vin[9]


def _vin_wmi(vin: str) -> str:
    """Extract WMI (World Manufacturer Identifier) from VIN."""
    if len(vin) < 3:
        return vin
    wmi = vin[:3]
    if wmi[2] == "9" and len(vin) >= 14:
        wmi = vin[:3] + vin[11:14]
    return wmi


def _vin_model_year(vin: str) -> tuple[int | None, int | None]:
    """Extract model year(s) from VIN position 10.
    Returns (primary_year, alternate_year). Alternate is +30 if ambiguous."""
    if len(vin) < 10:
        return None, None
    code = vin[9]
    if code not in YEAR_CODES:
        return None, None
    base = YEAR_CODES[code]
    alt = base + 30
    now = datetime.now().year
    if alt <= now + 2:
        return alt, base
    return base, None


def _keys_match(pattern_keys: str, vin_keys: str) -> bool:
    """Check if a pattern's keys match the VIN's key positions."""
    if "[" in pattern_keys:
        regex = _sqlwild_to_regex(pattern_keys)
        return bool(re.match(regex, vin_keys))
    sql_like = pattern_keys.replace("*", ".")
    return bool(re.match("^" + sql_like, vin_keys))


class VinDecoder:
    def __init__(self, db_path: str | Path = "vpic.db"):
        self._db_path = str(db_path)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._resolver = LookupResolver(self._conn)
        self._elements = self._load_elements()
        self._wmi_cache: dict[str, dict | None] = {}
        self._schema_cache: dict[tuple[str, int], list] = {}
        self._pattern_cache: dict[int, list] = {}
        self._error_cache: dict[int, str] = self._load_errors()
        self._conversion_cache: list | None = None
        self._default_cache: dict[int, list] = {}

    def _load_errors(self) -> dict[int, str]:
        rows = self._conn.execute("SELECT id, name FROM errorcode").fetchall()
        return {r["id"]: r["name"] for r in rows}

    def _load_elements(self) -> dict[int, dict]:
        rows = self._conn.execute("SELECT * FROM element").fetchall()
        return {r["id"]: dict(r) for r in rows}

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def decode(self, vin: str, year: int | None = None) -> DecodeResult:
        vin = vin.upper().strip()
        result = DecodeResult(vin=vin)

        if len(vin) != 17:
            result.error_code = "6"
            result.error_text = "VIN must be 17 characters"
            return result

        descriptor = _vin_descriptor(vin)
        wmi_str = _vin_wmi(vin)

        # Look up WMI (cached)
        wmi_key = wmi_str[:3]
        if wmi_key not in self._wmi_cache:
            row = self._conn.execute(
                "SELECT id, manufacturerid, vehicletypeid, trucktypeid "
                "FROM wmi WHERE wmi = ? AND (publicavailabilitydate IS NULL OR publicavailabilitydate <= datetime('now'))",
                (wmi_key,),
            ).fetchone()
            self._wmi_cache[wmi_key] = dict(row) if row else None

        wmi_row = self._wmi_cache[wmi_key]
        if wmi_row is None:
            result.error_code = "7"
            result.error_text = "Manufacturer not recognized (WMI not found)"
            return result

        wmi_id = wmi_row["id"]
        vehicle_type_id = wmi_row["vehicletypeid"]
        truck_type_id = wmi_row["trucktypeid"]

        # Determine model year
        dmy = self._conn.execute(
            "SELECT modelyear FROM vindescriptor WHERE descriptor = ?",
            (descriptor,),
        ).fetchone()
        dmy = dmy["modelyear"] if dmy else None

        primary_year, alt_year = _vin_model_year(vin)

        # Build decode passes
        passes = []
        now_year = datetime.now().year

        if dmy and 1980 <= dmy <= now_year + 2:
            passes.append((1, dmy, True))
        else:
            if year and 1980 <= year <= now_year + 2:
                if year != primary_year and year != alt_year:
                    passes.append((2, year, True))

            if primary_year:
                passes.append((3, primary_year, dmy is not None))
            if alt_year:
                passes.append((4, alt_year, dmy is not None))

        if not passes and primary_year:
            passes.append((3, primary_year, False))

        # Run each pass
        all_items: dict[int, list[DecItem]] = {}
        vin_keys = ""
        if len(vin) > 3:
            vin_keys = vin[3:8]
            if len(vin) > 9:
                vin_keys += "|" + vin[9:17]

        for pass_num, model_year, conclusive in passes:
            items = self._decode_core(
                pass_num, model_year, vin, wmi_str[:3], wmi_id,
                vin_keys, vehicle_type_id
            )
            all_items[pass_num] = items

        if not all_items:
            result.error_code = "8"
            result.error_text = "No matching patterns found"
            return result

        # Pick best pass
        best_pass = self._pick_best_pass(all_items, year)
        items = all_items[best_pass]

        # Resolve XXX values through lookup tables
        for item in items:
            if item.value == "XXX":
                item.value = self._resolver.resolve(item.element_id, item.attribute_id)

        # Add defaults for missing elements
        if vehicle_type_id:
            existing_elements = {item.element_id for item in items}
            defaults = self._conn.execute(
                "SELECT elementid, defaultvalue FROM defaultvalue WHERE vehicletypeid = ? AND defaultvalue IS NOT NULL",
                (vehicle_type_id,),
            ).fetchall()
            for dv in defaults:
                if dv["elementid"] not in existing_elements:
                    elem = self._elements.get(dv["elementid"], {})
                    val = dv["defaultvalue"]
                    if elem.get("datatype") == "lookup" and val == "0":
                        val = "Not Applicable"
                    else:
                        val = self._resolver.resolve(dv["elementid"], val)
                    items.append(DecItem(
                        decoding_id=best_pass, created_on=None, pattern_id=None,
                        keys="", vinschema_id=None, wmi_id=None,
                        element_id=dv["elementid"], attribute_id=dv["defaultvalue"],
                        value=val, source="Default", priority=10,
                    ))

        # Check digit validation
        is_car_mpv_lt = vehicle_type_id in (2, 7) or (vehicle_type_id == 3 and truck_type_id == 1)
        error_codes = []

        calc_cd = compute_check_digit(vin, is_car_mpv_lt)
        actual_cd = vin[8]

        is_exception = self._conn.execute(
            "SELECT 1 FROM vinexception WHERE vin = ? AND checkdigit = 1", (vin,)
        ).fetchone() is not None

        if actual_cd != calc_cd and not is_exception:
            error_codes.append(1)

        invalid_chars = validate_vin_chars(vin, is_car_mpv_lt)
        if invalid_chars:
            error_codes.append(400)

        has_patterns = any(
            item.source in ("Pattern", "EngineModelPattern", "Formula Pattern")
            and item.value not in ("", "Not Applicable")
            for item in items
        )
        if not has_patterns:
            error_codes.append(8)

        has_model = any(item.element_id == 28 for item in items)
        if not has_model and not error_codes:
            error_codes.append(14)

        # Check for off-road
        off_road_body_ids = {"69", "84", "86", "88", "97", "105", "113", "124", "126", "127"}
        is_off_road = any(
            item.element_id == 5 and item.attribute_id in off_road_body_ids
            for item in items
        )
        if is_off_road:
            error_codes.append(10)

        # Check for incomplete vehicle
        if any(item.element_id == 5 and item.attribute_id == "64" for item in items):
            error_codes.append(9)

        if not error_codes or error_codes == [14]:
            error_codes.insert(0, 0)

        # Look up error messages (from cache)
        if error_codes:
            error_messages = [
                self._error_cache[c] for c in sorted(set(error_codes))
                if c in self._error_cache
            ]
            result.error_code = ",".join(str(c) for c in sorted(set(error_codes)))
            result.error_text = "; ".join(error_messages)

        # Build output: flatten items into variable→value dict, ordered by group
        self._build_output(items, result)
        return result

    def _decode_core(
        self, pass_num: int, model_year: int, vin: str,
        wmi_str: str, wmi_id: int, vin_keys: str, vehicle_type_id: int | None,
    ) -> list[DecItem]:
        items: list[DecItem] = []

        # Find VIN schemas for this WMI + year (cached by wmi+year)
        cache_key = (wmi_str, model_year)
        if cache_key not in self._schema_cache:
            self._schema_cache[cache_key] = self._conn.execute(
                "SELECT wvs.vinschemaid, wvs.wmiid, wvs.yearfrom "
                "FROM wmi_vinschema wvs "
                "INNER JOIN wmi w ON wvs.wmiid = w.id "
                "WHERE w.wmi = ? AND (? BETWEEN wvs.yearfrom AND COALESCE(wvs.yearto, 2999))",
                (wmi_str, model_year),
            ).fetchall()

        schemas = self._schema_cache[cache_key]
        schema_ids = [s["vinschemaid"] for s in schemas]
        if not schema_ids:
            return items

        schema_wmi_map = {s["vinschemaid"]: (s["wmiid"], s["yearfrom"]) for s in schemas}

        # Pattern matching — the core decode (patterns cached per schema)
        patterns = []
        for sid in schema_ids:
            if sid not in self._pattern_cache:
                self._pattern_cache[sid] = self._conn.execute(
                    "SELECT p.id, p.vinschemaid, p.keys, p.elementid, p.attributeid, "
                    "p.createdon, p.updatedon "
                    "FROM pattern p "
                    "INNER JOIN element e ON p.elementid = e.id "
                    "WHERE p.vinschemaid = ? "
                    "AND p.elementid NOT IN (26, 27, 29, 39) "
                    "AND e.decode IS NOT NULL "
                    "AND COALESCE(e.isprivate, 0) = 0 "
                    "ORDER BY p.id ASC",
                    (sid,),
                ).fetchall()
            patterns.extend(self._pattern_cache[sid])

        for p in patterns:
            if _keys_match(p["keys"], vin_keys):
                sw = schema_wmi_map.get(p["vinschemaid"], (wmi_id, 0))
                items.append(DecItem(
                    decoding_id=pass_num,
                    created_on=p["updatedon"] or p["createdon"],
                    pattern_id=p["id"],
                    keys=p["keys"].upper(),
                    vinschema_id=p["vinschemaid"],
                    wmi_id=sw[0],
                    element_id=p["elementid"],
                    attribute_id=p["attributeid"],
                    value="XXX",
                    source="Pattern",
                    priority=sw[1],
                ))

        # Engine model patterns
        engine_items = [i for i in items if i.element_id == 18]
        if engine_items:
            engine_items.sort(key=lambda x: (-x.priority, x.created_on or "", -1), reverse=False)
            best_engine = engine_items[-1]
            engine_name = best_engine.attribute_id

            eng_patterns = self._conn.execute(
                "SELECT emp.elementid, emp.attributeid, emp.createdon, emp.updatedon "
                "FROM enginemodelpattern emp "
                "INNER JOIN enginemodel em ON emp.enginemodelid = em.id "
                "INNER JOIN element e ON emp.elementid = e.id "
                "WHERE LOWER(TRIM(em.name)) = LOWER(TRIM(?))",
                (engine_name,),
            ).fetchall()

            for ep in eng_patterns:
                items.append(DecItem(
                    decoding_id=pass_num,
                    created_on=ep["updatedon"] or ep["createdon"],
                    pattern_id=best_engine.pattern_id,
                    keys=best_engine.keys,
                    vinschema_id=best_engine.vinschema_id,
                    wmi_id=wmi_id,
                    element_id=ep["elementid"],
                    attribute_id=ep["attributeid"],
                    value="XXX",
                    source="EngineModelPattern",
                    priority=50,
                ))

        # Add vehicle type
        vtype = self._conn.execute(
            "SELECT t.id, t.name FROM wmi w "
            "JOIN vehicletype t ON t.id = w.vehicletypeid "
            "WHERE w.wmi = ? AND (w.publicavailabilitydate IS NULL OR w.publicavailabilitydate <= datetime('now'))",
            (wmi_str,),
        ).fetchone()
        if vtype:
            items.append(DecItem(
                decoding_id=pass_num, created_on=None, pattern_id=None,
                keys=wmi_str.upper(), vinschema_id=None, wmi_id=wmi_id,
                element_id=39, attribute_id=str(vtype["id"]),
                value=vtype["name"].upper(), source="VehType", priority=100,
            ))

        # Add manufacturer
        mfr = self._conn.execute(
            "SELECT m.id, m.name FROM wmi w "
            "JOIN manufacturer m ON m.id = w.manufacturerid "
            "WHERE w.wmi = ?",
            (wmi_str,),
        ).fetchone()
        if mfr:
            items.append(DecItem(
                decoding_id=pass_num, created_on=None, pattern_id=None,
                keys=wmi_str.upper(), vinschema_id=None, wmi_id=wmi_id,
                element_id=27, attribute_id=str(mfr["id"]),
                value=mfr["name"].upper(), source="Manu. Name", priority=100,
            ))
            items.append(DecItem(
                decoding_id=pass_num, created_on=None, pattern_id=None,
                keys=wmi_str.upper(), vinschema_id=None, wmi_id=wmi_id,
                element_id=157, attribute_id=str(mfr["id"]),
                value=str(mfr["id"]), source="Manu. Id", priority=100,
            ))

        # Add model year
        items.append(DecItem(
            decoding_id=pass_num, created_on=None, pattern_id=None,
            keys="", vinschema_id=None, wmi_id=None,
            element_id=29, attribute_id=str(model_year),
            value=str(model_year), source="ModelYear", priority=100,
        ))

        # Formula patterns (numeric extraction)
        formula_keys = vin_keys
        for d in "0123456789":
            formula_keys = formula_keys.replace(d, "#")

        ph = ",".join("?" * len(schema_ids))
        formula_patterns = self._conn.execute(
            f"SELECT p.id, p.keys, p.vinschemaid, p.elementid, p.attributeid, "
            f"p.createdon, p.updatedon "
            f"FROM pattern p "
            f"INNER JOIN element e ON p.elementid = e.id "
            f"WHERE p.vinschemaid IN ({ph}) "
            f"AND p.elementid NOT IN (26, 27, 29, 39) "
            f"AND INSTR(p.keys, '#') > 0",
            schema_ids,
        ).fetchall()

        for fp in formula_patterns:
            fp_keys = fp["keys"]
            fp_formula_keys = fp_keys
            for d in "0123456789":
                fp_formula_keys = fp_formula_keys.replace(d, "#")

            if _keys_match(fp_formula_keys.replace("#", "*"), formula_keys.replace("#", "")):
                hash_start = fp_keys.index("#")
                hash_end = len(fp_keys) - fp_keys[::-1].index("#")
                extracted = vin_keys[hash_start:hash_end] if hash_end <= len(vin_keys) else ""

                items.append(DecItem(
                    decoding_id=pass_num,
                    created_on=fp["updatedon"] or fp["createdon"],
                    pattern_id=fp["id"],
                    keys=fp["keys"],
                    vinschema_id=fp["vinschemaid"],
                    wmi_id=None,
                    element_id=fp["elementid"],
                    attribute_id=fp["attributeid"],
                    value=extracted,
                    source="Formula Pattern",
                    priority=100,
                ))

        # Deduplicate: keep best per element (except multi-value elements)
        items = self._deduplicate(items, pass_num)

        # Resolve make from model
        model_item = next((i for i in items if i.element_id == 28), None)
        if model_item:
            make_from_model = self._conn.execute(
                "SELECT mk.id, mk.name FROM make_model mm "
                "INNER JOIN make mk ON mm.makeid = mk.id "
                "WHERE mm.modelid = ?",
                (model_item.attribute_id,),
            ).fetchone()
            if make_from_model:
                items.append(DecItem(
                    decoding_id=pass_num, created_on=None,
                    pattern_id=model_item.pattern_id,
                    keys=model_item.keys,
                    vinschema_id=model_item.vinschema_id,
                    wmi_id=None,
                    element_id=26, attribute_id=str(make_from_model["id"]),
                    value=make_from_model["name"].upper(),
                    source="pattern - model", priority=1000,
                ))
        else:
            # Try make from WMI
            wmi_makes = self._conn.execute(
                "SELECT mk.id, mk.name FROM wmi_make wm "
                "JOIN make mk ON mk.id = wm.makeid "
                "WHERE wm.wmiid = (SELECT id FROM wmi WHERE wmi = ?)",
                (wmi_str,),
            ).fetchall()
            if len(wmi_makes) == 1:
                mk = wmi_makes[0]
                items.append(DecItem(
                    decoding_id=pass_num, created_on=None, pattern_id=None,
                    keys=wmi_str, vinschema_id=None, wmi_id=wmi_id,
                    element_id=26, attribute_id=str(mk["id"]),
                    value=mk["name"].upper(), source="Make", priority=-100,
                ))

        # Conversion formulas
        items = self._apply_conversions(items, pass_num)

        # Vehicle spec patterns
        items = self._apply_vehicle_specs(
            items, pass_num, wmi_str, wmi_id, vehicle_type_id, model_year,
            model_item.attribute_id if model_item else None,
        )

        return items

    def _deduplicate(self, items: list[DecItem], pass_num: int) -> list[DecItem]:
        """Keep only the best item per element ID (except multi-value elements)."""
        by_element: dict[int, list[DecItem]] = {}
        for item in items:
            if item.decoding_id != pass_num:
                continue
            by_element.setdefault(item.element_id, []).append(item)

        result = []
        for elem_id, elem_items in by_element.items():
            if elem_id in MULTI_VALUE_ELEMENTS:
                result.extend(elem_items)
            else:
                elem_items.sort(key=lambda x: (
                    -x.priority,
                    x.created_on or "",
                    -len((x.keys or "").replace("*", "")),
                    (x.keys or "").replace("[", "").replace("]", ""),
                ))
                result.append(elem_items[0])
        return result

    def _apply_conversions(self, items: list[DecItem], pass_num: int) -> list[DecItem]:
        """Apply conversion formulas to derive values from existing decoded elements."""
        if self._conversion_cache is None:
            self._conversion_cache = self._conn.execute(
                "SELECT c.id, c.fromelementid, c.toelementid, c.formula, e.datatype "
                "FROM conversion c "
                "INNER JOIN element e ON c.toelementid = e.id"
            ).fetchall()
        conversions = self._conversion_cache

        existing_elements = {i.element_id for i in items}

        for conv in conversions:
            to_elem = conv["toelementid"]
            if to_elem in existing_elements:
                continue

            from_item = next(
                (i for i in items if i.element_id == conv["fromelementid"]),
                None,
            )
            if from_item is None:
                continue

            formula = conv["formula"].replace("#x#", from_item.attribute_id)
            try:
                raw = eval(formula)  # noqa: S307 — formulas are from trusted DB
                if isinstance(raw, float):
                    if raw == int(raw) and abs(raw) < 1e15:
                        val = f"{raw:.1f}"
                    else:
                        val = f"{raw:.14g}"
                else:
                    val = str(raw)
            except Exception:
                val = "0"

            items.append(DecItem(
                decoding_id=pass_num, created_on=None,
                pattern_id=from_item.pattern_id,
                keys=from_item.keys,
                vinschema_id=from_item.vinschema_id,
                wmi_id=from_item.wmi_id,
                element_id=to_elem,
                attribute_id=val, value=val,
                source=f"Conversion {conv['id']}", priority=100,
            ))
            existing_elements.add(to_elem)

        return items

    def _apply_vehicle_specs(
        self, items: list[DecItem], pass_num: int,
        wmi_str: str, wmi_id: int, vehicle_type_id: int | None,
        model_year: int, model_id: str | None,
    ) -> list[DecItem]:
        """Add vehicle spec pattern data for elements not yet decoded."""
        if not model_id or not vehicle_type_id:
            return items

        try:
            model_id_int = int(model_id)
        except (ValueError, TypeError):
            return items

        specs = self._conn.execute(
            "SELECT DISTINCT sp.id, s.tobeqced "
            "FROM vehiclespecschema s "
            "INNER JOIN vspecschemapattern sp ON s.id = sp.schemaid "
            "INNER JOIN vehiclespecpattern p ON sp.id = p.vspecschemapatternid "
            "INNER JOIN vehiclespecschema_model vssm ON vssm.vehiclespecschemaid = s.id "
            "LEFT JOIN vehiclespecschema_year vssy ON vssy.vehiclespecschemaid = s.id "
            "INNER JOIN wmi_make wm ON wm.makeid = s.makeid "
            "INNER JOIN wmi ON wmi.id = wm.wmiid "
            "WHERE wmi.wmi = ? AND s.vehicletypeid = ? "
            "AND vssm.modelid = ? "
            "AND (vssy.year = ? OR vssy.id IS NULL) "
            "AND p.iskey = 1 "
            "AND COALESCE(s.tobeqced, 0) = 0",
            (wmi_str, vehicle_type_id, model_id_int, model_year),
        ).fetchall()

        if not specs:
            return items

        spec_ids = [s["id"] for s in specs]

        # Check that all key patterns match
        valid_spec_ids = []
        for spec_id in spec_ids:
            key_patterns = self._conn.execute(
                "SELECT elementid, attributeid FROM vehiclespecpattern "
                "WHERE vspecschemapatternid = ? AND iskey = 1",
                (spec_id,),
            ).fetchall()

            all_match = True
            for kp in key_patterns:
                if not any(
                    i.element_id == kp["elementid"]
                    and i.attribute_id.lower() == kp["attributeid"].lower()
                    for i in items
                ):
                    all_match = False
                    break

            if all_match:
                valid_spec_ids.append(spec_id)

        if not valid_spec_ids:
            return items

        existing_elements = {
            i.element_id for i in items
            if i.element_id not in (1, *MULTI_VALUE_ELEMENTS)
        }

        placeholders = ",".join("?" * len(valid_spec_ids))
        spec_values = self._conn.execute(
            f"SELECT DISTINCT vsp.elementid, vsp.attributeid, "
            f"COALESCE(vsp.updatedon, vsp.createdon) as changedon "
            f"FROM vehiclespecpattern vsp "
            f"WHERE vsp.vspecschemapatternid IN ({placeholders}) "
            f"AND vsp.iskey = 0 "
            f"AND vsp.elementid NOT IN ({','.join(str(e) for e in existing_elements)})",
            valid_spec_ids,
        ).fetchall()

        # Deduplicate by element, keep latest
        by_elem: dict[int, tuple] = {}
        for sv in spec_values:
            eid = sv["elementid"]
            if eid not in by_elem or (sv["changedon"] or "") > (by_elem[eid][1] or ""):
                by_elem[eid] = (sv["attributeid"], sv["changedon"])

        for eid, (attr_id, changed_on) in by_elem.items():
            items.append(DecItem(
                decoding_id=pass_num, created_on=changed_on, pattern_id=None,
                keys="", vinschema_id=None, wmi_id=None,
                element_id=eid, attribute_id=attr_id,
                value="XXX", source="Vehicle Specs", priority=-100,
            ))

        return items

    def _pick_best_pass(
        self, all_items: dict[int, list[DecItem]], user_year: int | None,
    ) -> int:
        """Pick the decode pass with the best score."""
        scores = {}
        for pass_num, items in all_items.items():
            # Error value (lower is better for real errors)
            error_item = next((i for i in items if i.element_id == 143), None)
            error_val = 0
            if error_item:
                codes = error_item.value.split(",")
                error_val = sum(1 for c in codes if c.strip() and c.strip() != "0")

            # Element weight
            elem_weight = 0
            seen_elems = set()
            for item in items:
                if item.value and item.element_id not in seen_elems:
                    w = self._elements.get(item.element_id, {}).get("weight")
                    if w:
                        elem_weight += w
                        seen_elems.add(item.element_id)

            # Pattern count
            pattern_count = sum(
                1 for i in items
                if i.source in ("Pattern", "EngineModelPattern", "Formula Pattern")
                and i.value not in ("", "Not Applicable")
            )

            # Model year bonus
            my_item = next((i for i in items if i.element_id == 29), None)
            my_val = int(my_item.value) if my_item else 0
            my_bonus = 10000 if user_year and my_val == user_year else 0

            scores[pass_num] = (-error_val, elem_weight, pattern_count, my_val + my_bonus)

        return max(scores, key=lambda k: scores[k])

    def _build_output(self, items: list[DecItem], result: DecodeResult):
        """Build the final variable→value output dict, deterministically ordered."""
        output_pairs = []

        for item in items:
            elem = self._elements.get(item.element_id)
            if not elem or not elem.get("decode"):
                continue
            if elem.get("isprivate"):
                continue

            group = elem.get("groupname") or ""
            name = elem.get("name", f"Element_{item.element_id}")
            value = (item.value or "")
            value = value.replace("\\r\\n", " ").replace("\\r", " ").replace("\\n", " ")
            value = value.replace("\r\n", " ").replace("\r", " ").replace("\n", " ").replace("\t", " ")
            value = " ".join(value.split()).strip()

            group_order = GROUP_ORDER.get(group, 99)
            output_pairs.append((group_order, item.element_id, name, value))

        output_pairs.sort(key=lambda x: (x[0], x[1]))

        for _, elem_id, name, value in output_pairs:
            if name in result.results:
                existing = result.results[name]
                if value and value != existing:
                    result.results[name] = existing + "; " + value
            else:
                result.results[name] = value

    def decode_batch(self, vins: list[str], year: int | None = None) -> list[DecodeResult]:
        return [self.decode(vin, year) for vin in vins]
