from __future__ import annotations

import pytest

import rlm_ig as rlm
from rlm_ig import loaders
from rlm_ig.loaders import load, load_many

# --------------------------------------------------------------- single files


def test_json_file_is_decoded(tmp_path):
    (tmp_path / "data.json").write_text('{"a": [1, 2], "b": null}')
    assert load(tmp_path / "data.json") == {"a": [1, 2], "b": None}


def test_json_suffix_is_case_insensitive(tmp_path):
    (tmp_path / "DATA.JSON").write_text("[1, 2, 3]")
    assert load(tmp_path / "DATA.JSON") == [1, 2, 3]


def test_jsonl_file_becomes_a_list_of_records_and_ignores_blank_lines(tmp_path):
    (tmp_path / "rows.jsonl").write_text('{"n": 1}\n\n   \n{"n": 2}\n')
    assert load(tmp_path / "rows.jsonl") == [{"n": 1}, {"n": 2}]


def test_ndjson_is_treated_like_jsonl(tmp_path):
    (tmp_path / "rows.ndjson").write_text('{"n": 1}\n{"n": 2}\n')
    assert load(tmp_path / "rows.ndjson") == [{"n": 1}, {"n": 2}]


def test_csv_file_becomes_a_list_of_row_dicts(tmp_path):
    (tmp_path / "t.csv").write_text("name,qty\nbolt,3\nnut,5\n")
    assert load(tmp_path / "t.csv") == [{"name": "bolt", "qty": "3"}, {"name": "nut", "qty": "5"}]


def test_csv_quoted_fields_with_commas_and_newlines(tmp_path):
    (tmp_path / "t.csv").write_text('name,note\nbolt,"a, b"\nnut,"line1\nline2"\n')
    rows = load(tmp_path / "t.csv")
    assert rows[0]["note"] == "a, b" and rows[1]["note"] == "line1\nline2"


def test_tsv_file_is_split_on_tabs(tmp_path):
    (tmp_path / "t.tsv").write_text("name\tqty\nbolt\t3\n")
    assert load(tmp_path / "t.tsv") == [{"name": "bolt", "qty": "3"}]


def test_plain_text_file_is_returned_verbatim(tmp_path):
    (tmp_path / "log.txt").write_text("line one\nline two\n")
    assert load(tmp_path / "log.txt") == "line one\nline two\n"


def test_unknown_extension_is_read_as_text(tmp_path):
    (tmp_path / "notes.weird").write_text("hello")
    assert load(tmp_path / "notes.weird") == "hello"


def test_file_without_extension_is_read_as_text(tmp_path):
    (tmp_path / "Makefile").write_text("all:\n\ttrue\n")
    assert load(tmp_path / "Makefile") == "all:\n\ttrue\n"


def test_undecodable_bytes_are_replaced_not_fatal(tmp_path):
    (tmp_path / "x.txt").write_bytes(b"ok \xff\xfe end")
    text = load(tmp_path / "x.txt")
    assert text.startswith("ok ") and text.endswith(" end")


def test_path_may_be_given_as_a_string(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    assert load(str(tmp_path / "a.txt")) == "x"


def test_user_home_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "a.txt").write_text("from home")
    assert load("~/a.txt") == "from home"


def test_invalid_json_raises_config_error_naming_the_path(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(rlm.ConfigError) as info:
        load(bad)
    assert str(bad) in str(info.value) and "json" in str(info.value)


def test_invalid_jsonl_line_raises_config_error_naming_the_path(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"ok": 1}\nnot json\n')
    with pytest.raises(rlm.ConfigError, match="bad.jsonl"):
        load(bad)


def test_missing_path_raises_config_error(tmp_path):
    missing = tmp_path / "nope.txt"
    with pytest.raises(rlm.ConfigError, match="no such file or directory") as info:
        load(missing)
    assert str(missing) in str(info.value)


def test_a_single_file_is_loaded_even_when_larger_than_the_directory_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(loaders, "DIR_FILE_LIMIT", 10)
    (tmp_path / "big.txt").write_text("x" * 100)
    assert load(tmp_path / "big.txt") == "x" * 100


# ---------------------------------------------------------------- directories


def test_directory_becomes_dict_of_relative_posix_paths(tmp_path):
    (tmp_path / "a.txt").write_text("A")
    (tmp_path / "sub" / "deep").mkdir(parents=True)
    (tmp_path / "sub" / "b.txt").write_text("B")
    (tmp_path / "sub" / "deep" / "c.md").write_text("C")
    assert load(tmp_path) == {"a.txt": "A", "sub/b.txt": "B", "sub/deep/c.md": "C"}


def test_directory_files_are_always_text_even_for_json(tmp_path):
    (tmp_path / "d.json").write_text('{"a": 1}')
    assert load(tmp_path) == {"d.json": '{"a": 1}'}


def test_directory_skips_hidden_files_and_hidden_folders(tmp_path):
    (tmp_path / "seen.txt").write_text("yes")
    (tmp_path / ".secret").write_text("no")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "x.txt").write_text("no")
    assert load(tmp_path) == {"seen.txt": "yes"}


@pytest.mark.parametrize("name", [".git", "node_modules", "__pycache__", ".venv", "dist", "build"])
def test_directory_skips_tooling_folders(tmp_path, name):
    (tmp_path / "keep.txt").write_text("k")
    (tmp_path / name).mkdir()
    (tmp_path / name / "junk.txt").write_text("junk")
    assert load(tmp_path) == {"keep.txt": "k"}


def test_directory_skips_nested_tooling_folders(tmp_path):
    (tmp_path / "pkg" / "node_modules").mkdir(parents=True)
    (tmp_path / "pkg" / "index.js").write_text("js")
    (tmp_path / "pkg" / "node_modules" / "dep.js").write_text("dep")
    assert load(tmp_path) == {"pkg/index.js": "js"}


def test_directory_skips_binary_files(tmp_path):
    (tmp_path / "text.txt").write_text("hello")
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n\x00\x00data")
    assert load(tmp_path) == {"text.txt": "hello"}


def test_directory_skips_oversized_files(tmp_path, monkeypatch):
    monkeypatch.setattr(loaders, "DIR_FILE_LIMIT", 50)
    (tmp_path / "small.txt").write_text("s" * 50)
    (tmp_path / "large.txt").write_text("l" * 51)
    assert load(tmp_path) == {"small.txt": "s" * 50}


def test_directory_keys_are_sorted_by_folder_walk(tmp_path):
    for name in ("b.txt", "a.txt", "c.txt"):
        (tmp_path / name).write_text(name)
    assert list(load(tmp_path)) == ["a.txt", "b.txt", "c.txt"]


def test_empty_directory_raises_config_error(tmp_path):
    with pytest.raises(rlm.ConfigError, match="no readable text files"):
        load(tmp_path)


def test_directory_with_only_skipped_files_raises_config_error(tmp_path):
    (tmp_path / ".hidden").write_text("x")
    (tmp_path / "bin.dat").write_bytes(b"\0\0\0")
    with pytest.raises(rlm.ConfigError, match="no readable text files"):
        load(tmp_path)


# ------------------------------------------------------------------ load_many


def test_load_many_with_no_paths_is_none():
    assert load_many([]) is None


def test_load_many_with_one_path_returns_that_value_directly(tmp_path):
    (tmp_path / "a.json").write_text("[1]")
    assert load_many([str(tmp_path / "a.json")]) == [1]


def test_load_many_with_several_paths_keys_by_file_name(tmp_path):
    (tmp_path / "a.txt").write_text("A")
    (tmp_path / "b.json").write_text('{"k": 1}')
    assert load_many([str(tmp_path / "a.txt"), str(tmp_path / "b.json")]) == {"a.txt": "A", "b.json": {"k": 1}}


def test_load_many_numbers_duplicate_file_names(tmp_path):
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    (tmp_path / "three").mkdir()
    for folder, text in (("one", "1"), ("two", "2"), ("three", "3")):
        (tmp_path / folder / "log.txt").write_text(text)
    paths = [str(tmp_path / f / "log.txt") for f in ("one", "two", "three")]
    assert load_many(paths) == {"log.txt": "1", "log.txt (2)": "2", "log.txt (3)": "3"}


def test_load_many_mixes_files_and_directories(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.txt").write_text("A")
    (tmp_path / "x.txt").write_text("X")
    assert load_many([str(tmp_path / "docs"), str(tmp_path / "x.txt")]) == {"docs": {"a.txt": "A"}, "x.txt": "X"}


def test_load_many_propagates_a_missing_path(tmp_path):
    (tmp_path / "a.txt").write_text("A")
    with pytest.raises(rlm.ConfigError, match="no such file"):
        load_many([str(tmp_path / "a.txt"), str(tmp_path / "missing.txt")])
