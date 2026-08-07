# Forecourt

One dashboard that watches, every couple of hours, the second-hand and on-sale
things you're after — cars, the Vespa, nursery kit and renovation materials —
and flags what's new. Runs free on GitHub Actions + Pages. No server, no scraping.

## How it pulls things in (three routes)

| Route | Covers | How |
|---|---|---|
| **eBay Browse API** | used cars, scooter, prams, cribs, reclaimed materials, ex-display units | official free API |
| **RSS feeds** | Amazon price drops (CamelCamelCamel) + community deals (HotUKDeals) | you paste feed URLs |
| **Email ingest** | Facebook Marketplace, Gumtree, Preloved, Vinted, Enviromate | their own saved-search alerts, read from one Gmail label |

The third route is the answer to "these sites have no API." You never scrape
them — you use each site's built-in saved-search alerts, funnel those emails to
a label, and the watcher reads the label. Fully within their terms.

## Setup

### 1. eBay keys (free)
developer.ebay.com → sign in → create an app → open the **Production** keyset →
copy **App ID (Client ID)** and **Cert ID (Client Secret)**. Add as repo secrets
`EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET`
(Settings → Secrets and variables → Actions).

### 2. Turn on the dashboard
Settings → Pages → Deploy from branch → `main` / **/docs**.
Board lives at `https://<you>.github.io/<repo>/`.

### 3. RSS feeds (optional but recommended)
- **CamelCamelCamel** (Amazon price drops): make a **public** Amazon wishlist of
  your kit (SnüzPod, Cybex Gazelle, Cloud T, eufy pump, Babysense…), import it at
  camelcamelcamel.com, then copy the RSS URL it gives you into the matching
  `feeds` entry in `searches.json`.
- **HotUKDeals** (community deals): search a keyword on hotukdeals.com, grab the
  RSS link for that search, paste it into a `feeds` entry. One feed per keyword.

### 4. Facebook / Gumtree / etc. (the no-API route, optional)
1. On each site, save a search (e.g. "Cybex Gazelle within 15 miles") and turn on
   **email alerts**.
2. In Gmail, make a filter that labels those alert emails **forecourt** (match on
   sender, e.g. from:facebookmail.com OR from:gumtree.com).
3. Create a Gmail **app password** (Google Account → Security → 2-Step
   Verification → App passwords) and add repo secrets:
   `IMAP_HOST` = `imap.gmail.com`, `IMAP_USER` = your address,
   `IMAP_PASS` = the app password, `IMAP_FOLDER` = `forecourt`.

Those alerts then appear on the board in their own section.

### 5. Run it
Actions → **forecourt-watch** → Run workflow. Then it runs itself every 2 hours.
Optional outbound email digest of new finds: add `SMTP_*` and `ALERT_TO` secrets.

## Tuning (`searches.json`)
- `ebay_searches`: `type` is `car`, `scooter`, or `goods`. `reference_price` is
  what a deal is measured against (0 = don't score, just show newest).
- `feeds`: any RSS URL; `tag_label` is the badge it shows.
- `section` groups everything on the dashboard (Cars, Scooter, Nursery,
  Renovation, Saved-search alerts).

## Honest limits
eBay isn't the whole market and the email route depends on each site's alerts
firing, so this is your automated **first look**, not total coverage. For
new-goods "is this actually cheap?", trust the CamelCamelCamel history and the
HotUKDeals crowd over any number you'd compute yourself.
