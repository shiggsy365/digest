from digest.text import plain_text


def test_html_description_is_converted_to_readable_plain_text() -> None:
    value = "<p>A <em>warning</em> &amp; a promise.</p><p>Second paragraph.<br>Next line.</p>"

    assert plain_text(value) == "A warning & a promise.\n\nSecond paragraph.\n\nNext line."


def test_scripts_and_styles_are_removed_from_description() -> None:
    assert plain_text("Before<script>alert('x')</script><style>x{}</style>After") == "BeforeAfter"
