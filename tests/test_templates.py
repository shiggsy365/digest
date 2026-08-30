import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from digest.text import plain_text


def test_all_templates_parse() -> None:
    root = Path(__file__).parents[1] / "digest" / "templates"
    environment = Environment(loader=FileSystemLoader(root))
    environment.filters["plaintext"] = lambda value: plain_text(value) or ""
    environment.filters["fromjson"] = json.loads

    for template in root.rglob("*.html"):
        environment.get_template(str(template.relative_to(root)))


def test_ereader_has_every_current_screen() -> None:
    root = Path(__file__).parents[1] / "digest" / "templates"
    modern = {item.name for item in (root / "modern").glob("*.html")}
    ereader = {item.name for item in (root / "ereader").glob("*.html")}

    assert modern <= ereader
