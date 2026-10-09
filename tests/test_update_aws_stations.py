"""Offline tests for the developer catalog refresh CLI.

Exercises ``scripts/update_aws_stations.py`` with small synthetic public-shaped
HTML and temporary catalog files — no network, no committed full HTML, no
addresses/coordinates/observations/auth. The real bundled catalog is only touched
by a read-only ``--check`` subprocess run.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "update_aws_stations.py"

_spec = importlib.util.spec_from_file_location("update_aws_stations", _SCRIPT)
assert _spec and _spec.loader
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)


def _station(aws_id, name, region, stn_sp="SA"):
    return {"awsId": str(aws_id), "nameKo": name, "stnSp": stn_sp, "addr": f"{region} 어딘가읍"}


def _html(regions):
    inner = json.dumps(regions, ensure_ascii=False)
    literal = json.dumps(inner, ensure_ascii=False)  # double-encoded, like the page
    return f"<html><script>var sidoStations = JSON.parse({literal});</script></html>"


def _write_html(tmp_path, regions, name="page.html"):
    path = tmp_path / name
    path.write_text(_html(regions), encoding="utf-8")
    return path


def _write_catalog(tmp_path, catalog, *, date="2026-10-08", name="aws_stations.py"):
    items = [f'{k}: ("{n}", "{r}")' for k, (n, r) in sorted(catalog.items())]
    rows = ["    " + ",    ".join(items[i : i + 4]) + "," for i in range(0, len(items), 4)]
    body = "{\n" + "\n".join(rows) + "\n}"
    path = tmp_path / name
    path.write_text(
        f'"""테스트 카탈로그.\n\n추출일: {date}. 유형 ASOS/AWS/SG.\n"""\n'
        "from __future__ import annotations\n\n"
        f"AWS_STATION_CATALOG: dict[int, tuple[str, str]] = {body}\n",
        encoding="utf-8",
    )
    return path


def _args(*argv):
    return tool.build_parser().parse_args(list(argv))


# --- parsing / filtering ---------------------------------------------------

def test_parse_source_filters_to_sa_ss_sg_and_maps_region():
    html = _html({
        "11": [
            _station(1, "서울", "서울특별시", "SA"),
            _station(2, "부산", "부산광역시", "SS"),
            _station(3, "남면", "경기도", "SG"),
            _station(4, "공항", "제주특별자치도", "AM"),
            _station(5, "무인", "강원특별자치도", "SW"),
            _station(6, "해상", "인천광역시", "OA"),
        ]
    })
    assert tool.parse_source(html) == {
        1: ("서울", "서울특별시"),
        2: ("부산", "부산광역시"),
        3: ("남면", "경기도"),
    }


def test_parse_source_region_is_first_addr_token():
    html = _html({"11": [_station(100, "대관령", "(산지)강원특별자치도", "SA")]})
    assert tool.parse_source(html)[100] == ("대관령", "(산지)강원특별자치도")


def test_parse_source_deduplicates_exact_duplicates():
    html = _html({
        "11": [_station(1, "서울", "서울특별시")],
        "26": [_station(1, "서울", "서울특별시")],  # exact duplicate across regions
    })
    assert tool.parse_source(html) == {1: ("서울", "서울특별시")}


def test_parse_source_rejects_conflicting_duplicates():
    html = _html({
        "11": [_station(1, "서울", "서울특별시")],
        "26": [_station(1, "다른이름", "부산광역시")],
    })
    with pytest.raises(tool.SourceError, match="conflicting duplicate"):
        tool.parse_source(html)


@pytest.mark.parametrize(
    "html",
    [
        "<html>no marker</html>",
        '<script>var sidoStations = JSON.parse("{}");</script>',  # empty object
        '<script>var sidoStations = JSON.parse("not json");</script>',  # bad inner
        _html({"11": [_station(9, "무관", "제주특별자치도", "AM")]}),  # only excluded types
    ],
)
def test_parse_source_rejects_empty_or_malformed(html):
    with pytest.raises(tool.SourceError):
        tool.parse_source(html)


@pytest.mark.parametrize("bad_id", ["abc", "0", "-1", "", None])
def test_parse_source_rejects_invalid_aws_id(bad_id):
    station = {"awsId": bad_id, "nameKo": "서울", "stnSp": "SA", "addr": "서울특별시 강남"}
    with pytest.raises(tool.SourceError, match="invalid awsId"):
        tool.parse_source(_html({"11": [station]}))


@pytest.mark.parametrize(
    "station",
    [
        {"awsId": "1", "nameKo": "  ", "stnSp": "SA", "addr": "서울특별시 강남"},
        {"awsId": "1", "nameKo": "서울", "stnSp": "SA", "addr": "   "},
    ],
)
def test_parse_source_rejects_invalid_name_or_region(station):
    with pytest.raises(tool.SourceError):
        tool.parse_source(_html({"11": [station]}))


# --- read / render ---------------------------------------------------------

def test_read_catalog_parses_without_importing(tmp_path):
    path = _write_catalog(tmp_path, {1: ("서울", "서울특별시"), 3: ("남면", "경기도")})
    _source, catalog, _node = tool.read_catalog(path)
    assert catalog == {1: ("서울", "서울특별시"), 3: ("남면", "경기도")}


def test_render_map_is_deterministic_and_sorted():
    catalog = {3: ("남면", "경기도"), 1: ("서울", "서울특별시")}
    rendered = tool.render_map(catalog)
    first = rendered.splitlines()[1]
    assert first.startswith("    1: ")  # sorted: id 1 before id 3
    assert first.index("1:") < first.index("3:")
    import ast

    assert ast.literal_eval(rendered) == catalog


def test_render_map_roundtrips_quotes_backslashes_unicode():
    """Renderer must emit valid Python for quotes/backslashes/unicode (independent)."""
    catalog = {
        351: ('남면 "관측"', "경기도"),
        352: ("백슬래시\\이름", "강원특별자치도"),
        353: ("작은따옴표'섞임", "서울특별시"),
        354: ("줄바꿈\n탭\t끝", "제주특별자치도"),
    }
    rendered = tool.render_map(catalog)
    import ast

    assert ast.literal_eval(rendered) == catalog


# --- check mode ------------------------------------------------------------

def test_check_in_sync_exit_zero_and_no_write(tmp_path):
    catalog = {1: ("서울", "서울특별시"), 2: ("남면", "경기도")}
    cat_path = _write_catalog(tmp_path, catalog)
    html_path = _write_html(tmp_path, {"11": [_station(1, "서울", "서울특별시"), _station(2, "남면", "경기도", "SG")]})
    before = cat_path.read_bytes()
    assert tool.run(_args("--check", "--html", str(html_path)), catalog_path=cat_path) == 0
    assert cat_path.read_bytes() == before


def test_check_drift_exit_nonzero_and_no_write(tmp_path, capsys):
    cat_path = _write_catalog(tmp_path, {1: ("서울", "서울특별시")})
    html_path = _write_html(tmp_path, {"11": [_station(1, "서울", "서울특별시"), _station(2, "남면", "경기도", "SG")]})
    before = cat_path.read_bytes()
    assert tool.run(_args("--check", "--html", str(html_path)), catalog_path=cat_path) == 1
    assert cat_path.read_bytes() == before
    assert "added" in capsys.readouterr().out


def test_check_rejects_date_option(tmp_path):
    cat_path = _write_catalog(tmp_path, {1: ("서울", "서울특별시")})
    html_path = _write_html(tmp_path, {"11": [_station(1, "서울", "서울특별시")]})
    assert tool.run(_args("--check", "--date", "2026-01-01", "--html", str(html_path)), catalog_path=cat_path) == 2


# --- write mode ------------------------------------------------------------

def test_write_refuses_removals_by_default(tmp_path, capsys):
    cat_path = _write_catalog(tmp_path, {1: ("서울", "서울특별시"), 2: ("남면", "경기도")})
    html_path = _write_html(tmp_path, {"11": [_station(1, "서울", "서울특별시")]})
    before = cat_path.read_bytes()
    assert tool.run(_args("--write", "--html", str(html_path)), catalog_path=cat_path) == 2
    assert cat_path.read_bytes() == before
    assert "refusing to remove" in capsys.readouterr().err


def test_write_allows_removals_with_flag(tmp_path):
    cat_path = _write_catalog(tmp_path, {1: ("서울", "서울특별시"), 2: ("남면", "경기도")})
    html_path = _write_html(tmp_path, {"11": [_station(1, "서울", "서울특별시")]})
    assert tool.run(_args("--write", "--allow-removals", "--html", str(html_path)), catalog_path=cat_path) == 0
    _src, catalog, _node = tool.read_catalog(cat_path)
    assert catalog == {1: ("서울", "서울특별시")}


def test_write_updates_map_and_date(tmp_path):
    cat_path = _write_catalog(tmp_path, {1: ("서울", "서울특별시")}, date="2026-10-08")
    html_path = _write_html(tmp_path, {"11": [_station(1, "서울", "서울특별시"), _station(2, "남면", "경기도", "SG")]})
    assert tool.run(
        _args("--write", "--date", "2026-11-30", "--html", str(html_path)), catalog_path=cat_path
    ) == 0
    source, catalog, _node = tool.read_catalog(cat_path)
    assert catalog == {1: ("서울", "서울특별시"), 2: ("남면", "경기도")}
    assert "추출일: 2026-11-30." in source


def test_write_unchanged_map_preserves_literal(tmp_path):
    catalog = {1: ("서울", "서울특별시"), 2: ("남면", "경기도")}
    cat_path = _write_catalog(tmp_path, catalog, date="2026-10-08")
    html_path = _write_html(tmp_path, {"11": [_station(1, "서울", "서울특별시"), _station(2, "남면", "경기도", "SG")]})
    before = cat_path.read_bytes()
    # same date + unchanged map -> byte-identical (no whole-catalog reflow)
    assert tool.run(
        _args("--write", "--date", "2026-10-08", "--html", str(html_path)), catalog_path=cat_path
    ) == 0
    assert cat_path.read_bytes() == before


def test_write_rejects_bad_date(tmp_path):
    cat_path = _write_catalog(tmp_path, {1: ("서울", "서울특별시")})
    html_path = _write_html(tmp_path, {"11": [_station(1, "서울", "서울특별시")]})
    assert tool.run(
        _args("--write", "--date", "2026/01/01", "--html", str(html_path)), catalog_path=cat_path
    ) == 2


def test_write_requires_extraction_date_field(tmp_path):
    """A catalog without 추출일 must not be written (no silent success)."""
    path = tmp_path / "aws_stations.py"
    path.write_text(
        '"""no date field."""\nfrom __future__ import annotations\n\n'
        "AWS_STATION_CATALOG: dict[int, tuple[str, str]] = {\n"
        '    1: ("서울", "서울특별시"),\n}\n',
        encoding="utf-8",
    )
    html_path = _write_html(tmp_path, {"11": [_station(1, "서울", "서울특별시")]})
    before = path.read_bytes()
    assert tool.run(_args("--write", "--html", str(html_path)), catalog_path=path) == 2
    assert path.read_bytes() == before


def test_write_preserves_mode_and_leaves_no_temp(tmp_path):
    import stat

    cat_path = _write_catalog(tmp_path, {1: ("서울", "서울특별시")})
    os.chmod(cat_path, 0o640)
    html_path = _write_html(
        tmp_path,
        {"11": [_station(1, "서울", "서울특별시"), _station(2, "남면", "경기도", "SG")]},
    )
    assert tool.run(
        _args("--write", "--date", "2026-11-30", "--html", str(html_path)), catalog_path=cat_path
    ) == 0
    assert stat.S_IMODE(cat_path.stat().st_mode) == 0o640
    assert not list(tmp_path.glob("*.tmp"))


def test_write_validates_generated_source_before_replace(tmp_path, monkeypatch):
    cat_path = _write_catalog(tmp_path, {1: ("서울", "서울특별시")})
    html_path = _write_html(
        tmp_path,
        {"11": [_station(1, "서울", "서울특별시"), _station(2, "남면", "경기도", "SG")]},
    )
    before = cat_path.read_bytes()
    monkeypatch.setattr(tool, "render_map", lambda _catalog: "{ not valid python")
    assert tool.run(_args("--write", "--html", str(html_path)), catalog_path=cat_path) == 2
    assert cat_path.read_bytes() == before


def test_check_rejects_allow_removals(tmp_path):
    cat_path = _write_catalog(tmp_path, {1: ("서울", "서울특별시")})
    html_path = _write_html(tmp_path, {"11": [_station(1, "서울", "서울특별시")]})
    assert tool.run(
        _args("--check", "--allow-removals", "--html", str(html_path)), catalog_path=cat_path
    ) == 2


def test_html_non_utf8_exit_two(tmp_path):
    cat_path = _write_catalog(tmp_path, {1: ("서울", "서울특별시")})
    bad = tmp_path / "bad.html"
    bad.write_bytes(b"\xff\xfe\x00not utf8")
    assert tool.run(_args("--check", "--html", str(bad)), catalog_path=cat_path) == 2


def test_fetch_failure_exit_two(tmp_path):
    cat_path = _write_catalog(tmp_path, {1: ("서울", "서울특별시")})

    def _boom():
        raise OSError("network down")

    assert tool.run(_args("--check"), catalog_path=cat_path, fetch=_boom) == 2


# --- true CLI behavioral check --------------------------------------------

def test_cli_subprocess_check_reports_drift(tmp_path):
    """Run the real CLI: a small synthetic page drifts from the bundled catalog."""
    html_path = _write_html(tmp_path, {"11": [_station(1, "서울", "서울특별시")]})
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "--check", "--html", str(html_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 1, proc.stderr
    assert "catalog drift" in proc.stdout
