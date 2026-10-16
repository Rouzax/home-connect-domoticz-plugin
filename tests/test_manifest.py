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
    assert root.get("key") == "homeconnect"
    assert root.get("author") == "Rouzax"


def test_settings_and_defaults():
    params = {p.get("field"): p for p in manifest().iter("param")}
    expected = {"Username", "Password", "AllowControl", "DeviceLanguage", "SignOut", "DebugLevel"}
    assert set(params) == expected
    assert params["Password"].get("password") == "true"
    assert params["AllowControl"].get("default") == "false"
    assert params["SignOut"].get("default") == "false"
    options = params["DeviceLanguage"].iter("option")
    defaults = {o.get("value") for o in options if o.get("default") == "true"}
    assert defaults == {"auto"}


def test_manifest_warns_about_cleartext_storage():
    assert "cleartext" in ET.tostring(manifest(), encoding="unicode")
