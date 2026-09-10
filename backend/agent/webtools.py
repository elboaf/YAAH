"""Web tools: search, fetch, and image download for the agent.

Ported from the Drip harness (Desktop/Drip/driptoken/tools.py), minus the
third-party CORS-proxy fallback. Both search and fetch prefer a headless
Chrome/Edge route: a real browser TLS fingerprint that executes JS, with a
persistent cookie profile at LOCALAPPDATA/YAAH/chrome-profile so repeat
visits look like a returning user and bot challenges (DuckDuckGo anomaly
modal, Cloudflare interstitials) are usually defeated on the built-in
retry. Direct HTTP is the fallback route.
"""
import asyncio
import os
import subprocess


_WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Text fragments that mark a fetched page as a bot-wall, not real content.
_BLOCK_MARKERS = ("error page", "access denied", "pardon our interruption",
                  "just a moment", "are you a robot", "unusual traffic")


def _http_get(url, timeout=15, data=None):
    import gzip
    import zlib
    import urllib.request
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": _WEB_UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(2_000_000)
        charset = resp.headers.get_content_charset() or "utf-8"
    # Some CDNs (e.g. Fastly) serve gzip even when not requested — sniff
    # and decompress by magic bytes instead of trusting content-encoding.
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    elif raw[:1] == b"\x78":
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            pass
    return raw.decode(charset, errors="replace")


def strip_html(html):
    """Very small HTML-to-text: drop scripts/styles, unwrap tags, squash
    whitespace and decode the common entities."""
    import html as html_mod
    import re
    html = re.sub(r"(?is)<(script|style|noscript|svg|head)\b.*?</\1>", " ", html)
    html = re.sub(r"(?i)<(br|/p|/div|/li|/h[1-6]|/tr)[^>]*>", "\n", html)
    text = re.sub(r"<[^>]+>", " ", html)
    text = html_mod.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def _find_chrome():
    """Path to an installed Chrome/Edge, or None."""
    import shutil
    for exe in ("chrome", "msedge"):
        found = shutil.which(exe)
        if found:
            return found
    cands = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    for c in cands:
        if os.path.exists(c):
            return c
    return None


def _browser_get_sync(url, timeout=30):
    """Fetch a URL through headless Chrome; return the rendered DOM as
    HTML. Retries once on an empty DOM (the first hit on a fresh profile
    plants challenge cookies; the retry lands as a returning user)."""
    chrome = _find_chrome()
    if not chrome:
        raise RuntimeError("no Chrome/Edge installed for headless fetch")
    profile = os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
        "YAAH", "chrome-profile")
    os.makedirs(profile, exist_ok=True)
    cmd = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--user-data-dir=" + profile,
        "--disable-blink-features=AutomationControlled",
        "--user-agent=" + _WEB_UA,
        "--virtual-time-budget=8000",
        "--timeout=%d" % (timeout * 1000),
        "--dump-dom",
        url,
    ]
    last_err = None
    for attempt in range(2):
        try:
            proc = subprocess.run(cmd, capture_output=True,
                                  timeout=timeout + 5, errors="replace",
                                  creationflags=(
                                      subprocess.CREATE_NO_WINDOW
                                      if os.name == "nt" else 0))
            html = proc.stdout
            if html and "<html" in html.lower():
                return html
            last_err = RuntimeError("headless Chrome returned no DOM")
        except subprocess.TimeoutExpired as e:
            last_err = e
    raise last_err or RuntimeError("headless Chrome failed")


async def _browser_get(url, timeout=30):
    # Chrome startup + page render are blocking subprocess calls; keep
    # them off the event loop.
    return await asyncio.to_thread(_browser_get_sync, url, timeout)


# ---------------------------------------------------------------- search

async def web_search(query, max_results=8, workspace=None):
    """Search the web via DuckDuckGo's free HTML endpoint (no API key).
    Returns numbered results: title, URL, snippet."""
    import re
    import urllib.parse
    results = []
    html = None
    # Primary route: headless Chrome (bypasses DuckDuckGo's bot challenge,
    # which flags direct HTTP requests from some IPs).
    try:
        html = await _browser_get(
            "https://html.duckduckgo.com/html/?q="
            + urllib.parse.quote(query))
    except Exception:
        html = None
    # Fallback: direct POST request (original route).
    if not html or "anomaly-modal" in html:
        body = urllib.parse.urlencode({"q": query, "kl": "wt-wt"}).encode()
        try:
            html = await asyncio.to_thread(
                _http_get, "https://html.duckduckgo.com/html/", 15, body)
        except Exception:
            html = None
    if not html:
        return (f"No results found for '{query}' (search request failed; "
                f"try again or rephrase).")

    # result blocks: <a class="result__a" href="//duckduckgo.com/l/?uddg=<url>">
    for m in re.finditer(
            r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            html, re.S):
        href, title = m.group(1), strip_html(m.group(2))
        uddg = re.search(r"uddg=([^&]+)", href)
        url = urllib.parse.unquote(uddg.group(1)) if uddg else href
        if not url.startswith("http"):
            url = "https://" + url.lstrip("/")
        results.append([title, url, ""])
        if len(results) >= max_results:
            break
    # snippets come after each title link
    snips = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, re.S)
    for i, s in enumerate(snips[:len(results)]):
        results[i][2] = strip_html(s)
    if not results:
        return (f"No results found for '{query}' (DuckDuckGo may have "
                f"challenged the request; try again or rephrase).")
    out = [f"WEB RESULTS for '{query}':"]
    for i, (title, url, snip) in enumerate(results, 1):
        out.append(f"{i}. {title}\n   {url}\n   {snip[:200]}")
    return "\n".join(out)


# ---------------------------------------------------------------- fetch

async def web_fetch(url, max_chars=20000, workspace=None):
    """Fetch a URL and return its readable text (HTML stripped). Primary
    route is headless Chrome (real browser fingerprint, renders JS) so
    bot-blocked sites usually just work; direct HTTP is the fallback. If
    both fail, returns a hint to use web_search snippets."""
    import re
    if not re.match(r"^https?://", url):
        url = "https://" + url
    via = "Chrome"
    text = None
    html = None
    try:
        html = await _browser_get(url)
        text = strip_html(html)
        if text and any(m in text[:600].lower() for m in _BLOCK_MARKERS):
            text = None  # bot-wall page; try the next route
    except Exception:
        pass
    if text is None:
        try:
            html = await asyncio.to_thread(_http_get, url)
            text = strip_html(html)
            via = "direct"
        except Exception:
            text = None
    if text is None:
        return (f"[BLOCKED] {url} could not be fetched (bot protection "
                f"defeated both the headless browser and direct HTTP "
                f"routes). Use web_search instead and work from snippets, "
                f"or try a different source for the same info.")
    head = f"PAGE: {url}"
    if via != "Chrome":
        head += f" (via {via} — headless Chrome failed for this URL)"
    head += "\n"
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html or "")
    if m:
        head += f"TITLE: {strip_html(m.group(1))}\n"
    head += "=" * 50 + "\n"
    # surface image URLs so the model can call view_image on one
    import urllib.parse as _up
    imgs = []
    for im in re.finditer(
            r'(?is)<img[^>]+(?:data-src|src)\s*=\s*["\']([^"\']+)["\']', html):
        src = im.group(1).strip()
        if src.startswith("data:") or src.endswith(".svg") or ".svg?" in src:
            continue
        if src.startswith("//"):
            src = "https:" + src
        elif not src.startswith("http"):
            src = _up.urljoin(url, src)
        if src not in imgs:
            imgs.append(src)
        if len(imgs) >= 5:
            break
    if imgs:
        head += ("IMAGES ON PAGE (use view_image to see one):\n"
                 + "\n".join("  " + u for u in imgs) + "\n")
    budget = max_chars - len(head)
    if len(text) > budget:
        text = text[:budget] + "\n... (truncated)"
    return head + text


# ---------------------------------------------------------------- images

async def view_image(url, workspace=None):
    """Download an image from a URL, store it on disk, and attach it so a
    vision-capable model can see it. The returned dict carries the stored
    image's rel path; the agent loop turns that into an image_url part."""
    import base64
    import re
    import urllib.request

    from backend.agent.imagedata import save_bytes

    if not re.match(r"^https?://", url):
        url = "https://" + url
    req = urllib.request.Request(url, headers={
        "User-Agent": _WEB_UA,
        "Accept": "image/*,*/*;q=0.8",
    })

    def _download():
        with urllib.request.urlopen(req, timeout=20) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
            raw = resp.read(6_000_001)
        return ctype, raw

    ctype, raw = await asyncio.to_thread(_download)
    if len(raw) > 5_000_000:
        return {"error": "image larger than 5 MB", "url": url}
    if not ctype.startswith("image/"):
        # some servers serve octet-stream; sniff magic bytes as a fallback
        if raw[:8] == b"\x89PNG\r\n\x1a\n":
            ctype = "image/png"
        elif raw[:3] == b"\xff\xd8\xff":
            ctype = "image/jpeg"
        elif raw[:6] in (b"GIF87a", b"GIF89a"):
            ctype = "image/gif"
        else:
            return {"error": f"not an image (content-type {ctype!r})",
                    "url": url}
    ctype = ctype.split("/")[1].lower()
    rel = save_bytes(raw, "jpg" if ctype == "jpeg" else ctype, subdir="agent")
    return {
        "image": rel,
        "url": url,
        "note": ("Image downloaded from the URL above and attached below "
                 "for viewing."),
    }
