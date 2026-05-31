# NHTSA VIN Decoder V2

A standalone, offline VIN decoder using NHTSA's [vPIC database](https://vpic.nhtsa.dot.gov/). Decodes Vehicle Identification Numbers into make, model, year, electrification level, body style, and 30+ other vehicle attributes — no API calls, no SQL Server, no internet required after setup.

Built for batch research on large datasets (100K+ VINs). Runs at ~160 VINs/sec on a single core.

## Why this exists

The NHTSA vPIC API is rate-limited and impractical for large-scale research. The [official standalone database](https://vpic.nhtsa.dot.gov/Downloads) requires SQL Server and returns results in a nondeterministic column order, making batch processing difficult. This tool solves both problems: it converts the database to portable SQLite and provides a Python decode engine with deterministic, CSV-friendly output.

The [original version](https://github.com/ssrpw2/NHTSA-VIN-Decoder) of this decoder was built in 2022 for crash testing research at the [Midwest Roadside Safety Facility (MwRSF)](https://mwrsf.unl.edu/). It was used to identify battery electric vehicles (BEVs) in state DOT crash data — records that contain VINs but don't indicate whether a vehicle is electric. The decoder made it possible to extract electrification level, fuel type, and other attributes at scale, enabling analysis of BEV involvement in real-world crashes. That work contributed to a [published paper on BEV crash compatibility](https://doi.org/10.1177/03611981231188584) (Transportation Research Record, 2023).

## Quick start

### Requirements

- Python 3.10+
- ~400 MB disk space for the SQLite database

### Setup

1. Download the latest vPIC database dump from [NHTSA](https://vpic.nhtsa.dot.gov/Downloads) (choose "PostgreSQL plain-text" format).

2. Convert to SQLite:
```bash
python convert_db.py path/to/vPICList_lite_YYYY_MM.sql
```
This produces `vpic.db` (~395 MB, ~10.9M rows). Takes about 2 minutes.

3. Decode VINs:
```bash
# Single VIN
python decode.py input.csv output.csv --vin 5YJSA1DG9DFP14705

# Batch from file (one VIN per line, or CSV with VINs in first column)
python decode.py vins.csv results.csv
```

## Output format

CSV with deterministic column order: `VIN, Error Code, Error Text`, then all decoded fields alphabetically. Key fields include:

| Field | Example |
|-------|---------|
| Make | TESLA |
| Model | Model S |
| Model Year | 2013 |
| Body Class | Hatchback/Liftback/Notchback |
| Electrification Level | BEV (Battery Electric Vehicle) |
| Fuel Type - Primary | Electric |
| Vehicle Type | PASSENGER CAR |
| Drive Type | RWD/Rear-Wheel Drive |
| Plant City | FREMONT |
| Displacement (CC) | 2000.0 |

## Accuracy

Verified against the [NHTSA vPIC online API](https://vpic.nhtsa.dot.gov/api/) across 25 test VINs covering 15+ manufacturers:

- **98.7%** overall field match rate (1052/1066 fields)
- **100%** accuracy on research-critical fields (Make, Model, Year, Electrification Level, Fuel Type, Vehicle Type, Body Class)
- Remaining mismatches are floating-point precision in displacement unit conversions (last-digit rounding between Python and SQL Server arithmetic)

## How it works

The decoder is a Python port of NHTSA's `spvindecode` stored procedure pipeline:

1. **WMI lookup** — positions 1–3 identify the manufacturer
2. **Model year** — position 10 maps to year (with 30-year cycle disambiguation)
3. **Pattern matching** — positions 4–8 and 10–17 match against manufacturer-specific decode patterns
4. **Multi-pass resolution** — multiple schema/year combinations scored by error count, element weight, and pattern count
5. **Lookup resolution** — attribute IDs resolved to human-readable values through 80+ lookup tables
6. **Engine model patterns** — engine-specific attributes (displacement, cylinders, fuel type)
7. **Conversion formulas** — derived values (CC → CI, CC → L)
8. **Vehicle spec patterns** — additional attributes from manufacturer vehicle specifications
9. **Check digit validation** — position 9 verified per NHTSA rules

## Files

| File | Purpose |
|------|---------|
| `convert_db.py` | PostgreSQL dump → SQLite converter |
| `decode.py` | CLI batch decoder |
| `verify_against_api.py` | Accuracy verification against NHTSA online API |
| `vin_decoder/decoder.py` | Core decode engine |
| `vin_decoder/lookups.py` | Lookup table resolver |
| `vin_decoder/check_digit.py` | VIN check digit validation |

## Comparison with V1

| | V1 (2022) | V2 |
|---|-----------|-----|
| Database | SQL Server `.bak` | SQLite (portable) |
| Interface | SQL Server Management Studio + Excel | Python CLI |
| Batch size | 10,000 VINs at a time | Unlimited |
| Speed | ~1,000 VINs/min | ~9,600 VINs/min |
| Output | Pipe-delimited, 6 columns | CSV, 30+ columns |
| Dependencies | SQL Server, Notepad++, Excel | Python 3.10+ |
| Database vintage | 2022 (frozen) | Any vPIC release |

## License

MIT
