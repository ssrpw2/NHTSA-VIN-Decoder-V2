"""VIN check digit validation (position 9)."""

TRANSLITERATION = {
    "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8,
    "J": 1, "K": 2, "L": 3, "M": 4, "N": 5, "P": 7, "R": 9,
    "S": 2, "T": 3, "U": 4, "V": 5, "W": 6, "X": 7, "Y": 8, "Z": 9,
}

WEIGHTS = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]

# For cars/MPV/light trucks, positions 13-17 must be numeric
# For other vehicles, positions 14-17 must be numeric
VALID_VIN_CHARS = set("0123456789ABCDEFGHJKLMNPRSTUVWXYZ")
VALID_CHECK_CHARS = set("0123456789X")
VALID_YEAR_CHARS = set("123456789ABCDEFGHJKLMNPRSTVWXY")


def compute_check_digit(vin: str, is_car_mpv_lt: bool = False) -> str:
    total = 0
    for i, ch in enumerate(vin.upper()):
        if ch.isdigit():
            val = int(ch)
        else:
            val = TRANSLITERATION.get(ch, 0)
        total += val * WEIGHTS[i]
    remainder = total % 11
    return "X" if remainder == 10 else str(remainder)


def validate_vin_chars(vin: str, is_car_mpv_lt: bool = False) -> list[tuple[int, str]]:
    """Return list of (position, char) for invalid characters."""
    invalid = []
    start_numeric = 13 if is_car_mpv_lt else 14
    if vin[2] == "9":
        start_numeric = 15

    for j in range(len(vin)):
        pos = j + 1
        ch = vin[j]
        if pos == 9:
            if ch not in VALID_CHECK_CHARS:
                invalid.append((pos, ch))
        elif pos == 10:
            if ch not in VALID_YEAR_CHARS:
                invalid.append((pos, ch))
        elif pos < start_numeric:
            if ch not in VALID_VIN_CHARS:
                invalid.append((pos, ch))
        else:
            if not ch.isdigit() and ch != "*":
                invalid.append((pos, ch))
    return invalid
