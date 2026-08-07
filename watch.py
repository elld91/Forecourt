#!/usr/bin/env python3
"""
Forecourt watcher (v2).

Three ingestion paths, one dashboard:
  1. eBay UK Browse API  -> structured used-listing search (cars, scooter, goods)
  2. RSS feeds           -> CamelCamelCamel price drops, HotUKDeals keyword deals
  3. Email ingest (IMAP) -> saved-search alerts forwarded from Facebook, Gumtree,
                            Preloved, Vinted etc. into one Gmail label

Everything is grouped into sections and written to docs/data.json.

Required env (only if you use eBay searches):
  EBAY_CLIENT_ID, EBAY_CLIENT_SECRET
Optional email ingest (the Facebook/Gumtree route):
  IMAP_HOST, IMAP_USER, IMAP_PASS, IMAP_FOLDER (default "forecourt")
Optional email alerts out:
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, ALERT_TO
"""

import base64
import datetime as dt
import email
import imaplib
import json
import os
import re
import smtplib
import sys
from email.header import decode_header
from email.mime.text import MIMEText
from pathlib import Path

import requests

try:
    import feedparser
except ImportError:
    feedparser = None

ROOT = Path(__file__).resolve().parent
SEARCHES_FILE = ROOT / "searches.json"
DATA_FILE = ROOT / "docs" / "data.json"
SEEN_FILE = ROOT / "state" / "seen.json"

OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
BROWSE_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
MARKETPLACE = "EBAY_GB"
RESULTS_PER_SEARCH = 50

PRICE_RE = re.compile(r"£\s?([0-9][0-9,]*(?:\.[0-9]{2})?)")
LINK_RE = re.compile(r"https?://[^\s\"'<>)]+")


# ---------- shared helpers ----------

def parse_price(text: str) -> float:
    m = PRICE_RE.search(text or "")
    if not m:
        return 0.0
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return 0.0


def score_goods(price: float, ref: float, is_new: bool):
    tags, headline, score = [], "", 45
    if ref and price:
        pct = (ref - price) / ref
        score = int(max(0, min(100, 50 + pct * 200)))
        if pct >= 0.15:
            tags.append("underpriced")
            headline = f"~{round(pct*100)}% below the usual price"
        elif pct >= 0.05:
            headline = f"~{round(pct*100)}% below usual"
        else:
            headline = "around usual price"
    if is_new:
        score = min(100, score + 12)
    return score, tags, headline


# ---------- eBay ----------

def get_token() -> str:
    cid = os.environ.get("EBAY_CLIENT_ID")
    secret = os.environ.get("EBAY_CLIENT_SECRET")
    if not cid or not secret:
        print("  ! eBay keys missing \u2013 skipping eBay searches.")
        return ""
    basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    resp = requests.post(
        OAUTH_URL,
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials",
              "scope": "https://api.ebay.com/oauth/api_scope"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def search_ebay(token: str, s: dict) -> list:
    filt = f"price:[{s['min_price']}..{s['max_price']}],priceCurrency:GBP"
    params = {"q": s["query"], "filter": filt, "limit": RESULTS_PER_SEARCH}
    if s.get("category_ids"):
        params["category_ids"] = str(s["category_ids"])
    headers = {"Authorization": f"Bearer {token}",
               "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE}
    try:
        r = requests.get(BROWSE_URL, headers=headers, params=params, timeout=30)
        r.raise_for_status()
    except requests.RequestException as exc:
        print(f"  ! '{s['name']}' failed: {exc}")
        return []
    return r.json().get("itemSummaries", []) or []


def text_of(item: dict) -> str:
    return " ".join(p for p in [item.get("title", ""),
                                item.get("shortDescription", "")] if p).lower()


def passes(item: dict, s: dict) -> bool:
    t = text_of(item)
    must = [m.lower() for m in s.get("must_include", [])]
    if must and not any(m in t for m in must):
        return False
    return not any(b.lower() in t for b in s.get("exclude", []))


def score_ebay(item: dict, s: dict):
    price = float(item.get("price", {}).get("value", 0) or 0)
    text = text_of(item)
    if s["type"] == "scooter":
        hits = [k for k in s.get("fault_keywords", []) if k in text]
        mx = float(s.get("max_price", 1) or 1)
        cheap = max(0.0, min(1.0, (mx - price) / mx)) if price else 0
        score = int(max(0, min(100, 30 + len(hits) * 18 + cheap * 25)))
        if hits:
            return score, ["named fault"], "fault noted: " + ", ".join(sorted(set(hits))[:3])
        return score, [], "no fault language in title \u2013 ask the seller"
    # car or goods both score against reference_price
    return score_goods(price, float(s.get("reference_price", 0) or 0),
                       item.get("itemId", "") not in SEEN)


def row_from_ebay(item: dict, s: dict, is_new: bool) -> dict:
    score, tags, headline = score_ebay(item, s)
    loc = item.get("itemLocation", {}) or {}
    return {
        "id": item.get("itemId", ""),
        "title": item.get("title", ""),
        "price": float(item.get("price", {}).get("value", 0) or 0),
        "url": item.get("itemWebUrl", ""),
        "image": (item.get("image", {}) or {}).get("imageUrl", ""),
        "source": "eBay",
        "location": ", ".join(filter(None, [loc.get("city"), loc.get("postalCode")])),
        "new": is_new, "score": score, "tags": tags, "headline": headline,
    }


# ---------- RSS feeds ----------

def rows_from_feed(f: dict) -> list:
    if feedparser is None:
        print("  ! feedparser not installed \u2013 skipping feeds.")
        return []
    url = f.get("url", "")
    if not url or url.startswith("PASTE_"):
        print(f"  - {f['name']}: no feed URL set yet, skipping.")
        return []
    try:
        parsed = feedparser.parse(url)
    except Exception as exc:  # noqa: BLE001
        print(f"  ! feed '{f['name']}' failed: {exc}")
        return []
    out = []
    for e in parsed.entries[:40]:
        eid = e.get("id") or e.get("link", "")
        if not eid:
            continue
        is_new = eid not in SEEN
        if is_new:
            SEEN[eid] = NOW
        title = e.get("title", "Untitled")
        price = parse_price(title + " " + e.get("summary", ""))
        score = 70 if is_new else 45
        tags = [f["tag_label"]] if f.get("tag_label") else []
        out.append({
            "id": eid, "title": title, "price": price,
            "url": e.get("link", ""), "image": "", "source": f.get("source", "Feed"),
            "location": "", "new": is_new, "score": score, "tags": tags,
            "headline": f.get("tag_label", ""),
        })
    return out


# ---------- email ingest (the Facebook / Gumtree route) ----------

def _decode(s):
    if not s:
        return ""
    out = []
    for part, enc in decode_header(s):
        out.append(part.decode(enc or "utf-8", "ignore") if isinstance(part, bytes) else part)
    return "".join(out)


def _body_text(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_payload(decode=True).decode("utf-8", "ignore")
                except Exception:  # noqa: BLE001
                    continue
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    return part.get_payload(decode=True).decode("utf-8", "ignore")
                except Exception:  # noqa: BLE001
                    continue
        return ""
    try:
        return msg.get_payload(decode=True).decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return ""


def source_from_sender(sender: str) -> str:
    s = sender.lower()
    for key, label in [("facebook", "Facebook Marketplace"), ("gumtree", "Gumtree"),
                       ("vinted", "Vinted"), ("preloved", "Preloved"),
                       ("enviromate", "Enviromate"), ("ebay", "eBay alert")]:
        if key in s:
            return label
    return "Saved-search alert"


def rows_from_email() -> list:
    host = os.environ.get("IMAP_HOST")
    user = os.environ.get("IMAP_USER")
    pw = os.environ.get("IMAP_PASS")
    if not (host and user and pw):
        return []
    folder = os.environ.get("IMAP_FOLDER", "forecourt")
    since = (dt.datetime.utcnow() - dt.timedelta(days=4)).strftime("%d-%b-%Y")
    rows = []
    try:
        M = imaplib.IMAP4_SSL(host)
        M.login(user, pw)
        M.select(f'"{folder}"')
        typ, data = M.search(None, f'(SINCE "{since}")')
        ids = data[0].split()[-40:] if data and data[0] else []
        for num in ids:
            typ, msgdata = M.fetch(num, "(RFC822)")
            if typ != "OK" or not msgdata or not msgdata[0]:
                continue
            msg = email.message_from_bytes(msgdata[0][1])
            mid = msg.get("Message-ID", "") or f"email-{num.decode()}"
            is_new = mid not in SEEN
            if is_new:
                SEEN[mid] = NOW
            subject = _decode(msg.get("Subject", ""))
            sender = _decode(msg.get("From", ""))
            body = _body_text(msg)
            link_match = LINK_RE.search(body)
            rows.append({
                "id": mid, "title": subject or "New alert",
                "price": parse_price(subject + " " + body),
                "url": link_match.group(0) if link_match else "",
                "image": "", "source": source_from_sender(sender),
                "location": "", "new": is_new,
                "score": 65 if is_new else 40,
                "tags": ["alert"], "headline": source_from_sender(sender),
            })
        M.logout()
    except Exception as exc:  # noqa: BLE001
        print(f"  ! email ingest failed (non-fatal): {exc}")
    return rows


# ---------- orchestration ----------

SEEN = {}
NOW = dt.datetime.now(dt.timezone.utc).isoformat()


def main() -> None:
    global SEEN
    cfg = json.loads(SEARCHES_FILE.read_text())
    if SEEN_FILE.exists():
        SEEN = json.loads(SEEN_FILE.read_text())

    out, new_hits = [], []
    token = get_token() if cfg.get("ebay_searches") else ""

    for s in cfg.get("ebay_searches", []):
        if not token:
            break
        print(f"- [eBay] {s['name']}")
        items = []
        for it in search_ebay(token, s):
            if not passes(it, s):
                continue
            iid = it.get("itemId", "")
            is_new = iid not in SEEN
            row = row_from_ebay(it, s, is_new)
            if is_new and iid:
                SEEN[iid] = NOW
                new_hits.append((s["name"], row))
            items.append(row)
        items.sort(key=lambda r: (-r["score"], r["price"] or 9e9))
        out.append({"name": s["name"], "section": s.get("section", "Other"),
                    "count": len(items), "items": items})
        print(f"    {len(items)} matches")

    for f in cfg.get("feeds", []):
        print(f"- [feed] {f['name']}")
        items = rows_from_feed(f)
        for r in items:
            if r["new"]:
                new_hits.append((f["name"], r))
        items.sort(key=lambda r: (-r["score"], r["price"] or 9e9))
        out.append({"name": f["name"], "section": f.get("section", "Other"),
                    "count": len(items), "items": items})

    email_rows = rows_from_email()
    if email_rows:
        print(f"- [email] {len(email_rows)} alert(s)")
        for r in email_rows:
            if r["new"]:
                new_hits.append(("Saved-search alerts", r))
        email_rows.sort(key=lambda r: (-r["score"],))
        out.append({"name": "Facebook / Gumtree / other alerts",
                    "section": "Saved-search alerts",
                    "count": len(email_rows), "items": email_rows})

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps({"generated_at": NOW, "searches": out}, indent=2))
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(SEEN, indent=2))
    print(f"Wrote data. {len(new_hits)} new item(s).")
    if new_hits:
        maybe_email(new_hits)


def maybe_email(new_items: list) -> None:
    host, to = os.environ.get("SMTP_HOST"), os.environ.get("ALERT_TO")
    if not host or not to:
        return
    lines = ["New on the watch:\n"]
    for name, r in new_items:
        price = f"\u00a3{int(r['price'])} " if r["price"] else ""
        lines.append(f"[{name}] {price}{r['title']}")
        if r["url"]:
            lines.append(f"    {r['url']}")
        lines.append("")
    msg = MIMEText("\n".join(lines))
    msg["Subject"] = f"Forecourt: {len(new_items)} new"
    msg["From"] = os.environ.get("SMTP_USER", to)
    msg["To"] = to
    try:
        with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", 587))) as srv:
            srv.starttls()
            if os.environ.get("SMTP_USER"):
                srv.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASS", ""))
            srv.send_message(msg)
        print("Alert email sent.")
    except Exception as exc:  # noqa: BLE001
        print(f"Alert email failed (non-fatal): {exc}")


if __name__ == "__main__":
    main()
