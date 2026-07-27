"""ISO 639-3 code table for validating etymology language fields.

`data/iso639_3.json` is generated from the npm `iso-639-3` package (the
same table the web editor's help dialog shows), with the four special
placeholder codes (mis/mul/und/zxx) dropped — a blank field already
means "unknown". Regenerate from the `iso-639-3` package if the standard
gains codes.
"""

import json
from functools import lru_cache
from pathlib import Path

_DATA_PATH = Path(__file__).parent / 'data' / 'iso639_3.json'


@lru_cache(maxsize=1)
def _tables():
    with open(_DATA_PATH, encoding='utf-8') as f:
        data = json.load(f)
    return data['codes'], data['iso1']


def normalize_code(code):
    """Return the canonical ISO 639-3 code (lowercased; 639-1 two-letter
    aliases mapped, e.g. fr -> fra), or None if the code is unknown."""
    codes, iso1 = _tables()
    code = code.strip().lower()
    code = iso1.get(code, code)
    return code if code in codes else None
