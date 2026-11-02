"""Minimal ISO 10303-21 (STEP / .ifc) helpers: value encoding and record surgery.

Used to add georeferencing to a file by copying it byte for byte and only
inserting or replacing a handful of records, instead of re-serialising every
entity (seconds instead of minutes for large models).
"""
import math
import re

_SPECIAL = re.compile(rb"[';/]")


def string(value):
    """Encode a Python string as a STEP string literal (with \\X2\\ escapes)."""
    out, pending = [], []

    def flush():
        if not pending:
            return
        if any(ord(ch) > 0xFFFF for ch in pending):
            out.append("\\X4\\" + "".join(f"{ord(ch):08X}" for ch in pending) + "\\X0\\")
        else:
            out.append("\\X2\\" + "".join(f"{ord(ch):04X}" for ch in pending) + "\\X0\\")
        pending.clear()

    for ch in str(value):
        code = ord(ch)
        if 32 <= code <= 126:
            flush()
            out.append("''" if ch == "'" else "\\\\" if ch == "\\" else ch)
        else:
            pending.append(ch)
    flush()
    return "'" + "".join(out) + "'"


def optional_string(value, max_len=None):
    if value is None or value == "":
        return "$"
    value = str(value)
    if max_len:
        value = value[:max_len]
    return string(value)


def real(value):
    """Encode a float as a STEP REAL (always with a decimal point)."""
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"Cannot write a non-finite number ({value}) to IFC.")
    text = repr(value).upper()
    if "E" in text:
        mantissa, exponent = text.split("E")
        if "." not in mantissa:
            mantissa += "."
        return f"{mantissa}E{exponent}"
    return text if "." in text else text + "."


def ref(entity_id):
    return f"#{int(entity_id)}"


def record(entity_id, entity_type, args):
    return f"#{int(entity_id)}={entity_type.upper()}({','.join(args)});"


def _record_end(data, pos):
    """Index just after the ';' that terminates the record starting before pos."""
    while True:
        m = _SPECIAL.search(data, pos)
        if m is None:
            raise ValueError("Unterminated record in IFC file.")
        i = m.start()
        ch = data[i:i + 1]
        if ch == b";":
            return i + 1
        if ch == b"'":
            j = i + 1
            while True:
                k = data.find(b"'", j)
                if k == -1:
                    raise ValueError("Unterminated string in IFC file.")
                if data[k + 1:k + 2] == b"'":
                    j = k + 2
                    continue
                pos = k + 1
                break
        elif data[i + 1:i + 2] == b"*":
            k = data.find(b"*/", i + 2)
            if k == -1:
                raise ValueError("Unterminated comment in IFC file.")
            pos = k + 2
        else:
            pos = i + 1


def record_span(data, entity_id):
    """(start, end) byte offsets of record #entity_id, or None if absent."""
    pattern = re.compile(rb"(?m)(?:^|(?<=;))[ \t]*#" + str(int(entity_id)).encode() + rb"[ \t]*=")
    m = pattern.search(data)
    if m is None:
        return None
    start = data.index(b"#", m.start())
    return start, _record_end(data, m.end())


def data_section_end(data):
    """Offset where new records can be inserted (start of the DATA section's ENDSEC line)."""
    end_iso = data.rfind(b"END-ISO-10303-21")
    if end_iso == -1:
        raise ValueError("The file is incomplete: END-ISO-10303-21 is missing.")
    endsec = data.rfind(b"ENDSEC", 0, end_iso)
    if endsec == -1 or data.rfind(b"DATA", 0, endsec) == -1:
        raise ValueError("The file has no DATA section.")
    line_start = data.rfind(b"\n", 0, endsec) + 1
    if data[line_start:endsec].strip():
        line_start = endsec
    return line_start


def newline(data):
    return b"\r\n" if b"\r\n" in data[:4096] else b"\n"
