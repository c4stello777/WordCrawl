# WordCrawl

Recursive website wordlist generator. Crawls a site and builds a password / bug-hunting wordlist from page text, meta tags, HTML comments, JS and CSS.

![Python](https://img.shields.io/badge/Python-3.8%2B-blue) ![License](https://img.shields.io/badge/License-MIT-green)

## Install

```bash
git clone https://github.com/c4stello777/WordCrawl.git
cd WordCrawl
pip install -r requirements.txt
```

## Usage

```bash
python wordcrawl.py https://target.com -d 3 --max-pages 500 -m 3 -o wordlist.txt --verbose
python wordcrawl.py http://localhost:8000 -d 2 -o wordlist.txt --lower --verbose
python wordcrawl.py https://target.com -o wordlist.txt --no-specials
```

| Option | Purpose |
|---|---|
| `-d, --depth` | Crawl depth (0 = start page only) |
| `--max-pages` | Max pages to crawl (default 500) |
| `-m, --min-len` | Min word length (default 3) |
| `-o, --output` | Output file (default wordlist.txt) |
| `--lower` | Lowercase all words |
| `--no-specials` | Clean words only, no password-style tokens |
| `--allow-offsite` | Follow links to other domains |
| `--no-js-css` | Skip external JS/CSS files |
| `-v, --verbose` | Verbose logging |

Full options: `python wordcrawl.py -h`

## Output

Clean words (`adminPanel`, `dashboard`) plus password-style tokens (`FrameSave!19`, `midnight-matinee77#`) from comments, scripts, and styles. One word per line, sorted unique.

## Legal

Use only on sites you own or are authorized to test.
