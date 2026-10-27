"""Search Query Details pages: file names per keyword."""
from __future__ import annotations

import json

from app.competitors import page_file


def test_keywords_differing_only_in_punctuation_get_their_own_file(tmp_path):
    first = page_file(tmp_path, "B0TESTAAAA", "car sun-shade")
    first.write_text(json.dumps({"search_query": "car sun-shade"}), encoding="utf-8")
    second = page_file(tmp_path, "B0TESTAAAA", "car sun shade")
    assert second != first and second.name.startswith(first.stem + "_")
    assert page_file(tmp_path, "B0TESTAAAA", "car sun-shade") == first  # the saved one still finds itself
