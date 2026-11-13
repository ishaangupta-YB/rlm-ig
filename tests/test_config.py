from __future__ import annotations

import pytest

import rlm_ig as rlm
from rlm_ig.config import PROJECT_FILE, Config, default_trace_dir, user_config_path

ENV_VARS = (
    "RLM_CONFIG",
    "RLM_MODEL",
    "RLM_SUB_MODEL",
    "RLM_MAX_DEPTH",
    "RLM_MAX_STEPS",
    "RLM_MAX_CALLS",
    "RLM_MAX_USD",
    "RLM_MAX_PARALLEL",
    "RLM_TIMEOUT",
    "RLM_TRACE_DIR",
)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """No RLM_* variables, a private user config home, and a project-free cwd."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    return workdir


def write_user(tmp_path, text):
    path = tmp_path / "xdg" / "rlm-ig" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    return path


# ------------------------------------------------------------------ defaults


def test_defaults():
    config = Config()
    assert config.model is None and config.sub_model is None
    assert (config.max_depth, config.max_steps) == (2, 20)
    assert config.max_calls == 300 and config.max_usd == 5.0
    assert config.max_tokens is None and config.timeout is None
    assert config.cell_timeout == 120.0 and config.output_limit == 8000
    assert config.trace is True and config.verbosity == 1
    assert config.providers == {} and config.pricing == {} and config.mcp == {}


def test_load_with_no_files_gives_the_defaults(tmp_path):
    config = Config.load()
    assert config.max_steps == 20 and config.model is None
    assert config.trace_dir == str(tmp_path / "state" / "rlm-ig" / "traces")


def test_user_config_path_follows_xdg_config_home(tmp_path):
    assert user_config_path() == tmp_path / "xdg" / "rlm-ig" / "config.toml"


def test_user_config_path_falls_back_to_home_dot_config(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert user_config_path() == tmp_path / "home" / ".config" / "rlm-ig" / "config.toml"


def test_default_trace_dir_follows_xdg_state_home(tmp_path):
    assert default_trace_dir() == str(tmp_path / "state" / "rlm-ig" / "traces")


# ---------------------------------------------------------------- precedence


def test_user_file_is_read(tmp_path):
    write_user(tmp_path, "max_steps = 7\n")
    assert Config.load().max_steps == 7


def test_project_file_beats_user_file(tmp_path, isolated):
    write_user(tmp_path, "max_steps = 7\nmax_depth = 1\n")
    (isolated / PROJECT_FILE).write_text("max_steps = 9\n")
    config = Config.load()
    assert config.max_steps == 9
    assert config.max_depth == 1  # keys the project file does not set still come from the user file


def test_project_file_is_found_in_a_parent_directory(tmp_path, isolated, monkeypatch):
    (isolated / PROJECT_FILE).write_text("max_steps = 11\n")
    nested = isolated / "a" / "b"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert Config.load().max_steps == 11


def test_nearest_project_file_wins(isolated, monkeypatch):
    (isolated / PROJECT_FILE).write_text("max_steps = 11\n")
    inner = isolated / "inner"
    inner.mkdir()
    (inner / PROJECT_FILE).write_text("max_steps = 12\n")
    monkeypatch.chdir(inner)
    assert Config.load().max_steps == 12


def test_env_vars_beat_files(tmp_path, isolated, monkeypatch):
    write_user(tmp_path, "max_steps = 7\n")
    (isolated / PROJECT_FILE).write_text("max_steps = 9\n")
    monkeypatch.setenv("RLM_MAX_STEPS", "13")
    assert Config.load().max_steps == 13


def test_keyword_overrides_beat_env_vars(monkeypatch):
    monkeypatch.setenv("RLM_MAX_STEPS", "13")
    assert Config.load(max_steps=15).max_steps == 15


def test_none_overrides_are_ignored(isolated):
    (isolated / PROJECT_FILE).write_text("max_steps = 9\n")
    assert Config.load(max_steps=None, model=None).max_steps == 9


def test_full_precedence_chain(tmp_path, isolated, monkeypatch):
    write_user(tmp_path, "max_steps = 1\nmax_depth = 1\nmax_calls = 10\nmax_usd = 1.0\n")
    (isolated / PROJECT_FILE).write_text("max_depth = 2\nmax_calls = 20\nmax_usd = 2.0\n")
    monkeypatch.setenv("RLM_MAX_CALLS", "30")
    monkeypatch.setenv("RLM_MAX_USD", "3.5")
    config = Config.load(max_usd=4.0)
    assert config.max_steps == 1  # user file only
    assert config.max_depth == 2  # project beats user
    assert config.max_calls == 30  # env beats project
    assert config.max_usd == 4.0  # override beats env


def test_env_var_values_are_cast(monkeypatch):
    monkeypatch.setenv("RLM_MODEL", "scripted:x")
    monkeypatch.setenv("RLM_SUB_MODEL", "scripted:y")
    monkeypatch.setenv("RLM_MAX_DEPTH", "4")
    monkeypatch.setenv("RLM_MAX_PARALLEL", "3")
    monkeypatch.setenv("RLM_TIMEOUT", "2.5")
    monkeypatch.setenv("RLM_TRACE_DIR", "/somewhere/traces")
    config = Config.load()
    assert (config.model, config.sub_model) == ("scripted:x", "scripted:y")
    assert config.max_depth == 4 and config.max_parallel == 3
    assert config.timeout == 2.5 and config.trace_dir == "/somewhere/traces"


def test_empty_env_var_is_ignored(monkeypatch):
    monkeypatch.setenv("RLM_MAX_STEPS", "")
    assert Config.load().max_steps == 20


def test_bad_env_value_raises_config_error_naming_variable(monkeypatch):
    monkeypatch.setenv("RLM_MAX_STEPS", "many")
    with pytest.raises(rlm.ConfigError, match=r"\$RLM_MAX_STEPS='many' is not a valid int"):
        Config.load()


def test_bad_float_env_value_raises_config_error(monkeypatch):
    monkeypatch.setenv("RLM_MAX_USD", "lots")
    with pytest.raises(rlm.ConfigError, match="RLM_MAX_USD"):
        Config.load()


def test_nested_tables_from_files_merge_instead_of_replacing(tmp_path, isolated):
    write_user(tmp_path, '[providers.a]\nkind = "command"\n[providers.b]\nkind = "command"\n')
    (isolated / PROJECT_FILE).write_text('[providers.b]\ncommand = ["x"]\n')
    config = Config.load()
    assert set(config.providers) == {"a", "b"}
    assert config.providers["b"] == {"kind": "command", "command": ["x"]}


# -------------------------------------------------------------- explicit file


def test_explicit_path_replaces_project_file_lookup(isolated, tmp_path):
    (isolated / PROJECT_FILE).write_text("max_steps = 9\n")
    other = tmp_path / "other.toml"
    other.write_text("max_depth = 3\n")
    config = Config.load(other)
    assert config.max_depth == 3 and config.max_steps == 20


def test_explicit_path_still_sits_above_the_user_file(tmp_path):
    write_user(tmp_path, "max_steps = 7\nmax_depth = 1\n")
    other = tmp_path / "other.toml"
    other.write_text("max_depth = 3\n")
    config = Config.load(str(other))
    assert config.max_steps == 7 and config.max_depth == 3


def test_rlm_config_env_var_selects_the_file(tmp_path, monkeypatch):
    other = tmp_path / "other.toml"
    other.write_text("max_steps = 5\n")
    monkeypatch.setenv("RLM_CONFIG", str(other))
    assert Config.load().max_steps == 5


def test_explicit_path_beats_rlm_config(tmp_path, monkeypatch):
    env_file, arg_file = tmp_path / "env.toml", tmp_path / "arg.toml"
    env_file.write_text("max_steps = 5\n")
    arg_file.write_text("max_steps = 6\n")
    monkeypatch.setenv("RLM_CONFIG", str(env_file))
    assert Config.load(arg_file).max_steps == 6


def test_missing_explicit_file_is_an_error(tmp_path):
    with pytest.raises(rlm.ConfigError, match="config file not found"):
        Config.load(tmp_path / "nope.toml")


def test_missing_rlm_config_file_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("RLM_CONFIG", str(tmp_path / "nope.toml"))
    with pytest.raises(rlm.ConfigError, match="config file not found"):
        Config.load()


# ------------------------------------------------------------- nested tables


def test_sandbox_table_becomes_sandbox_options(isolated):
    (isolated / PROJECT_FILE).write_text(
        '[sandbox]\nmemory_mb = 512\ninherit_env = true\npass_env = ["HOME"]\n[sandbox.env]\nFOO = "bar"\n'
    )
    sandbox = Config.load().sandbox
    assert sandbox.memory_mb == 512 and sandbox.inherit_env is True
    assert sandbox.pass_env == ["HOME"] and sandbox.env == {"FOO": "bar"}


def test_provider_tables_are_kept_as_dicts(isolated):
    (isolated / PROJECT_FILE).write_text(
        '[providers.mycli]\nkind = "command"\ncommand = ["mycli", "--x"]\n[providers.openai]\nbase_url = "http://localhost:1"\n'
    )
    providers = Config.load().providers
    assert providers["mycli"] == {"kind": "command", "command": ["mycli", "--x"]}
    assert providers["openai"] == {"base_url": "http://localhost:1"}


def test_pricing_table_with_quoted_model_names(isolated):
    (isolated / PROJECT_FILE).write_text('[pricing."my-model:1.5"]\ninput = 3.0\noutput = 15.0\ncached = 0.3\n')
    assert Config.load().pricing == {"my-model:1.5": {"input": 3.0, "output": 15.0, "cached": 0.3}}


def test_mcp_tables(isolated):
    (isolated / PROJECT_FILE).write_text(
        '[mcp.files]\ncommand = "server"\nargs = ["--root", "."]\n[mcp.remote]\nurl = "http://localhost:9/mcp"\n'
    )
    mcp = Config.load().mcp
    assert mcp["files"] == {"command": "server", "args": ["--root", "."]}
    assert mcp["remote"] == {"url": "http://localhost:9/mcp"}


def test_top_level_keys_next_to_tables(isolated):
    (isolated / PROJECT_FILE).write_text(
        'model = "scripted:x"\ntrace = false\nverbosity = "verbose"\n[sandbox]\nmemory_mb = 64\n'
    )
    config = Config.load()
    assert config.model == "scripted:x" and config.trace is False
    assert config.verbosity == 2 and config.sandbox.memory_mb == 64


def test_sandbox_launcher_string_is_split_into_words():
    config = Config(sandbox={"launcher": "firejail --quiet --net=none"})
    assert config.sandbox.launcher == ["firejail", "--quiet", "--net=none"]


def test_sandbox_launcher_list_is_kept_as_is():
    config = Config(sandbox={"launcher": ["bwrap", "--unshare-net"]})
    assert config.sandbox.launcher == ["bwrap", "--unshare-net"]


def test_sandbox_launcher_string_from_toml_is_split(isolated):
    (isolated / PROJECT_FILE).write_text('[sandbox]\nlauncher = "nice -n 5"\n')
    assert Config.load().sandbox.launcher == ["nice", "-n", "5"]


def test_cell_timeout_reaches_the_sandbox_options():
    assert Config(cell_timeout=7).sandbox.cell_timeout == 7.0
    assert Config().sandbox.cell_timeout == 120.0


def test_cell_timeout_from_a_file_reaches_the_sandbox_options(isolated):
    (isolated / PROJECT_FILE).write_text("cell_timeout = 9\n")
    assert Config.load().sandbox.cell_timeout == 9.0


def test_replace_cell_timeout_does_not_leave_a_stale_sandbox_value():
    config = Config(cell_timeout=7).replace(cell_timeout=30)
    assert config.sandbox.cell_timeout == 30.0


def test_replace_does_not_change_the_original_configs_sandbox():
    base = Config(cell_timeout=7)
    base.replace(cell_timeout=30)
    assert base.sandbox.cell_timeout == 7.0


# --------------------------------------------------------------------- errors


def test_unknown_top_level_key_names_the_key(isolated):
    (isolated / PROJECT_FILE).write_text("max_stepz = 3\n")
    with pytest.raises(rlm.ConfigError, match="unknown option.*max_stepz"):
        Config.load()


def test_several_unknown_keys_are_all_named(isolated):
    (isolated / PROJECT_FILE).write_text("zzz = 1\naaa = 2\n")
    with pytest.raises(rlm.ConfigError, match="aaa, zzz"):
        Config.load()


def test_unknown_override_is_an_error():
    with pytest.raises(rlm.ConfigError, match="bogus"):
        Config.load(bogus=1)


def test_unknown_sandbox_key_names_the_key(isolated):
    (isolated / PROJECT_FILE).write_text("[sandbox]\nmemory = 5\n")
    with pytest.raises(rlm.ConfigError, match=r"\[sandbox\].*memory"):
        Config.load()


def test_invalid_toml_names_the_file(isolated):
    path = isolated / PROJECT_FILE
    path.write_text("max_steps = = 3\n")
    with pytest.raises(rlm.ConfigError) as info:
        Config.load()
    assert str(path) in str(info.value)


def test_invalid_toml_in_the_user_file_is_reported(tmp_path):
    path = write_user(tmp_path, "[unterminated\n")
    with pytest.raises(rlm.ConfigError, match="config.toml"):
        Config.load()
    assert path.is_file()


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"max_depth": -1}, "max_depth cannot be negative"),
        ({"max_steps": 0}, "max_steps must be at least 1"),
        ({"output_limit": 199}, "output_limit must be at least 200"),
        ({"max_parallel": 0}, "max_parallel must be at least 1"),
        ({"max_calls": 0}, "max_calls must be positive"),
        ({"max_usd": -1.0}, "max_usd must be positive"),
        ({"max_tokens": -5}, "max_tokens must be positive"),
        ({"timeout": 0}, "timeout must be positive"),
    ],
)
def test_validation_errors(options, message):
    with pytest.raises(rlm.ConfigError, match=message):
        Config(**options)


def test_output_limit_of_exactly_200_is_allowed():
    assert Config(output_limit=200).output_limit == 200


def test_limits_may_be_disabled_with_none():
    config = Config(max_calls=None, max_usd=None)
    assert config.max_calls is None and config.max_usd is None


def test_validation_applies_to_loaded_files(isolated):
    (isolated / PROJECT_FILE).write_text("max_steps = 0\n")
    with pytest.raises(rlm.ConfigError, match="max_steps"):
        Config.load()


# ------------------------------------------------------------------ verbosity


@pytest.mark.parametrize(
    ("name", "level"), [("quiet", 0), ("silent", 0), ("normal", 1), ("verbose", 2), (0, 0), (1, 1), (2, 2)]
)
def test_verbosity_names_and_numbers(name, level):
    assert Config(verbosity=name).verbosity == level


def test_bad_verbosity_name_lists_the_choices():
    with pytest.raises(rlm.ConfigError, match="verbosity must be one of quiet, silent, normal, verbose"):
        Config(verbosity="loud")


# -------------------------------------------------------------------- replace


def test_replace_returns_a_modified_copy():
    base = Config(max_steps=5)
    changed = base.replace(max_steps=8, max_depth=0)
    assert (changed.max_steps, changed.max_depth) == (8, 0)
    assert base.max_steps == 5


def test_replace_ignores_none_values():
    assert Config(max_steps=5).replace(max_steps=None).max_steps == 5


def test_replace_rejects_unknown_names():
    with pytest.raises(rlm.ConfigError, match="unknown option.*nope"):
        Config().replace(nope=1)


def test_replace_validates_the_result():
    with pytest.raises(rlm.ConfigError, match="max_steps"):
        Config().replace(max_steps=0)
