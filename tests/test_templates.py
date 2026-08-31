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


def test_ereader_static_assets_keep_eink_layout_contract() -> None:
    root = Path(__file__).parents[1] / "digest" / "static"
    ereader_css = (root / "ereader.css").read_text()
    spa_css = (root / "ereader-app.css").read_text()
    spa_js = (root / "ereader-app.js").read_text()
    ereader_base = (
        Path(__file__).parents[1] / "digest" / "templates" / "ereader" / "base.html"
    ).read_text()
    spa_base = (
        Path(__file__).parents[1] / "digest" / "templates" / "ereader" / "app.html"
    ).read_text()

    assert "prefers-color-scheme:dark" not in ereader_css
    assert "prefers-color-scheme:dark" not in spa_css
    assert "function pageSizeFor" in spa_js
    assert "page_size=24" in spa_js
    assert "description-page" in spa_js
    assert "id=\"pg-back\"" in ereader_base
    assert "id=\"spa-back\"" in spa_base
    assert "&lt;&lt;" in ereader_base and "&gt;&gt;" in ereader_base
    assert "&lt;&lt;" in spa_base and "&gt;&gt;" in spa_base
    assert "#shell-filters-row .shell-cell" in ereader_css
    assert "#spa-filters{position:relative;z-index:1" in spa_css


def test_ereader_book_covers_are_links() -> None:
    root = Path(__file__).parents[1] / "digest"
    template_text = "\n".join(
        item.read_text() for item in (root / "templates" / "ereader").glob("*.html")
    )
    spa_js = (root / "static" / "ereader-app.js").read_text()

    assert '<div class="book-cover"><img' not in template_text
    assert '<div class="book-cover"><a href="' in spa_js
