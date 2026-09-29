#!/usr/bin/env python3
"""
rcewl.py - Recursive CeWL-like wordlist generator.

Recursively crawls a site, extracts words from:
  - visible page text
  - <title>, meta tags, alt/title/placeholder/aria-* attributes
  - HTML comments (<!-- -->)
  - inline <script> JS comments (// and /* */) + identifiers/strings
  - external JS files (even offsite/CDN) - comments + identifiers + strings
  - external CSS files (even offsite) - comments + class/id names + words

Two kinds of entries are collected:
  1. clean words  (CeWL-like): [A-Za-z0-9'_-]+  e.g. FrameSave, admin123
  2. raw tokens WITH specials (password-style): e.g. FrameSave!19,
     midnight-matinee77#, Fr4m3$ave77, 7h3cur4t0r!, k33psakes!07
     Disable with --no-specials.

Usage:
  python3 rcewl.py https://target.com -d 3 --max-pages 500 -m 3 -o wordlist.txt --verbose
  python3 rcewl.py http://localhost:8000 -d 2 -o wordlist.txt --lower --verbose
  python3 rcewl.py https://target.com -o wordlist.txt --no-specials   # CeWL-like clean only

Requires: pip install requests beautifulsoup4
"""

import argparse
import re
import sys
import time
from collections import deque
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup, Comment

# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------

# JS comments
RE_JS_LINE_COMMENT = re.compile(r"//[^\n]*")
RE_JS_BLOCK_COMMENT = re.compile(r"/\*[\s\S]*?\*/")
# CSS comments
RE_CSS_COMMENT = re.compile(r"/\*[\s\S]*?\*/")
# CSS class/id selectors: .my-class, #my-id
RE_CSS_SELECTOR = re.compile(r"[.#]([A-Za-z][A-Za-z0-9_-]*)")
# JS string literals (single, double, backtick) - crude but effective
RE_JS_STRING = re.compile(r"""(?:"([^"\\\n]{1,200})"|'([^'\\\n]{1,200})'|`([^`\\]{1,200})`)""")
# camelCase splitter
RE_CAMEL_SPLIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def build_word_regex(include_numbers: bool):
    if include_numbers:
        # start with alnum, then allow alnum + ' _ -
        return re.compile(r"[A-Za-z0-9][A-Za-z0-9'_-]*")
    else:
        return re.compile(r"[A-Za-z][A-Za-z'_-]*")


DEFAULT_SPECIALS = "!@#$%^&*+=:?./~"
# chars stripped from the edges of raw tokens. NOTE: !@#$%^&*-_+=?~ are
# deliberately NOT stripped so passwords like foo! / bar# / a$b survive.
RAW_STRIP = " \t\n\r\"'()[]{}<>,;:.`\\/"
MAX_RAW_LEN = 64


def parse_args():
    p = argparse.ArgumentParser(
        description="Recursive CeWL-like crawler: words from pages + comments + JS/CSS -> wordlist",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("url", help="Starting URL (e.g. https://target.com)")
    p.add_argument("-d", "--depth", type=int, default=3,
                   help="Max crawl depth. 0 = start page only, 1 = +1 link hop, etc.")
    p.add_argument("--max-pages", type=int, default=500,
                   help="Max HTML pages to crawl")
    p.add_argument("-m", "--min-len", type=int, default=3,
                   help="Minimum word length to keep")
    p.add_argument("-o", "--output", default="wordlist.txt",
                   help="Output wordlist file")
    p.add_argument("--lower", action="store_true", default=False,
                   help="Lowercase all words (default: preserve case for password lists)")
    p.add_argument("--include-numbers", action="store_true", default=True,
                   help="Keep alphanumeric words like admin123 (default ON)")
    p.add_argument("--no-numbers", action="store_true", default=False,
                   help="Drop any word containing a digit")
    p.add_argument("--allow-offsite", action="store_true", default=False,
                   help="Follow <a> links to other domains (default: same-domain only)")
    p.add_argument("--no-js-css", action="store_true", default=False,
                   help="Do NOT fetch/parse external JS & CSS (default: fetch them, even offsite/CDN)")
    p.add_argument("--no-comments", action="store_true", default=False,
                   help="Ignore HTML + JS/CSS comments")
    p.add_argument("--no-meta", action="store_true", default=False,
                   help="Ignore meta/title/alt attributes")
    p.add_argument("--no-split", action="store_true", default=False,
                   help="Do NOT split camelCase/snake-case/kebab-case identifiers into sub-words")
    p.add_argument("--no-specials", action="store_true", default=False,
                   help="Disable password-style raw tokens with !@#$%% etc (default: keep them)")
    p.add_argument("--special-chars", default=DEFAULT_SPECIALS,
                   help="Which special chars to preserve in raw tokens (default: %(default)s)")
    p.add_argument("--timeout", type=int, default=10, help="Request timeout seconds")
    p.add_argument("--delay", type=float, default=0.2, help="Delay between requests (politeness)")
    p.add_argument("--max-size", type=int, default=2 * 1024 * 1024,
                   help="Max bytes to download per URL")
    p.add_argument("--insecure", action="store_true", default=False,
                   help="Disable TLS verification (-k)")
    p.add_argument("--user-agent", default="rcewl/1.0 (+recursive-cewl)",
                   help="Custom User-Agent")
    p.add_argument("-v", "--verbose", action="store_true", default=False,
                   help="Verbose per-URL logging")
    return p.parse_args()


def normalize_url(url: str) -> str:
    """Strip fragment, drop empty query edge cases, return canonical string."""
    try:
        parts = urlparse(url)
        # remove fragment
        parts = parts._replace(fragment="")
        # normalize: lower scheme+netloc, remove default ports, strip trailing / except root
        scheme = parts.scheme.lower()
        netloc = parts.netloc.lower()
        if (scheme == "http" and netloc.endswith(":80")):
            netloc = netloc[:-3]
        if (scheme == "https" and netloc.endswith(":443")):
            netloc = netloc[:-4]
        path = parts.path or "/"
        return urlunparse((scheme, netloc, path, parts.params, parts.query, ""))
    except Exception:
        return url.split("#")[0]


def is_same_domain(url: str, base_netloc: str) -> bool:
    try:
        return urlparse(url).netloc.lower() == base_netloc.lower()
    except Exception:
        return False


def is_html_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    # skip obvious binaries
    for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico",
                ".pdf", ".zip", ".gz", ".mp4", ".mp3", ".avi", ".mov",
                ".woff", ".woff2", ".ttf", ".eot", ".otf"):
        if path.endswith(ext):
            return False
    return True


# ---------------------------------------------------------------------------
# Word handling
# ---------------------------------------------------------------------------

class WordCollector:
    def __init__(self, min_len=3, do_lower=False, include_numbers=True, drop_numbers=False,
                 split_ids=True, keep_specials=True, special_chars=DEFAULT_SPECIALS):
        self.min_len = min_len
        self.do_lower = do_lower
        self.split_ids = split_ids
        self.drop_numbers = drop_numbers
        self.keep_specials = keep_specials
        self.special_chars = special_chars or ""
        self.special_set = set(self.special_chars)
        self.regex = build_word_regex(include_numbers and not drop_numbers)
        # raw token: alnum + clean chars + specials, bounded length.
        # min_len is applied dynamically; max avoids URL garbage.
        # NOTE: literal '-' goes LAST in the class to avoid a bad range like _-!.
        allowed_core = "A-Za-z0-9'_" + re.escape(self.special_chars)
        self.raw_regex = re.compile(r"[" + allowed_core + r"-]{" + str(min_len) + r"," + str(MAX_RAW_LEN) + r"}")
        self.words = set()

    def _keep(self, token: str):
        token = token.strip("'_-")
        if len(token) < self.min_len:
            return
        if self.drop_numbers and any(c.isdigit() for c in token):
            return
        if self.do_lower:
            token = token.lower()
        if len(token) < self.min_len:
            return
        self.words.add(token)
        # split compound identifiers into sub-words (adminPanel -> admin + Panel)
        if self.split_ids and (("-" in token) or ("_" in token) or RE_CAMEL_SPLIT.search(token)):
            parts = RE_CAMEL_SPLIT.split(token)
            flat = []
            for part in parts:
                flat.extend(re.split(r"[-_]+", part))
            for sub in flat:
                sub = sub.strip("'_-")
                if len(sub) >= self.min_len:
                    if self.drop_numbers and any(c.isdigit() for c in sub):
                        continue
                    self.words.add(sub.lower() if self.do_lower else sub)

    def _keep_raw(self, token: str):
        """Keep a password-style token WITH specials, e.g. FrameSave!19."""
        token = token.strip(RAW_STRIP)
        if len(token) < self.min_len or len(token) > MAX_RAW_LEN:
            return
        # must start with alnum: skips CSS/JS noise like #fff, @media,
        # *::after, !variable, -50%, /about/, :hover
        # (real passwords almost always start with a letter/digit)
        if not token[0].isalnum():
            return
        # must contain at least one alnum (skip lone !!! / ###)
        if not any(c.isalnum() for c in token):
            return
        # must contain at least one true special so we don't just
        # duplicate clean words (clean pass already handles those)
        if not any(c in self.special_set for c in token):
            return
        # skip URLs and paths: http://, https://, //cdn, /about/
        if "://" in token:
            return
        # skip CSS numeric noise: 50%, 100%, 0px, 1.5em, #fff handled above
        if re.fullmatch(r"[0-9.,%]+", token):
            return
        if re.fullmatch(r"[0-9.]+(px|em|rem|%|vh|vw|pt|s|ms)", token):
            return
        if re.fullmatch(r"#[0-9a-fA-F]{3,8}", token):
            return
        if self.drop_numbers and any(c.isdigit() for c in token):
            return
        if self.do_lower:
            token = token.lower()
        self.words.add(token)

    def feed_text(self, text: str):
        if not text:
            return
        for m in self.regex.finditer(text):
            self._keep(m.group(0))
        if self.keep_specials:
            for m in self.raw_regex.finditer(text):
                self._keep_raw(m.group(0))

    def __len__(self):
        return len(self.words)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def extract_js_words(js_text: str, collector: WordCollector, with_comments: bool):
    if not js_text:
        return
    if with_comments:
        for m in RE_JS_BLOCK_COMMENT.finditer(js_text):
            collector.feed_text(m.group(0))
        for m in RE_JS_LINE_COMMENT.finditer(js_text):
            # avoid matching :// in URLs twice - still fine, words extracted anyway
            collector.feed_text(m.group(0))
    # string literals often hold interesting tokens (passwords, keys, paths)
    for m in RE_JS_STRING.finditer(js_text):
        for g in m.groups():
            if g:
                collector.feed_text(g)
    # identifiers / remaining code
    collector.feed_text(js_text)


def extract_css_words(css_text: str, collector: WordCollector, with_comments: bool):
    if not css_text:
        return
    if with_comments:
        for m in RE_CSS_COMMENT.finditer(css_text):
            collector.feed_text(m.group(0))
    # class/id names
    for m in RE_CSS_SELECTOR.finditer(css_text):
        collector.feed_text(m.group(1))
    # remaining words (font names, etc.)
    collector.feed_text(css_text)


def extract_from_html(html: str, collector: WordCollector, with_comments: bool, with_meta: bool):
    """Extract words from HTML doc. Returns (links, js_urls, css_urls, inline_js, inline_css_texts)."""
    soup = BeautifulSoup(html, "html.parser")

    # remove script/style/noscript from visible-text extraction (handled separately)
    scripts = soup.find_all("script")
    styles = soup.find_all("style")
    inline_js = []
    for s in scripts:
        if not s.get("src"):
            if s.string:
                inline_js.append(s.string)
            elif s.get_text():
                inline_js.append(s.get_text())
    inline_css = [st.get_text() for st in styles if st.get_text()]

    for tag in scripts + styles:
        tag.decompose()

    # 1. visible text
    visible = soup.get_text(" ")
    collector.feed_text(visible)

    # 2. HTML comments
    if with_comments:
        for c in soup.find_all(string=lambda t: isinstance(t, Comment)):
            collector.feed_text(str(c))

    # 3. meta / attributes
    if with_meta:
        # <title>
        if soup.title and soup.title.string:
            collector.feed_text(soup.title.string)
        # meta description/keywords/author/og/twitter
        for meta in soup.find_all("meta"):
            for attr in ("content", "value"):
                v = meta.get(attr)
                if v:
                    collector.feed_text(v)
        # alt, title, placeholder, aria-label, aria-labelledby, value, label
        for tag in soup.find_all(attrs={"alt": True}):
            collector.feed_text(tag.get("alt", ""))
        for tag in soup.find_all(attrs={"title": True}):
            # avoid double-counting <title> element, but harmless
            if tag.name != "title":
                collector.feed_text(tag.get("title", ""))
        for tag in soup.find_all(attrs={"placeholder": True}):
            collector.feed_text(tag.get("placeholder", ""))
        for tag in soup.find_all(attrs={"aria-label": True}):
            collector.feed_text(tag.get("aria-label", ""))
        for tag in soup.find_all("input"):
            v = tag.get("value", "")
            if v:
                collector.feed_text(v)

    # 4. inline JS / CSS
    for js in inline_js:
        extract_js_words(js, collector, with_comments)
    for css in inline_css:
        extract_css_words(css, collector, with_comments)

    # 5. link discovery
    links, js_urls, css_urls = [], [], []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href:
            continue
        # skip fragments, mailto/tel/js and placeholder templates like [LANTERN_FORUM_URL]
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        if "[" in href or "]" in href:
            continue
        links.append(href)
    # NOTE: we decomposed scripts/styles, so re-parse for src/href discovery:
    soup2 = BeautifulSoup(html, "html.parser")
    for s in soup2.find_all("script", src=True):
        src = s["src"].strip()
        if src and not src.startswith(("data:", "javascript:")):
            js_urls.append(src)
    for l in soup2.find_all("link", href=True):
        href = l["href"].strip()
        rel = " ".join(l.get("rel", [])).lower()
        ltype = (l.get("type", "") or "").lower()
        as_attr = (l.get("as", "") or "").lower()
        if href and not href.startswith("data:"):
            if "stylesheet" in rel or href.lower().endswith(".css") or "css" in ltype or as_attr == "style":
                css_urls.append(href)
            elif href.lower().endswith(".js"):
                js_urls.append(href)
    return links, js_urls, css_urls


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch(session: requests.Session, url: str, timeout: int, verify: bool, max_size: int):
    try:
        r = session.get(url, timeout=timeout, verify=verify, stream=True,
                        headers={"Accept": "text/html,application/xhtml+xml,text/css,application/javascript,*/*;q=0.8"})
        ctype = r.headers.get("Content-Type", "").lower()
        buf = b""
        for chunk in r.iter_content(chunk_size=8192):
            buf += chunk
            if len(buf) > max_size:
                break
        try:
            text = buf.decode(r.encoding or "utf-8", errors="ignore")
        except Exception:
            text = buf.decode("utf-8", errors="ignore")
        return text, ctype, r.status_code, None
    except requests.RequestException as e:
        return None, "", 0, str(e)


# ---------------------------------------------------------------------------
# Crawl
# ---------------------------------------------------------------------------

def crawl(args) -> WordCollector:
    start = normalize_url(args.url)
    if not urlparse(start).scheme.startswith("http"):
        print(f"[!] URL must start with http:// or https:// (got: {args.url})", file=sys.stderr)
        sys.exit(1)

    base_netloc = urlparse(start).netloc
    drop_numbers = args.no_numbers
    include_numbers = args.include_numbers and not drop_numbers

    collector = WordCollector(min_len=args.min_len, do_lower=args.lower,
                              include_numbers=include_numbers,
                              drop_numbers=drop_numbers,
                              split_ids=not args.no_split,
                              keep_specials=not args.no_specials,
                              special_chars=args.special_chars)

    session = requests.Session()
    session.headers.update({"User-Agent": args.user_agent})
    verify = not args.insecure
    fetch_assets = not args.no_js_css
    with_comments = not args.no_comments
    with_meta = not args.no_meta

    queue = deque([(start, 0)])
    visited_pages = set()
    visited_assets = set()
    pages_crawled = 0
    asset_count = 0

    print(f"[*] rcewl: start={start} depth={args.depth} max-pages={args.max_pages} "
          f"min-len={args.min_len} lower={args.lower} numbers={include_numbers} "
          f"specials={'off' if args.no_specials else args.special_chars}")
    print(f"[*] assets(js/css even offsite)={'off' if args.no_js_css else 'on'} "
          f"comments={'off' if args.no_comments else 'on'} meta={'off' if args.no_meta else 'on'} "
          f"offsite-links={'on' if args.allow_offsite else 'off'}")

    while queue and pages_crawled < args.max_pages:
        url, depth = queue.popleft()
        norm = normalize_url(url)
        if norm in visited_pages or norm in visited_assets:
            continue
        if depth > args.depth:
            continue
        if not is_html_url(norm):
            continue

        visited_pages.add(norm)
        if args.verbose:
            print(f"[+] [{pages_crawled+1}] depth={depth} GET {norm}")
        else:
            print(f"[*] crawling ({pages_crawled+1}/{args.max_pages}) depth={depth}: {norm[:100]}",
                  end="\r", flush=True)

        text, ctype, status, err = fetch(session, norm, args.timeout, verify, args.max_size)
        if err:
            if args.verbose:
                print(f"    [!] fetch error: {err}")
            continue
        if status >= 400:
            if args.verbose:
                print(f"    [!] HTTP {status}")
            continue
        if "text/html" not in ctype and "text/plain" not in ctype and "xml" not in ctype and ctype:
            if args.verbose:
                print(f"    [-] skip non-html ({ctype})")
            continue

        pages_crawled += 1
        before = len(collector)

        try:
            links, js_urls, css_urls = extract_from_html(text, collector, with_comments, with_meta)
        except Exception as e:
            if args.verbose:
                print(f"    [!] parse error: {e}")
            continue

        if args.verbose:
            print(f"    [.] +{len(collector)-before} words (total {len(collector)}) "
                  f"links={len(links)} js={len(js_urls)} css={len(css_urls)}")

        # fetch assets (JS/CSS) - even offsite/CDN per user requirement
        if fetch_assets:
            for rel in js_urls + css_urls:
                absu = normalize_url(urljoin(norm, rel))
                if absu in visited_assets or absu in visited_pages:
                    continue
                visited_assets.add(absu)
                atext, actype, astatus, aerr = fetch(session, absu, args.timeout, verify, args.max_size)
                if aerr or astatus >= 400 or not atext:
                    if args.verbose:
                        print(f"    [-] asset skip {absu[:90]} ({aerr or astatus})")
                    continue
                asset_count += 1
                ab = len(collector)
                is_js = absu.lower().endswith(".js") or "javascript" in actype or rel in js_urls
                try:
                    if is_js:
                        extract_js_words(atext, collector, with_comments)
                    else:
                        extract_css_words(atext, collector, with_comments)
                except Exception as e:
                    if args.verbose:
                        print(f"    [!] asset parse error: {e}")
                    continue
                if args.verbose:
                    print(f"    [.] asset +{len(collector)-ab} words: {absu[:90]}")
                if args.delay:
                    time.sleep(args.delay / 2)

        # enqueue child links
        if depth < args.depth:
            for rel in links:
                absu = urljoin(norm, rel)
                absn = normalize_url(absu)
                pu = urlparse(absn)
                if pu.scheme not in ("http", "https"):
                    continue
                if absn in visited_pages:
                    continue
                if not args.allow_offsite and not is_same_domain(absn, base_netloc):
                    continue
                if not is_html_url(absn):
                    continue
                queue.append((absn, depth + 1))

        if args.delay:
            time.sleep(args.delay)

    print(f"\n[*] done: pages={pages_crawled} assets(js/css)={asset_count} unique_words={len(collector)}")
    return collector


def main():
    args = parse_args()
    collector = crawl(args)
    words = sorted(collector.words)
    try:
        with open(args.output, "w", encoding="utf-8") as f:
            for w in words:
                f.write(w + "\n")
        print(f"[*] wrote {len(words)} words -> {args.output}")
    except OSError as e:
        print(f"[!] cannot write {args.output}: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
