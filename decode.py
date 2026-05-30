#!/usr/bin/env python3
"""Decode VINs from a CSV/text file and output results as CSV.

Usage:
    python decode.py input.csv output.csv [--db vpic.db] [--year YYYY]

Input: one VIN per line, or a CSV where the first column contains VINs.
Output: CSV with one row per VIN, deterministic column order.
"""

import argparse
import csv
import sys
import time
from pathlib import Path

from vin_decoder import VinDecoder


def read_vins(path: str) -> list[str]:
    vins = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        sample = f.read(4096)
        f.seek(0)

        if "," in sample or "\t" in sample:
            dialect = csv.Sniffer().sniff(sample)
            reader = csv.reader(f, dialect)
            for row in reader:
                if row:
                    vin = row[0].strip().upper()
                    if len(vin) == 17 and vin.isalnum():
                        vins.append(vin)
        else:
            for line in f:
                vin = line.strip().upper()
                if len(vin) == 17 and vin.isalnum():
                    vins.append(vin)
    return vins


def main():
    parser = argparse.ArgumentParser(description="Batch VIN decoder using NHTSA vPIC database")
    parser.add_argument("input", help="Input file: one VIN per line or CSV with VINs in first column")
    parser.add_argument("output", help="Output CSV file path")
    parser.add_argument("--db", default="vpic.db", help="Path to SQLite database (default: vpic.db)")
    parser.add_argument("--year", type=int, default=None, help="Override model year for all VINs")
    parser.add_argument("--vin", help="Decode a single VIN (ignores input file)")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Error: Database not found at {db_path}", file=sys.stderr)
        print("Run convert_db.py first to create the SQLite database.", file=sys.stderr)
        sys.exit(1)

    if args.vin:
        vins = [args.vin.upper().strip()]
    else:
        input_path = Path(args.input)
        if not input_path.exists():
            print(f"Error: Input file not found: {input_path}", file=sys.stderr)
            sys.exit(1)
        vins = read_vins(str(input_path))

    if not vins:
        print("No valid VINs found in input.", file=sys.stderr)
        sys.exit(1)

    print(f"Decoding {len(vins):,} VINs...", file=sys.stderr)
    t0 = time.time()

    with VinDecoder(db_path) as decoder:
        # Collect all column names first
        all_columns = set()
        results = []
        batch_size = 1000
        decoded = 0

        for i in range(0, len(vins), batch_size):
            batch = vins[i : i + batch_size]
            batch_results = decoder.decode_batch(batch, args.year)
            for r in batch_results:
                all_columns.update(r.results.keys())
            results.extend(batch_results)
            decoded += len(batch)

            if decoded % 10000 == 0 or decoded == len(vins):
                elapsed = time.time() - t0
                rate = decoded / elapsed if elapsed > 0 else 0
                print(
                    f"  {decoded:,}/{len(vins):,} ({rate:.0f} VINs/sec)",
                    file=sys.stderr,
                )

    elapsed = time.time() - t0
    rate = len(vins) / elapsed if elapsed > 0 else 0

    # Fixed column order: VIN first, then Error, then alphabetical
    sorted_columns = sorted(all_columns)
    header = ["VIN", "Error Code", "Error Text"] + sorted_columns

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for r in results:
            row = [r.vin, r.error_code, r.error_text]
            row.extend(r.results.get(col, "") for col in sorted_columns)
            writer.writerow(row)

    print(f"\nDone. {len(vins):,} VINs decoded in {elapsed:.1f}s ({rate:.0f} VINs/sec)", file=sys.stderr)
    print(f"Output: {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
