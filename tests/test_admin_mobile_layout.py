from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ADMIN_HTML = ROOT / "static" / "admin.html"


def test_admin_mobile_layout_reclaims_the_full_content_width():
    html = ADMIN_HTML.read_text(encoding="utf-8")

    assert "@media (max-width: 700px)" in html
    assert ".app { flex-direction: column; height: 100dvh; }" in html
    assert ".sidebar { width: 100%; min-width: 0;" in html
    assert ".main { width: 100%; min-width: 0; padding: 16px 14px; }" in html


def test_admin_mobile_layout_keeps_dense_surfaces_readable():
    html = ADMIN_HTML.read_text(encoding="utf-8")

    assert ".sessions-layout { flex-direction: column; height: auto; }" in html
    assert ".ct-item { grid-template-columns: 28px minmax(0, 1fr) auto;" in html
    assert "#ev-out, #int-out { overflow-x: auto;" in html
    assert ".live-msg { max-width: 100%; padding: 12px 14px; }" in html
