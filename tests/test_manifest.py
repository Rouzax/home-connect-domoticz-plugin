import ast
import xml.etree.ElementTree as ET
from pathlib import Path


def manifest():
    source = (Path(__file__).resolve().parent.parent / "plugin.py").read_text(encoding="utf-8")
    docstring = ast.get_docstring(ast.parse(source))
    assert docstring is not None
    return ET.fromstring(docstring.strip())


def test_plugin_identity():
    root = manifest()
    assert root.get("key") == "homeconnectex"
    assert root.get("author") == "Rouzax"


def test_settings_and_defaults():
    params = {p.get("field"): p for p in manifest().iter("param")}
    expected = {
        "Username",
        "Password",
        "AllowControl",
        "DeviceLanguage",
        "ScreenMessages",
        "UpdateCheck",
        "SignOut",
        "DebugLevel",
    }
    assert set(params) == expected
    assert params["Password"].get("password") == "true"
    assert params["AllowControl"].get("default") == "false"
    assert params["SignOut"].get("default") == "false"
    options = params["DeviceLanguage"].iter("option")
    defaults = {o.get("value") for o in options if o.get("default") == "true"}
    assert defaults == {"auto"}
    update = params["UpdateCheck"].iter("option")
    assert {o.get("value") for o in update if o.get("default") == "true"} == {"auto"}
    screen = params["ScreenMessages"].iter("option")
    assert {o.get("value") for o in screen if o.get("default") == "true"} == {"problems"}


def test_manifest_warns_about_cleartext_storage():
    assert "cleartext" in ET.tostring(manifest(), encoding="unicode")


def help_text(param):
    node = param.find("description")
    return "" if node is None else " ".join(ET.tostring(node, encoding="unicode").split())


def test_every_setting_has_help():
    params = {p.get("field"): p for p in manifest().iter("param")}
    for field, param in params.items():
        assert help_text(param), field
        assert chr(0x2014) not in help_text(param), field
    assert "developer.home-connect.com" in help_text(params["Username"])
    assert "empty" in help_text(params["Password"])
    allow = help_text(params["AllowControl"])
    for words in ("scenes", "timers", "scripts", "start a program", "remote start"):
        assert words in allow, words
    sign_out = help_text(params["SignOut"])
    assert "turn it off" in sign_out
    assert "withdraws" in sign_out
    assert "Verbose" in help_text(params["DebugLevel"])


def test_sign_in_help_points_to_the_account_device_and_where_to_find_it():
    root = manifest()
    description = " ".join(ET.tostring(root.find("description"), encoding="unicode").split())
    assert '"Home Connect" device' in description
    assert "hidden" in description
    assert "Setup &gt; Devices" in description or "Setup > Devices" in description


def test_manifest_version_matches_the_changelog():
    changelog = (Path(__file__).resolve().parent.parent / "CHANGELOG.md").read_text(
        encoding="utf-8"
    )
    top = next(line for line in changelog.splitlines() if line.startswith("## ["))
    assert top.split("]")[0].removeprefix("## [") == manifest().get("version")
