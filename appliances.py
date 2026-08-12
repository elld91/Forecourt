#!/usr/bin/env python3
"""
Forecourt - graded/refurbished kitchen-appliance watcher (fast-cadence module).

Separate from the nightly watch.py: restricts to graded-outlet eBay sellers,
normalises each seller's grade vocabulary onto ONE internal A/B/C scale, then
alerts (email via Gmail SMTP) the moment a target model appears at an acceptable
grade under its price ceiling. Dedups on item id; re-alerts only on a price drop
below the last alerted price.

Reuses existing secrets: EBAY_CLIENT_ID / EBAY_CLIENT_SECRET for the API, and
IMAP_USER / IMAP_PASS (the Gmail app password) for sending the alert email.
No new secrets required.

Run:  python appliances.py --group cooker      (every 2h, 08:00-22:00)
      python appliances.py --group chilled     (fridge + hood, every 6h)
      python appliances.py --group all
"""

import argparse
import base64
import datetime as dt
import json
import os
import re
import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state" / "appliance_state.json"
OUT_FILE = ROOT / "docs" / "appliances.json"

OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
BROWSE_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
ITEM_URL = "https://api.ebay.com/buy/browse/v1/item/"
MARKETPLACE = "EBAY_GB"

# ---------------------------------------------------------------------------
# SELLER SCOPE  (internal key -> eBay seller username used in the API filter)
# Any username still starting with PASTE_ is skipped until you fill it in.
# To find a seller ID: open a live listing -> "Seller information" -> username.
# ---------------------------------------------------------------------------
SELLERS = {
    "currys":   "currys_clearance",           # confirmed
    "hughes":   "hughes_clearance_outlet",    # confirmed
    "ao":       "aooutlettelford",            # confirmed (usr == str slug)
    "applidir": "buyitdirect_outlet",         # confirmed: Appliances Direct outlet. NB user ID != store slug "buyitdirectoutlet"
}
SELLER_LABEL = {
    "currys": "Currys Clearance", "hughes": "Hughes Clearance Outlet",
    "ao": "AO Outlet", "applidir": "Appliances Direct",
}
# Sellers whose grade lives in the condition field / description (need a getItem call)
NEED_ITEM_LOOKUP = {"ao", "hughes"}

USER_TO_KEY = {v: k for k, v in SELLERS.items()}
ACTIVE_SELLERS = [v for v in SELLERS.values() if not v.startswith("PASTE_")]

# ---------------------------------------------------------------------------
# TARGETS
# ---------------------------------------------------------------------------
TARGETS = [
    {"key": "cooker", "group": "cooker", "label": "Range cooker",
     "queries": ["Stoves Richmond Deluxe 100", "Rangemaster Classic Deluxe 100"],
     "ceiling": 900, "grades": ["A", "B"], "exclude": ["60cm", "90cm"], "flag": ""},
    {"key": "fridge", "group": "chilled", "label": "American fridge freezer",
     "queries": ["Samsung American fridge freezer", "LG American fridge freezer"],
     "ceiling": 650, "grades": ["A", "B", "C"], "exclude": [],
     "flag": "Energy rating (A-E) is NOT in the title - check it on click."},
    {"key": "hood", "group": "chilled", "label": "100cm cooker hood",
     "queries": ["100cm cooker hood", "Stoves 100cm hood", "Elica 100cm hood"],
     "ceiling": 200, "grades": ["A", "B", "C"], "exclude": [], "flag": ""},
]

# ---------------------------------------------------------------------------
# GRADE-NORMALISATION LAYER  -> maps every seller's wording onto A / B / C
# ---------------------------------------------------------------------------

def strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s or "")


def normalise_grade(seller_key: str, title: str, cond: str, desc: str):
    """Return (internal_grade, raw_token) or (None, None)."""
    title_l = (title or "").lower()
    blob = " ".join([title_l, (cond or "").lower(), (desc or "").lower()])

    if seller_key == "currys":
        m = re.search(r"refurb[\s\-]?([abc])\b", title_l) or re.search(r"refurb[\s\-]?([abc])\b", blob)
        if m:
            g = m.group(1).upper()
            return g, "REFURB-" + g

    elif seller_key == "applidir":
        m = re.search(r"\bA[\s\-]?([123])\b", title or "")
        if m:
            return {"1": "A", "2": "B", "3": "C"}[m.group(1)], "A" + m.group(1)

    elif seller_key == "hughes":
        m = re.search(r"grade\s*([abc])\b", blob)
        if m:
            g = m.group(1).upper()
            return g, "Grade " + g
        if "excellent" in blob:
            return "A", "Excellent"
        if "very good" in blob:
            return "B", "Very Good"
        if "good" in blob:
            return "C", "Good"

    elif seller_key == "ao":
        if "ex-display" in blob or "ex display" in blob:
            return "B", "Ex-display"
        if any(k in blob for k in ("clearance", "graded", "return", "refurb")):
            return "C", "Clearance/returns"

    # Generic eBay "Certified Refurbished" fallback (works for any seller)
    if "certified refurbished" in blob or "excellent - refurbished" in blob:
        return "A", "Certified/Excellent"
    if "very good - refurbished" in blob:
        return "B", "Very Good"
    if "good - refurbished" in blob:
        return "C", "Good"
    return None, None


# ---------------------------------------------------------------------------
# eBay API
# ---------------------------------------------------------------------------

def get_token() -> str:
    cid, secret = os.environ.get("EBAY_CLIENT_ID"), os.environ.get("EBAY_CLIENT_SECRET")
    if not cid or not secret:
        sys.exit("Missing EBAY_CLIENT_ID / EBAY_CLIENT_SECRET.")
    basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    r = requests.post(OAUTH_URL,
                      headers={"Authorization": f"Basic {basic}",
                               "Content-Type": "application/x-www-form-urlencoded"},
                      data={"grant_type": "client_credentials",
                            "scope": "https://api.ebay.com/oauth/api_scope"}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def search(token: str, query: str, ceiling: int) -> list:
    if not ACTIVE_SELLERS:
        return []
    filt = (f"price:[..{ceiling}],priceCurrency:GBP,sellers:{{"
            + "|".join(ACTIVE_SELLERS) + "}")
    try:
        r = requests.get(BROWSE_URL,
                         headers={"Authorization": f"Bearer {token}",
                                  "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE},
                         params={"q": query, "filter": filt, "limit": 50}, timeout=30)
        r.raise_for_status()
    except requests.RequestException as exc:
        print(f"  ! search '{query}' failed: {exc}")
        return []
    return r.json().get("itemSummaries", []) or []


def get_item(token: str, item_id: str) -> dict:
    try:
        r = requests.get(ITEM_URL + quote(item_id, safe=""),
                         headers={"Authorization": f"Bearer {token}",
                                  "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE}, timeout=30)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as exc:
        print(f"  ! getItem failed: {exc}")
        return {}


# ---------------------------------------------------------------------------
# MATCHING
# ---------------------------------------------------------------------------

def title_matches(title: str, query: str, exclude: list) -> bool:
    t = (title or "").lower()
    if not all(tok.lower() in t for tok in query.split()):
        return False
    return not any(x.lower() in t for x in exclude)


def evaluate(token, summary, target):
    """Return a match dict if this listing qualifies, else None."""
    title = summary.get("title", "")
    if not title_matches(title, target["_query"], target.get("exclude", [])):
        return None
    price = float(summary.get("price", {}).get("value", 0) or 0)
    if price <= 0 or price > target["ceiling"]:
        return None
    seller = (summary.get("seller", {}) or {}).get("username", "")
    seller_key = USER_TO_KEY.get(seller)
    if not seller_key:
        return None

    cond = summary.get("condition", "")
    short = summary.get("shortDescription", "")
    grade, raw = normalise_grade(seller_key, title, cond, short)

    # Enrich from the full item (condition field / description) if needed
    if grade is None and seller_key in NEED_ITEM_LOOKUP:
        it = get_item(token, summary.get("itemId", ""))
        if it:
            desc = strip_html(it.get("description", "")) + " " + (it.get("shortDescription", "") or "")
            cdesc = it.get("conditionDescription", "") or ""
            descriptors = " ".join(
                str(v) for cd in (it.get("conditionDescriptors", []) or [])
                for v in (cd.get("values", []) or []))
            grade, raw = normalise_grade(seller_key, title,
                                         (it.get("condition", "") + " " + cdesc + " " + descriptors),
                                         desc)

    if grade not in target["grades"]:
        return None

    return {
        "id": summary.get("itemId", ""),
        "label": target["label"],
        "model_query": target["_query"],
        "grade": grade,
        "raw_grade": raw,
        "price": price,
        "seller": SELLER_LABEL.get(seller_key, seller),
        "url": summary.get("itemWebUrl", ""),
        "title": title,
        "flag": target.get("flag", ""),
    }


# ---------------------------------------------------------------------------
# NOTIFY
# ---------------------------------------------------------------------------

def send_email(subject: str, body: str) -> None:
    user, pw = os.environ.get("IMAP_USER"), os.environ.get("IMAP_PASS")
    to = os.environ.get("ALERT_TO") or user   # empty ALERT_TO secret -> fall back to self
    if not (user and pw):
        print("No IMAP_USER/IMAP_PASS set - can't send email. Alerts printed above.")
        return
    msg = MIMEText(body)
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.starttls()
            s.login(user, pw)
            s.send_message(msg)
        print(f"Alert email sent to {to}.")
    except Exception as exc:  # noqa: BLE001
        print(f"Email failed (non-fatal): {exc}")


def format_alert(m: dict) -> str:
    lines = [f"[{m['label']}] Grade {m['grade']} ({m['raw_grade']}) \u00b7 \u00a3{int(m['price'])} \u00b7 {m['seller']}",
             f"    {m['title']}"]
    if m["flag"]:
        lines.append(f"    ! {m['flag']}")
    lines.append(f"    {m['url']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", choices=["cooker", "chilled", "all"], default="all")
    args = ap.parse_args()

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    token = get_token()
    now = dt.datetime.now(dt.timezone.utc).isoformat()

    targets = [t for t in TARGETS if args.group in ("all", t["group"])]
    all_matches, new_alerts = [], []

    for target in targets:
        for query in target["queries"]:
            target["_query"] = query
            for summary in search(token, query, target["ceiling"]):
                m = evaluate(token, summary, target)
                if not m:
                    continue
                all_matches.append(m)
                prev = state.get(m["id"])
                is_new = prev is None
                price_drop = (prev is not None and m["price"] < prev.get("price", 1e9))
                if is_new or price_drop:
                    m["reason"] = "new" if is_new else "price drop"
                    new_alerts.append(m)
                    state[m["id"]] = {"price": m["price"], "grade": m["grade"], "ts": now}
        target.pop("_query", None)

    # de-dup all_matches by id for the JSON view
    seen, deduped = set(), []
    for m in all_matches:
        if m["id"] not in seen:
            seen.add(m["id"])
            deduped.append(m)
    deduped.sort(key=lambda x: (x["label"], x["price"]))

    # write per-group JSON without clobbering the other group
    out = json.loads(OUT_FILE.read_text()) if OUT_FILE.exists() else {}
    grp = args.group
    if grp == "all":
        out = {"generated_at": now, "cooker": [m for m in deduped if m["label"] == "Range cooker"],
               "chilled": [m for m in deduped if m["label"] != "Range cooker"]}
    else:
        out.setdefault("generated_at", now)
        out["generated_at"] = now
        out[grp] = deduped
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(out, indent=2))

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))

    print(f"[{args.group}] {len(deduped)} live match(es), {len(new_alerts)} to alert.")
    for m in new_alerts:
        print(" ->", format_alert(m))

    if new_alerts:
        body = "Graded appliance matches:\n\n" + "\n\n".join(format_alert(m) for m in new_alerts)
        send_email(f"Forecourt: {len(new_alerts)} graded appliance alert(s)", body)


if __name__ == "__main__":
    main()
