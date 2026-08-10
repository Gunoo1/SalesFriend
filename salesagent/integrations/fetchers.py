"""Page/PDF fetching (timMtesting t07/t07b/t08 recipes):
- curl_cffi chrome124 impersonation with requests fallback
- pdfplumber for PDFs (visible_text on PDF bytes saves garbage)
- DuckDuckGo HTML resolver: answers the FIRST query then 202-bot-challenges
  fast follow-ups -> 0/20/45s retry ladder; unwrap uddg= redirects; prefer .gov
- 'blocked' is a DISTINCT status: it means UNKNOWN, never 'not found'
"""
from __future__ import annotations

import io
import re
import time
import urllib.parse

TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
HTML_RE = re.compile(r"<[^>]+>")


def visible_text(html: str) -> str:
    txt = TAG_RE.sub(" ", html)
    txt = HTML_RE.sub(" ", txt)
    txt = txt.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", txt).strip()


def _get(url: str, timeout: int = 45, verify: bool = True):
    try:
        from curl_cffi import requests as cr
        return cr.get(url, impersonate="chrome124", timeout=timeout,
                      verify=verify)
    except ImportError:
        import requests
        return requests.get(url, timeout=timeout, verify=verify,
                            headers={"User-Agent": "Mozilla/5.0"})


def fetch(url: str, timeout: int = 45) -> dict:
    """{status: ok|blocked|error|empty, text, content_type, http_status}"""
    try:
        r = _get(url, timeout)
    except Exception as e:
        msg = str(e)
        if "SSL" in msg or "TLS" in msg or "handshake" in msg.lower():
            try:
                r = _get(url, timeout, verify=False)
            except Exception as e2:
                return {"status": "blocked", "text": "",
                        "note": f"TLS refused even unverified: {type(e2).__name__}"}
        else:
            return {"status": "error", "text": "", "note": f"{type(e).__name__}: {msg[:120]}"}
    body = r.content or b""
    ctype = str(r.headers.get("content-type", "")).lower()
    if r.status_code in (403, 429, 202):
        return {"status": "blocked", "text": "", "http_status": r.status_code,
                "note": "anti-bot response — result is UNKNOWN, not absent"}
    if r.status_code != 200:
        return {"status": "error", "text": "", "http_status": r.status_code}
    if body[:5] == b"%PDF-" or "pdf" in ctype:
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(body)) as pdf:
                text = "\n".join((p.extract_text() or "")
                                 for p in pdf.pages[:40])
            return {"status": "ok" if text.strip() else "empty",
                    "text": text, "content_type": "pdf"}
        except Exception as e:
            return {"status": "error", "text": "",
                    "note": f"pdf extract failed: {type(e).__name__}"}
    text = visible_text(body.decode("utf-8", errors="replace"))
    return {"status": "ok" if text else "empty", "text": text,
            "content_type": "html"}


def ddg_resolve(query: str, prefer_gov: bool = True) -> list[dict]:
    """DuckDuckGo HTML search -> [{title, url}] with the 202 retry ladder."""
    import requests
    results: list[dict] = []
    for wait in (0, 20, 45):
        if wait:
            time.sleep(wait)
        r = requests.post("https://html.duckduckgo.com/html/",
                          data={"q": query},
                          headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        if r.status_code == 200 and "result__a" in r.text:
            for m in re.finditer(
                    r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                    r.text):
                href, title = m.group(1), visible_text(m.group(2))
                if "uddg=" in href:
                    q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                    href = urllib.parse.unquote(q.get("uddg", [href])[0])
                results.append({"title": title, "url": href})
            break
    if prefer_gov:
        results.sort(key=lambda x: 0 if (".gov" in x["url"] or ".us" in x["url"])
                     else 1)
    return results[:10]


def keyword_windows(text: str, keywords: list[str], span: int = 600,
                    cap: int = 14000) -> str:
    """Keyword-centered ±span windows with merged overlaps — 'fix the input,
    not the prompt' (t09b lesson: schedules live deep in long pages)."""
    hits = []
    low = text.lower()
    for kw in keywords:
        start = 0
        while True:
            i = low.find(kw.lower(), start)
            if i < 0:
                break
            hits.append((max(0, i - span), min(len(text), i + span)))
            start = i + 1
    if not hits:
        return text[:cap]
    hits.sort()
    merged = [list(hits[0])]
    for a, b in hits[1:]:
        if a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    out = " […] ".join(text[a:b] for a, b in merged)
    return out[:cap]
