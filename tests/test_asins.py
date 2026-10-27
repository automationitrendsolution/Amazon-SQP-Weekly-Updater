"""config/asins.txt: the explicit ASIN set, deduplicated, names kept for reference."""
from __future__ import annotations

import pytest

from app.asins import load_asin_list
from app.config import PROJECT_ROOT
from app.exceptions import ConfigurationError
from app.spapi import ASIN_OPTION_MAX_CHARS, batch_asins


def test_the_project_list_has_60_unique_asins():
    asin_list = load_asin_list(PROJECT_ROOT / "config" / "asins.txt")
    assert asin_list.lines == 65 and len(asin_list.asins) == 60 and not asin_list.invalid
    assert asin_list.repeated() == {
        "B095WNGQ35": ["RV", "Moss Pole"],
        "B01I90VP0G": ["Classic", "Small"],
        "B08T75LT1Z": ["steeringdesk", "Steering Desk"],
        "B086RL1HPM": ["windowshade XL 2 Semi + 2 Trans", "windowshade XL 2 Semi 2 Trans"],
        "B0CR6LVYJL": ["Blindspot Mirror & Rain Visor 2pack", "Name Not Found"],
    }


def test_the_project_list_fits_in_four_reports():
    batches = batch_asins(load_asin_list(PROJECT_ROOT / "config" / "asins.txt").asins)
    assert len(batches) == 4 and all(len(" ".join(b)) <= ASIN_OPTION_MAX_CHARS for b in batches)


def test_case_and_comments_are_normalised(tmp_path):
    path = tmp_path / "asins.txt"
    path.write_text("# header\n\nb0testaaaa  One\nB0TESTAAAA  one again\nnot-an-asin  Bad\n", encoding="utf-8")
    asin_list = load_asin_list(path)
    assert asin_list.asins == ["B0TESTAAAA"] and asin_list.lines == 3
    assert asin_list.invalid == [(5, "not-an-asin  Bad")]
    assert "1 unique ASINs (expected 62)" in asin_list.summary(62)[0]


def test_missing_or_empty_list_is_a_configuration_error(tmp_path):
    with pytest.raises(ConfigurationError):
        load_asin_list(tmp_path / "nope.txt")
    (tmp_path / "empty.txt").write_text("# nothing\n", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_asin_list(tmp_path / "empty.txt")
