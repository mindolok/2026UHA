"""Read rows from large XLSX files using Python's XML/ZIP standard library.

This avoids loading every workbook cell into RAM; values remain strings to
preserve identifiers such as 05108 and 02605.
"""
from __future__ import annotations
from pathlib import Path
import re
from typing import Iterator
import xml.etree.ElementTree as ET
import zipfile

MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    result: list[str] = []
    with archive.open("xl/sharedStrings.xml") as f:
        for _, el in ET.iterparse(f, events=("end",)):
            if el.tag == MAIN + "si":
                result.append("".join(c.text or "" for c in el.iter() if c.tag == MAIN + "t"))
                el.clear()
    return result


def iter_rows(path: str | Path, worksheet: str = "xl/worksheets/sheet1.xml") -> Iterator[dict[str, str]]:
    with zipfile.ZipFile(path) as z:
        strings = _shared_strings(z)
        with z.open(worksheet) as stream:
            for _, el in ET.iterparse(stream, events=("end",)):
                if el.tag != MAIN + "row":
                    continue
                row: dict[str, str] = {}
                for cell in el.findall(MAIN + "c"):
                    col = re.match(r"[A-Z]+", cell.attrib.get("r", ""))
                    if not col:
                        continue
                    v = cell.find(MAIN + "v")
                    inline = cell.find(MAIN + "is")
                    raw = v.text if v is not None and v.text is not None else ""
                    if inline is not None:
                        raw = "".join(c.text or "" for c in inline.iter() if c.tag == MAIN + "t")
                    if cell.attrib.get("t") == "s" and raw:
                        raw = strings[int(raw)]
                    row[col.group(0)] = raw
                el.clear()
                yield row


def iter_dicts(path: str | Path) -> Iterator[dict[str, str]]:
    rows = iter_rows(path)
    header = next(rows)
    for row in rows:
        yield {column_name: row.get(column, "") for column, column_name in header.items()}
