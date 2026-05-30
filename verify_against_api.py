#!/usr/bin/env python3
"""Compare our decoder output against the NHTSA vPIC online API.

Usage:
    python verify_against_api.py [--db vpic.db] [--count N]
"""

import argparse
import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

from vin_decoder import VinDecoder

API_URL = "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVin/{}?format=json"

# Fields the API returns that we don't decode (metadata, internal, or not in our scope)
SKIP_FIELDS = {
    "Suggested VIN", "Possible Values", "Additional Error Text",
    "Vehicle Descriptor", "Vehicle ID", "Error Code", "Error Text",
    "Manufacturer Id", "NCSA Body Type", "NCSA Make", "NCSA Model",
    "NCSA Note", "NCSA Mapping Exception", "CAFE Body Type", "CAFE Make",
    "CAFE Model", "NCIC Code", "Note", "Active Safety System Note",
}

# Fields where minor formatting differences are expected
NORMALIZE_FIELDS = True


def fetch_api(vin: str) -> dict[str, str]:
    """Decode a VIN using the NHTSA online API."""
    url = API_URL.format(vin)
    try:
        resp = urllib.request.urlopen(url, timeout=30)
        data = json.loads(resp.read())
    except Exception as e:
        print(f"  API error for {vin}: {e}", file=sys.stderr)
        return {}

    result = {}
    for item in data.get("Results", []):
        var = item.get("Variable", "")
        val = item.get("Value")
        if var and val is not None and str(val).strip():
            result[var] = str(val).strip()
    return result


def normalize(value: str) -> str:
    """Normalize a value for comparison."""
    v = value.strip().upper()
    v = v.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    v = " ".join(v.split())
    return v


def get_test_vins(db_path: str, count: int) -> list[str]:
    """Generate test VINs from known patterns in the database."""
    # Use a mix of hand-picked VINs covering different manufacturers and vehicle types
    known_vins = [
        "5YJSA1DG9DFP14705",  # Tesla Model S 2013
        "2T1BURHE0JC034461",  # Toyota Corolla 2018
        "1G1YY22G965104237",  # Chevrolet Corvette 2006
        "1FA6P8TH8L5100001",  # Ford Mustang 2020
        "1FTFW1ET5DFC10312",  # Ford F-150 2013
        "3GNAXUEV0NL123456",  # Chevrolet Equinox 2022
        "WBA3A5C51CF256789",  # BMW 3 Series 2012
        "WDDSJ4EB3EN012345",  # Mercedes CLA 2014
        "1N4AL3AP8JC123456",  # Nissan Altima 2018
        "2HGFC2F59KH567890",  # Honda Civic 2019
        "JTDKN3DU5A0123456",  # Toyota Prius 2010
        "1VWAT7A33FC012345",  # Volkswagen Passat 2015
        "5YJ3E1EA1JF012345",  # Tesla Model 3 2018
        "JM1BK32F781234567",  # Mazda3 2008
        "19UDE2F38KA012345",  # Acura ILX 2019
        "3C4PDCAB5ET123456",  # Dodge Journey 2014
        "KMHD35LH5EU123456",  # Hyundai Elantra 2014
        "KNAGM4AD7G5123456",  # Kia Optima 2016
        "4T1BF1FK5CU123456",  # Toyota Camry 2012
        "1GCGG25K071234567",  # Chevrolet Express 2007
        "5FNRL6H70NB012345",  # Honda Pilot 2022
        "1C4RJFBG0LC123456",  # Jeep Grand Cherokee 2020
        "3VW2B7AJ6DM123456",  # VW Jetta 2013
        "YV4A22PK8N1234567",  # Volvo XC60 2022
        "SALGS2RE0LA123456",  # Range Rover 2020
    ]
    return known_vins[:count]


def compare(vin: str, decoder: VinDecoder) -> dict:
    """Compare our decode vs API for a single VIN."""
    # Our decode
    local = decoder.decode(vin)

    # API decode
    time.sleep(0.5)  # Rate limit
    api = fetch_api(vin)

    if not api:
        return {"vin": vin, "status": "API_ERROR", "matches": 0, "mismatches": 0, "details": []}

    matches = 0
    mismatches = 0
    local_only = 0
    api_only = 0
    details = []

    # Compare fields present in both
    all_fields = set(local.results.keys()) | set(api.keys())

    for field in sorted(all_fields):
        if field in SKIP_FIELDS:
            continue

        local_val = local.results.get(field, "")
        api_val = api.get(field, "")

        if not local_val and not api_val:
            continue

        local_norm = normalize(local_val) if local_val else ""
        api_norm = normalize(api_val) if api_val else ""

        if local_norm == api_norm:
            matches += 1
        elif not local_val and api_val:
            api_only += 1
            details.append(f"  API only — {field}: {api_val}")
        elif local_val and not api_val:
            local_only += 1
            details.append(f"  Local only — {field}: {local_val}")
        else:
            mismatches += 1
            details.append(f"  MISMATCH — {field}:")
            details.append(f"    Local: {local_val}")
            details.append(f"    API:   {api_val}")

    status = "MATCH" if mismatches == 0 else "MISMATCH"
    return {
        "vin": vin,
        "status": status,
        "matches": matches,
        "mismatches": mismatches,
        "local_only": local_only,
        "api_only": api_only,
        "details": details,
    }


def main():
    parser = argparse.ArgumentParser(description="Verify decoder against NHTSA API")
    parser.add_argument("--db", default="vpic.db", help="Path to SQLite database")
    parser.add_argument("--count", type=int, default=25, help="Number of VINs to test")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show all field comparisons")
    args = parser.parse_args()

    vins = get_test_vins(args.db, args.count)
    print(f"Testing {len(vins)} VINs against NHTSA API...\n")

    decoder = VinDecoder(args.db)
    total_matches = 0
    total_mismatches = 0
    total_api_only = 0
    total_local_only = 0
    results = []

    for i, vin in enumerate(vins):
        result = compare(vin, decoder)
        results.append(result)

        symbol = "✓" if result["status"] == "MATCH" else "✗" if result["status"] == "MISMATCH" else "?"
        print(f"[{i+1}/{len(vins)}] {symbol} {vin} — {result['matches']} match, {result['mismatches']} mismatch, {result['api_only']} API-only, {result['local_only']} local-only")

        if result["details"] and (args.verbose or result["mismatches"] > 0):
            for line in result["details"]:
                print(line)
            print()

        total_matches += result["matches"]
        total_mismatches += result["mismatches"]
        total_api_only += result["api_only"]
        total_local_only += result["local_only"]

    decoder.close()

    print(f"\n{'='*60}")
    print(f"SUMMARY: {len(vins)} VINs tested")
    print(f"  Fields matched:    {total_matches}")
    print(f"  Fields mismatched: {total_mismatches}")
    print(f"  API-only fields:   {total_api_only}")
    print(f"  Local-only fields: {total_local_only}")

    match_rate = total_matches / (total_matches + total_mismatches) * 100 if (total_matches + total_mismatches) > 0 else 0
    print(f"  Match rate:        {match_rate:.1f}%")

    perfect = sum(1 for r in results if r["status"] == "MATCH")
    print(f"  Perfect VINs:      {perfect}/{len(vins)}")


if __name__ == "__main__":
    main()
