"""Package the Portal shell and content-versioned, same-origin assets."""

from functools import lru_cache
from hashlib import sha256
from importlib.resources import files

STYLES = ("vis-timeline-8.5.4.css", "portal.css", "portal-design.css")
SCRIPTS = ("vis-timeline-8.5.4.min.js", "portal.js")


@lru_cache(maxsize=1)
def portal_assets():
    """An exact allowlist prevents the asset endpoint from exposing files."""
    root = files("robotenon.web").joinpath("static")
    result = {}
    for name in (*STYLES, *SCRIPTS):
        payload = root.joinpath(name).read_bytes()
        version = sha256(payload).hexdigest()[:16]
        url = f"/assets/{version}/{name}"
        media_type = "text/css" if name.endswith(".css") else "text/javascript"
        result[url] = (payload, media_type + "; charset=utf-8")
    return result


def render_page(*, inline=False):
    """Inline mode supports offline inspection; HTTP uses cached assets."""
    assets = portal_assets()
    if inline:
        styles = "<style>" + "\n".join(value[0].decode() for key, value in assets.items() if key.endswith(".css")) + "</style>"
        scripts = "<script>" + "\n".join(value[0].decode() for key, value in assets.items() if key.endswith(".js")) + "</script>"
    else:
        styles = "\n".join(f'<link rel="stylesheet" href="{url}">' for url in assets if url.endswith(".css"))
        scripts = "\n".join(f'<script defer src="{url}"></script>' for url in assets if url.endswith(".js"))
    brand = files("robotenon.web").joinpath("static", "tenon.svg").read_text(encoding="utf-8")
    return files("robotenon.web").joinpath("static", "portal.html").read_text(encoding="utf-8").replace("{{brand}}", brand).replace("{{style}}", styles).replace("{{script}}", scripts)
