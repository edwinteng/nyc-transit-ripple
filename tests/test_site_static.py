"""Static checks of the dashboard files in site/ (no browser needed)."""

import re
from pathlib import Path

SITE = Path(__file__).resolve().parents[1] / "site"


def read(name: str) -> str:
    return (SITE / name).read_text(encoding="utf-8")


def test_cdn_assets_are_version_pinned_with_subresource_integrity():
    html = read("index.html")
    tags = re.findall(r"<(?:script|link)\b[^>]*https://[^>]*>", html)
    assert len(tags) == 3  # Leaflet CSS + JS, Chart.js
    for tag in tags:
        assert re.search(r'integrity="sha384-[A-Za-z0-9+/=]{64}"', tag), tag
        assert 'crossorigin="anonymous"' in tag
        assert re.search(r"@\d+\.\d+\.\d+/", tag), tag  # exact version
    assert 'http-equiv="Content-Security-Policy"' in html
    assert '<html lang="en">' in html and 'name="viewport"' in html


def test_every_element_id_used_by_the_script_exists():
    html, js = read("index.html"), read("app.js")
    used = set(re.findall(r'getElementById\("([^"]+)"\)', js))
    defined = set(re.findall(r'\bid="([^"]+)"', html))
    assert used and used <= defined, used - defined


def test_script_never_inserts_data_as_html():
    js = read("app.js")
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in js, sink
    # Leaflet tooltips render strings as HTML, so they always get text nodes.
    assert js.count(".bindTooltip(") == js.count(".bindTooltip(tip(")
    # The only HTML strings are map pins: a number or "?" and fixed class names.
    for html in re.findall(r"html: `([^`]*)`", js) + re.findall(r"html: '([^']*)'", js):
        assert set(re.findall(r"\$\{([^}]*)\}", html)) <= {
            "band",
            "label",
            'st.event.status === "ongoing" ? "ongoing" : "recent"',
        }, html


def test_map_attribution_and_about_section():
    html, js = read("index.html"), read("app.js")
    assert "openstreetmap.org/copyright" in js and "OpenStreetMap</a> contributors" in js
    assert "OpenStreetMap" in html
    assert 'id="about-heading"' in html


def test_site_text_is_english_only():
    cjk = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")
    for name in ("index.html", "style.css", "app.js"):
        assert not cjk.search(read(name)), name


def test_impact_is_never_color_only():
    """Every impact color class comes with a visible number or label in the script."""
    js = read("app.js")
    assert "Impact ${Math.round(ev.impact_score)}/100 (${IMPACT_TEXT[label]})" in js
    assert '"Impact: not enough data"' in js
    css = read("style.css")
    for band in ("severe", "high", "moderate", "low", "unknown"):
        assert f".impact-badge.impact-{band}" in css


def test_last_update_reflects_data_collection_not_page_build():
    """The page is rebuilt from stored data, so freshness must come from the pipeline run."""
    js = read("app.js")
    body = js[js.index("function renderStatus") : js.index("// ---------- event list")]
    assert "status.last_pipeline_run_utc" in body
    assert "generated_utc" not in body
