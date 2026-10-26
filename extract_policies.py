# Extract AI provider usage policies via the Wayback Machine.
# to run: python  extract_policies.py

# Prints out table of extracted policy information - just used to confirm we can extract policy

import csv
from pathlib import Path

import requests
from bs4 import BeautifulSoup

PROVIDERS = {
    "openai": ["openai.com/policies/usage-policies", "beta.openai.com/docs/usage-policies"],
    "anthropic": ["www.anthropic.com/legal/aup", "console.anthropic.com/legal/aup"],
    "google": ["policies.google.com/terms/generative-ai/use-policy"],
    "meta": ["ai.meta.com/llama/use-policy", "www.llama.com/llama3/use-policy"],
    "xai": ["x.ai/legal/acceptable-use-policy"],
}

DATA_DIR = Path(__file__).parent / "data"


def fetch(url, params=None):
    # Fail so an archive error page is never saved as a policy.
    resp = requests.get(url, params=params, timeout=60)
    resp.raise_for_status()
    return resp


def list_snapshots(url):
    params = {"url": url, "output": "json", "fl": "timestamp,original",
              "filter": "statuscode:200", "collapse": "digest"}
    rows = fetch("https://web.archive.org/cdx/search/cdx", params).json()
    return rows[1:]


def extract_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer"]):
        tag.decompose()
    root = soup.find("main") or soup.body or soup
    return "\n".join(line.strip() for line in root.get_text("\n").splitlines() if line.strip())


def main():
    (DATA_DIR / "text").mkdir(parents=True, exist_ok=True)
    index = []

    for provider, urls in PROVIDERS.items():
        snaps = sorted(s for url in urls for s in list_snapshots(url))
        index += [[provider, *s] for s in snaps]

        timestamp, original = snaps[-1]
        html = fetch(
            f"https://web.archive.org/web/{timestamp}id_/{original}").text
        text = extract_text(html)
        (DATA_DIR / "text" /
         f"{provider}_{timestamp}.txt").write_text(text, encoding="utf-8")
        print(f"{provider:<10} {len(snaps):>5} snapshots  first {snaps[0][0][:8]}  "
              f"latest {timestamp[:8]}  {len(text.split())} words")

    with open(DATA_DIR / "snapshot_index.csv", "w", newline="") as f:
        csv.writer(f).writerows(
            [["provider", "timestamp", "original"], *index])


if __name__ == "__main__":
    main()
