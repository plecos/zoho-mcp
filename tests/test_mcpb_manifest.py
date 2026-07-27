"""Tests for manifest.json, the MCPB bundle descriptor.

A manifest is only ever read by the host application, so nothing here fails
loudly during development -- a tool list that has drifted out of date, or an
entry_point that moved, surfaces as an extension that installs and then
misbehaves. These tests are the only thing standing between a rename and that.

The `uv` server type's requirements are checked too, since violating them
(bundling a virtualenv, omitting pyproject.toml) produces a bundle that fails
at install time on someone else's machine rather than here.
"""

import json
from pathlib import Path

import pytest

from tests.test_server import build_server

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads((REPO_ROOT / "manifest.json").read_text(encoding="utf-8"))


def test_the_manifest_is_valid_json_with_the_required_fields(manifest):
    for field in ("manifest_version", "name", "version", "description", "author"):
        assert manifest[field], field
    assert manifest["author"]["name"]


def test_the_declared_entry_point_exists(manifest):
    entry_point = REPO_ROOT / manifest["server"]["entry_point"]

    assert entry_point.is_file()


def test_the_entry_point_is_runnable_as_a_script(manifest):
    # The host executes the file; without a __main__ guard calling main() it
    # would import cleanly and then exit having served nothing.
    source = (REPO_ROOT / manifest["server"]["entry_point"]).read_text(encoding="utf-8")

    assert 'if __name__ == "__main__":' in source
    assert "main()" in source


def test_the_uv_server_type_requirements_are_met(manifest):
    assert manifest["server"]["type"] == "uv"
    # Required by the spec for this type: the host resolves dependencies from
    # pyproject.toml rather than from anything bundled.
    assert (REPO_ROOT / "pyproject.toml").is_file()
    # And forbidden by it: a bundled venv or vendored lib directory.
    assert not (REPO_ROOT / "server" / "lib").exists()
    assert not (REPO_ROOT / "server" / "venv").exists()


def test_the_ignore_file_excludes_what_must_not_ship():
    ignored = (REPO_ROOT / ".mcpbignore").read_text(encoding="utf-8")

    # .env holds a real client secret; .venv would break the uv server type;
    # scripts/ is CI tooling that has no business in a user's install.
    for pattern in (".env", ".venv/", "server/lib/", "server/venv/", "scripts/"):
        assert pattern in ignored, pattern


async def test_the_manifest_tool_list_matches_what_the_server_registers(manifest):
    # The drift this catches is silent: a renamed or added tool leaves the
    # manifest describing an extension that no longer exists.
    declared = {tool["name"] for tool in manifest["tools"]}
    registered = {tool.name for tool in await build_server().list_tools()}

    assert declared == registered


def test_every_declared_tool_has_a_description(manifest):
    undescribed = [t["name"] for t in manifest["tools"] if not t.get("description")]

    assert undescribed == []


def test_the_manifest_version_matches_the_package_version(manifest):
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert f'version = "{manifest["version"]}"' in pyproject


def test_required_credentials_are_collected_from_the_user(manifest):
    user_config = manifest["user_config"]

    assert user_config["zoho_client_id"]["required"] is True
    assert user_config["zoho_client_secret"]["required"] is True


def test_the_client_secret_is_marked_sensitive(manifest):
    # Without this the host renders it in plain text and stores it as plain
    # config rather than in the OS credential store.
    assert manifest["user_config"]["zoho_client_secret"]["sensitive"] is True


def test_the_client_id_is_not_marked_sensitive(manifest):
    # It isn't a secret, and masking it would stop the user checking they
    # pasted the right one.
    assert manifest["user_config"]["zoho_client_id"].get("sensitive", False) is False


def test_auto_send_is_exposed_as_a_setting_that_defaults_to_off(manifest):
    # It used to be withheld from the settings pane on the theory that
    # hand-editing the environment was useful friction. For a bundle install
    # it wasn't friction, it was unreachable: `.env` would have to live inside
    # the installed extension directory, which every update replaces. The
    # protection that does the work is the default, not the obscurity.
    setting = manifest["user_config"]["zoho_allow_auto_send"]

    assert setting["type"] == "boolean"
    assert setting["default"] is False
    assert setting["required"] is False
    assert (
        manifest["server"]["mcp_config"]["env"]["ZOHO_ALLOW_AUTO_SEND"]
        == "${user_config.zoho_allow_auto_send}"
    )


def test_the_auto_send_setting_says_what_turning_it_on_means(manifest):
    # The label is now the entire basis for the decision -- there's no README
    # in front of someone ticking a checkbox in a settings pane.
    setting = manifest["user_config"]["zoho_allow_auto_send"]

    assert "review" in setting["description"].lower()
    assert "draft" in setting["description"].lower()


def test_no_declared_tool_offers_to_change_the_servers_settings(manifest):
    # The invariant that outlived the old "not exposed as a setting" test:
    # whoever turns sending on, it must not be the model. A tool that edits
    # configuration would hand the gate to the thing the gate exists to stop.
    for tool in manifest["tools"]:
        assert "config" not in tool["name"], tool["name"]
        assert "setting" not in tool["name"], tool["name"]
        assert not tool["name"].startswith(("enable_", "disable_", "set_")), tool[
            "name"
        ]


def test_every_configured_env_var_maps_to_a_declared_user_config_key(manifest):
    # A typo here yields an empty environment variable at runtime, which
    # looks exactly like the user not having filled the field in.
    declared = set(manifest["user_config"])
    for value in manifest["server"]["mcp_config"]["env"].values():
        assert value.startswith("${user_config.") and value.endswith("}"), value
        assert value[len("${user_config.") : -1] in declared, value


def test_the_python_runtime_requirement_matches_pyproject(manifest):
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    declared = manifest["compatibility"]["runtimes"]["python"]

    assert f'requires-python = "{declared}"' in pyproject


def test_the_license_matches_the_project(manifest):
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert f'license = "{manifest["license"]}"' in pyproject


def test_the_uv_requirement_has_no_upper_bound():
    """pyproject.toml ships in the bundle, so its uv constraint binds users.

    The uv that reads it belongs to whoever installs the extension -- Claude
    Desktop supplies none -- so an upper bound would break every install and
    every CI run on the day uv ships a new major, with an error about a
    constraint the user never wrote. There was a `<0.12.0` here once.
    """
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    constraint = next(
        line.split("=", 1)[1].strip().strip('"')
        for line in pyproject.splitlines()
        if line.startswith("required-version")
    )

    assert "<" not in constraint, f"upper bound reintroduced: {constraint!r}"
