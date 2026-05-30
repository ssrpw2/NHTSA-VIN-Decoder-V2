#!/usr/bin/env python3
"""Convert NHTSA vPIC PostgreSQL plain-text dump to SQLite database."""

import re
import sqlite3
import sys
import time
from pathlib import Path


PG_TYPE_MAP = {
    "integer": "INTEGER",
    "int": "INTEGER",
    "smallint": "INTEGER",
    "bigint": "INTEGER",
    "serial": "INTEGER",
    "boolean": "INTEGER",
    "real": "REAL",
    "double precision": "REAL",
    "numeric": "REAL",
    "character varying": "TEXT",
    "character": "TEXT",
    "varchar": "TEXT",
    "char": "TEXT",
    "text": "TEXT",
    "timestamp without time zone": "TEXT",
    "timestamp": "TEXT",
}

# Tables the decoder needs. Skip the rest to keep the DB small.
REQUIRED_TABLES = {
    "abs", "adaptivecruisecontrol", "adaptivedrivingbeam", "airbaglocations",
    "airbaglocfront", "airbaglocknee", "autobrake", "automaticpedestrainalertingsound",
    "autoreversesystem", "axleconfiguration", "batterytype", "bedtype",
    "blindspotintervention", "blindspotmonitoring", "bodycab", "bodystyle",
    "brakesystem", "busfloorconfigtype", "bustype", "can_aacn", "chargerlevel",
    "combinedbrakingsystem", "conversion", "coolingtype", "country",
    "custommotorcycletype", "daytimerunninglight", "defaultvalue",
    "destinationmarket", "drivetype", "dynamicbrakesupport", "ecs", "edr",
    "electrificationlevel", "element", "engineconfiguration", "enginemodel",
    "enginemodelpattern", "entertainmentsystem", "errorcode", "evdriveunit",
    "forwardcollisionwarning", "fueldeliverytype", "fueltankmaterial",
    "fueltanktype", "fueltype", "grossvehicleweightrating", "keylessignition",
    "lanecenteringassistance", "lanedeparturewarning", "lanekeepsystem",
    "lowerbeamheadlamplightsource", "make", "make_model", "manufacturer",
    "manufacturer_make", "model", "motorcyclechassistype",
    "motorcyclesuspensiontype", "nonlanduse", "parkassist", "pattern",
    "pedestrianautomaticemergencybraking", "pretensioner",
    "rearautomaticemergencybraking", "rearcrosstrafficalert",
    "rearvisibilitycamera", "seatbeltsall", "semiautomaticheadlampbeamswitching",
    "steering", "tpms", "tractioncontrol", "trailerbodytype", "trailertype",
    "transmission", "turbo", "valvetraindesign", "vehiclespecpattern",
    "vehiclespecschema", "vehiclespecschema_model", "vehiclespecschema_year",
    "vehicletype", "vindescriptor", "vinexception", "vinschema",
    "vspecschemapattern", "wheelbasetype", "wheeliemitigation", "wmi",
    "wmi_make", "wmi_vinschema", "wmiyearvalidchars",
    "wmiyearvalidchars_cacheexceptions",
}

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_pattern_vinschemaid ON pattern(vinschemaid)",
    "CREATE INDEX IF NOT EXISTS idx_pattern_elementid ON pattern(elementid)",
    "CREATE INDEX IF NOT EXISTS idx_pattern_keys ON pattern(keys)",
    "CREATE INDEX IF NOT EXISTS idx_wmi_wmi ON wmi(wmi)",
    "CREATE INDEX IF NOT EXISTS idx_wmi_vinschema_wmiid ON wmi_vinschema(wmiid)",
    "CREATE INDEX IF NOT EXISTS idx_wmi_vinschema_vinschemaid ON wmi_vinschema(vinschemaid)",
    "CREATE INDEX IF NOT EXISTS idx_wmi_make_wmiid ON wmi_make(wmiid)",
    "CREATE INDEX IF NOT EXISTS idx_element_id ON element(id)",
    "CREATE INDEX IF NOT EXISTS idx_vinschema_id ON vinschema(id)",
    "CREATE INDEX IF NOT EXISTS idx_vindescriptor_descriptor ON vindescriptor(descriptor)",
    "CREATE INDEX IF NOT EXISTS idx_make_model_modelid ON make_model(modelid)",
    "CREATE INDEX IF NOT EXISTS idx_make_model_makeid ON make_model(makeid)",
    "CREATE INDEX IF NOT EXISTS idx_enginemodel_name ON enginemodel(name)",
    "CREATE INDEX IF NOT EXISTS idx_enginemodelpattern_enginemodelid ON enginemodelpattern(enginemodelid)",
    "CREATE INDEX IF NOT EXISTS idx_vehiclespecpattern_vspecschemapatternid ON vehiclespecpattern(vspecschemapatternid)",
    "CREATE INDEX IF NOT EXISTS idx_vehiclespecpattern_elementid ON vehiclespecpattern(elementid)",
    "CREATE INDEX IF NOT EXISTS idx_vehiclespecschema_makeid ON vehiclespecschema(makeid)",
    "CREATE INDEX IF NOT EXISTS idx_vehiclespecschema_model_schemaid ON vehiclespecschema_model(vehiclespecschemaid)",
    "CREATE INDEX IF NOT EXISTS idx_vehiclespecschema_year_schemaid ON vehiclespecschema_year(vehiclespecschemaid)",
    "CREATE INDEX IF NOT EXISTS idx_vspecschemapattern_schemaid ON vspecschemapattern(schemaid)",
    "CREATE INDEX IF NOT EXISTS idx_vinexception_vin ON vinexception(vin)",
    "CREATE INDEX IF NOT EXISTS idx_defaultvalue_vehicletypeid ON defaultvalue(vehicletypeid)",
    "CREATE INDEX IF NOT EXISTS idx_conversion_fromelementid ON conversion(fromelementid)",
]


def parse_pg_type(type_str):
    type_str = type_str.strip().lower()
    type_str = re.sub(r"default\s+.*", "", type_str).strip()
    type_str = re.sub(r"\s+not\s+null", "", type_str).strip()
    type_str = type_str.rstrip(",").strip()
    for pg_type, sqlite_type in PG_TYPE_MAP.items():
        if type_str.startswith(pg_type):
            return sqlite_type
    return "TEXT"


def parse_create_table(lines, start_idx):
    match = re.match(r"CREATE TABLE vpic\.(\w+)\s*\(", lines[start_idx])
    if not match:
        return None, None, start_idx + 1
    table_name = match.group(1).lower()
    columns = []
    i = start_idx + 1
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith(")"):
            break
        if line.startswith("--") or not line:
            i += 1
            continue
        # Skip generated columns
        if "GENERATED ALWAYS AS" in line.upper():
            i += 1
            continue
        col_match = re.match(r'"?(\w+)"?\s+(.+)', line)
        if col_match:
            col_name = col_match.group(1).lower()
            col_type = parse_pg_type(col_match.group(2))
            columns.append((col_name, col_type))
        i += 1
    return table_name, columns, i + 1


def parse_copy_data(lines, start_idx):
    match = re.match(r"COPY vpic\.(\w+)\s*\(([^)]+)\)", lines[start_idx])
    if not match:
        return None, None, [], start_idx + 1
    table_name = match.group(1).lower()
    col_names = [c.strip().strip('"').lower() for c in match.group(2).split(",")]
    rows = []
    i = start_idx + 1
    while i < len(lines):
        line = lines[i]
        if line.strip() == "\\.":
            break
        values = line.rstrip("\n").split("\t")
        converted = []
        for v in values:
            if v == "\\N":
                converted.append(None)
            elif v == "t":
                converted.append(1)
            elif v == "f":
                converted.append(0)
            else:
                converted.append(v)
        rows.append(converted)
        i += 1
    return table_name, col_names, rows, i + 1


def convert(sql_path, db_path):
    sql_path = Path(sql_path)
    db_path = Path(db_path)

    if db_path.exists():
        db_path.unlink()

    print(f"Reading {sql_path.name}...")
    t0 = time.time()
    lines = sql_path.read_text(encoding="utf-8", errors="replace").split("\n")
    print(f"  {len(lines):,} lines read in {time.time() - t0:.1f}s")

    # First pass: collect CREATE TABLE schemas
    schemas = {}
    i = 0
    while i < len(lines):
        if lines[i].startswith("CREATE TABLE vpic."):
            table_name, columns, next_i = parse_create_table(lines, i)
            if table_name and table_name in REQUIRED_TABLES:
                schemas[table_name] = columns
            i = next_i
        else:
            i += 1
    print(f"  Found {len(schemas)} table schemas")

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA cache_size=-200000")  # 200MB cache
    cur = conn.cursor()

    # Create tables
    for table_name, columns in schemas.items():
        col_defs = ", ".join(f"{name} {dtype}" for name, dtype in columns)
        cur.execute(f"CREATE TABLE IF NOT EXISTS {table_name} ({col_defs})")
    conn.commit()

    # Second pass: load COPY data
    print("Loading data...")
    total_rows = 0
    i = 0
    while i < len(lines):
        if lines[i].startswith("COPY vpic."):
            table_name, col_names, rows, next_i = parse_copy_data(lines, i)
            if table_name and table_name in REQUIRED_TABLES and rows:
                # Filter columns to only those in our schema (skip generated cols)
                schema_cols = {c[0] for c in schemas.get(table_name, [])}
                if schema_cols:
                    keep_idxs = [j for j, c in enumerate(col_names) if c in schema_cols]
                    filtered_cols = [col_names[j] for j in keep_idxs]
                    placeholders = ", ".join("?" * len(filtered_cols))
                    insert_sql = f"INSERT INTO {table_name} ({', '.join(filtered_cols)}) VALUES ({placeholders})"

                    filtered_rows = []
                    for row in rows:
                        filtered_rows.append(tuple(row[j] if j < len(row) else None for j in keep_idxs))

                    cur.executemany(insert_sql, filtered_rows)
                    total_rows += len(filtered_rows)
                    print(f"  {table_name}: {len(filtered_rows):,} rows")
            i = next_i
        else:
            i += 1
    conn.commit()

    # Create indexes
    print("Creating indexes...")
    for idx_sql in INDEXES:
        cur.execute(idx_sql)
    conn.commit()

    # Analyze for query planner
    cur.execute("ANALYZE")
    conn.commit()
    conn.close()

    db_size = db_path.stat().st_size / (1024 * 1024)
    print(f"\nDone. {total_rows:,} total rows → {db_path.name} ({db_size:.1f} MB)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python convert_db.py <path/to/vPICList_lite_YYYY_MM.sql> [output.db]")
        sys.exit(1)

    sql_file = sys.argv[1]
    db_file = sys.argv[2] if len(sys.argv) > 2 else "vpic.db"
    convert(sql_file, db_file)
