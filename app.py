import base64
import hashlib
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlparse, quote

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
import requests
import aiohttp
import asyncio

try:
    # Optional: lets local runs read credentials from a .env file.
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)

# Sessions hold the OAuth state and the signed-in user, so the key must be
# stable across restarts. Set SECRET_KEY in the environment for real use.
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "").lower() in {"1", "true", "yes"},
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WAYBACK_API = "https://archive.org/wayback/available"

# Wikimedia API requires a descriptive User-Agent header per policy
WIKIDATA_HEADERS = {
    "User-Agent": "WikidataReferenceValidator/2.0 (https://github.com/JosefAnthony/Wikidata-Reference-Validator-Hackathon2026; Hackathon 2026)"
}

# External sites often block non-browser User-Agents
EXTERNAL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
}


# ---------------------------------------------------
# CANDIDATE CACHE
# ---------------------------------------------------

class CandidateCache:
    """Simple in-memory cache with TTL for verified candidates."""
    
    def __init__(self, ttl_seconds=3600):
        self.cache = {}
        self.ttl = ttl_seconds
    
    def _make_key(self, claim_ctx):
        key_parts = [
            claim_ctx.get("qid", ""),
            claim_ctx.get("property_id", ""),
            claim_ctx.get("statement_value", "")
        ]
        return "|".join(key_parts)
    
    def get(self, claim_ctx):
        key = self._make_key(claim_ctx)
        if key in self.cache:
            entry = self.cache[key]
            if time.time() - entry["timestamp"] < self.ttl:
                return entry["candidates"]
            else:
                del self.cache[key]
        return None
    
    def set(self, claim_ctx, candidates):
        key = self._make_key(claim_ctx)
        self.cache[key] = {
            "candidates": candidates,
            "timestamp": time.time()
        }


# Global cache instance
candidate_cache = CandidateCache(ttl_seconds=3600)


# ---------------------------------------------------
# LANGUAGES
# ---------------------------------------------------
# Languages offered in the UI. The template renders this list, so adding a
# language here is all that is needed for it to appear in the picker.
SUPPORTED_LANGUAGES = [
    ("en", "English"),
    ("fr", "French"),
    ("es", "Spanish"),
    ("de", "German"),
    ("it", "Italian"),
    ("pt", "Portuguese"),
    ("sw", "Swahili"),
    ("ar", "Arabic"),
    ("zh", "Chinese"),
    ("ja", "Japanese"),
    ("nl", "Dutch"),
]
LANGUAGE_CODES = {code for code, _ in SUPPORTED_LANGUAGES}
DEFAULT_LANGUAGE = "en"


def normalize_language(language):
    """Return a supported language code, falling back to English."""
    if not isinstance(language, str):
        return DEFAULT_LANGUAGE
    code = language.strip().lower()
    return code if code in LANGUAGE_CODES else DEFAULT_LANGUAGE


def language_fallbacks(language):
    """
    Lookup order for labels: the requested language, then English, then the
    language-neutral 'mul' term that Wikidata increasingly uses.
    """
    return list(dict.fromkeys([normalize_language(language), DEFAULT_LANGUAGE, "mul"]))


@app.context_processor
def inject_languages():
    return {"languages": SUPPORTED_LANGUAGES, "default_language": DEFAULT_LANGUAGE}


# ---------------------------------------------------
# WIKIMEDIA LOGIN (OAuth 2.0)
# ---------------------------------------------------
# Register an OAuth 2.0 consumer at
# https://meta.wikimedia.org/wiki/Special:OAuthConsumerRegistration/propose
# then copy .env.example to .env and fill in the credentials. Client IDs are
# public; the client secret must never be committed to the repository.
WIKIMEDIA_OAUTH_CLIENT_ID = os.environ.get(
    "WIKIMEDIA_CLIENT_ID", "a7d62252493d736604d7219ca46e4f05"
)
WIKIMEDIA_OAUTH_CLIENT_SECRET = os.environ.get("WIKIMEDIA_CLIENT_SECRET", "")
WIKIMEDIA_OAUTH_REDIRECT_URI = os.environ.get("WIKIMEDIA_REDIRECT_URI", "")

# Wikimedia consumers that have a client secret are confidential clients and
# must send it to the token endpoint. PKCE is only for consumers registered
# without a secret, so it has to be opted into explicitly.
WIKIMEDIA_OAUTH_USE_PKCE = os.environ.get("WIKIMEDIA_USE_PKCE", "").lower() in {
    "1", "true", "yes",
}

WIKIMEDIA_OAUTH_AUTHORIZE_URL = "https://meta.wikimedia.org/w/rest.php/oauth2/authorize"
WIKIMEDIA_OAUTH_TOKEN_URL = "https://meta.wikimedia.org/w/rest.php/oauth2/access_token"
WIKIMEDIA_OAUTH_PROFILE_URL = "https://meta.wikimedia.org/w/rest.php/oauth2/resource/profile"

LOGIN_ERRORS = {
    "not_configured": "Wikimedia sign-in is not configured on this server yet.",
    "denied": "You cancelled the Wikimedia sign-in.",
    "state": "That sign-in attempt expired. Please start again.",
    "failed": "Wikimedia sign-in failed. Please try again.",
}


def oauth_configured():
    """Login needs a client secret, unless PKCE was explicitly enabled."""
    if not WIKIMEDIA_OAUTH_CLIENT_ID:
        return False
    return bool(WIKIMEDIA_OAUTH_CLIENT_SECRET) or WIKIMEDIA_OAUTH_USE_PKCE


def oauth_redirect_uri():
    """Callback URL that must exactly match the one registered for the consumer."""
    return WIKIMEDIA_OAUTH_REDIRECT_URI or url_for("oauth_callback", _external=True)


def pkce_pair():
    """Return (code_verifier, code_challenge) for the PKCE S256 method."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def safe_next(target):
    """Only allow same-site relative paths as a post-login redirect."""
    if isinstance(target, str) and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("home")


@app.context_processor
def inject_user():
    return {"user": session.get("user")}


@app.route("/login")
def login():
    if session.get("user"):
        return redirect(url_for("home"))
    return render_template(
        "login.html",
        oauth_configured=oauth_configured(),
        error=LOGIN_ERRORS.get(request.args.get("error", "")),
        next=safe_next(request.args.get("next")),
    )


@app.route("/oauth/login")
def oauth_login():
    if session.get("user"):
        return redirect(url_for("home"))
    if not oauth_configured():
        return redirect(url_for("login", error="not_configured"))

    state = secrets.token_urlsafe(32)
    session["oauth_state"] = state
    session["oauth_next"] = safe_next(request.args.get("next"))

    params = {
        "response_type": "code",
        "client_id": WIKIMEDIA_OAUTH_CLIENT_ID,
        "redirect_uri": oauth_redirect_uri(),
        "state": state,
    }

    # Without a client secret we act as a public client, which needs PKCE.
    if not WIKIMEDIA_OAUTH_CLIENT_SECRET:
        verifier, challenge = pkce_pair()
        session["oauth_code_verifier"] = verifier
        params["code_challenge"] = challenge
        params["code_challenge_method"] = "S256"

    return redirect(f"{WIKIMEDIA_OAUTH_AUTHORIZE_URL}?{urlencode(params)}")


@app.route("/oauth/callback")
@app.route("/login/callback")
def oauth_callback():
    error = request.args.get("error")
    if error:
        return redirect(url_for("login", error="denied" if error == "access_denied" else "failed"))

    code = request.args.get("code", "")
    state = request.args.get("state", "")
    expected_state = session.pop("oauth_state", None)

    if not code or not state or not expected_state or not secrets.compare_digest(state, expected_state):
        app.logger.warning("OAuth callback rejected: code or state missing/mismatched.")
        return redirect(url_for("login", error="state"))

    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": oauth_redirect_uri(),
    }
    auth = None
    if WIKIMEDIA_OAUTH_CLIENT_SECRET:
        auth = (WIKIMEDIA_OAUTH_CLIENT_ID, WIKIMEDIA_OAUTH_CLIENT_SECRET)
    else:
        payload["client_id"] = WIKIMEDIA_OAUTH_CLIENT_ID
        verifier = session.pop("oauth_code_verifier", None)
        if verifier:
            payload["code_verifier"] = verifier
            payload["code_challenge_method"] = "S256"

    try:
        token_response = requests.post(
            WIKIMEDIA_OAUTH_TOKEN_URL,
            data=payload,
            auth=auth,
            headers=WIKIDATA_HEADERS,
            timeout=15,
        )
        token_response.raise_for_status()
        access_token = (token_response.json() or {}).get("access_token")
        if not access_token:
            raise ValueError("token response did not contain an access_token")

        profile_response = requests.get(
            WIKIMEDIA_OAUTH_PROFILE_URL,
            headers={**WIKIDATA_HEADERS, "Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        profile_response.raise_for_status()
        profile = profile_response.json() or {}
    except Exception as e:
        app.logger.warning("Wikimedia login failed: %s", e)
        return redirect(url_for("login", error="failed"))

    if not profile.get("username"):
        app.logger.warning("Wikimedia profile response had no username.")
        return redirect(url_for("login", error="failed"))

    session.permanent = True
    session["user"] = {
        "id": profile.get("sub"),
        "username": profile["username"],
        "editcount": profile.get("editcount"),
        "groups": profile.get("groups", []),
        "blocked": bool(profile.get("blocked")),
    }
    app.logger.info("Wikimedia login succeeded for %s", profile["username"])

    target = session.pop("oauth_next", None)
    return redirect(safe_next(target))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("home"))


# ---------------------------------------------------
# SOURCE CATEGORIES
# ---------------------------------------------------
# Heuristic grouping of reference URLs by the kind of site they point to.
# Order matters: the first matching category wins.

SOURCE_CATEGORY_DOMAINS = {
    "archive": {
        "archive.org", "archive.today", "archive.ph", "archive.is", "archive.li",
        "webcitation.org", "perma.cc", "webarchive.org.uk", "webarchive.nla.gov.au",
    },
    "wikimedia": {
        "wikipedia.org", "wikimedia.org", "wikidata.org", "wikisource.org",
        "wikiquote.org", "wikinews.org", "wiktionary.org", "wikivoyage.org",
        "wikibooks.org", "wikiversity.org", "mediawiki.org",
    },
    "reference": {
        "viaf.org", "worldcat.org", "isni.org", "d-nb.info", "bnf.fr", "id.loc.gov",
        "idref.fr", "openlibrary.org", "musicbrainz.org", "discogs.com", "imdb.com",
        "geonames.org", "britannica.com", "encyclopedia.com", "findagrave.com",
        "deutsche-biographie.de", "catalogue.bnf.fr", "data.bnf.fr", "nla.gov.au",
        "trove.nla.gov.au", "allmusic.com", "rottentomatoes.com", "themoviedb.org",
        "goodreads.com", "librarything.com", "britishmuseum.org", "europeana.eu",
        "getty.edu", "unesco.org", "olympedia.org", "sports-reference.com",
        "transfermarkt.com", "fifa.com", "census.gov", "citypopulation.de",
    },
    "academic": {
        "doi.org", "arxiv.org", "jstor.org", "ncbi.nlm.nih.gov", "europepmc.org",
        "orcid.org", "researchgate.net", "scholar.google.com", "semanticscholar.org",
        "springer.com", "link.springer.com", "sciencedirect.com", "wiley.com",
        "nature.com", "science.org", "tandfonline.com", "cambridge.org",
        "oup.com", "academic.oup.com", "plos.org", "crossref.org", "ieee.org",
        "acm.org", "ssrn.com", "mathgenealogy.org", "zbmath.org", "dblp.org",
    },
    "news": {
        "bbc.co.uk", "bbc.com", "nytimes.com", "theguardian.com", "reuters.com",
        "apnews.com", "cnn.com", "washingtonpost.com", "aljazeera.com",
        "bloomberg.com", "ft.com", "wsj.com", "npr.org", "telegraph.co.uk",
        "independent.co.uk", "lemonde.fr", "spiegel.de", "elpais.com", "time.com",
        "forbes.com", "economist.com", "latimes.com", "usatoday.com", "abc.net.au",
        "cbc.ca", "dw.com", "france24.com", "lefigaro.fr", "corriere.it",
        "repubblica.it", "zeit.de", "faz.net", "sueddeutsche.de", "nzz.ch",
        "thehindu.com", "timesofindia.indiatimes.com", "hindustantimes.com",
        "scmp.com", "smh.com.au", "punchng.com", "premiumtimesng.com",
        "vanguardngr.com", "thecable.ng", "channelstv.com", "guardian.ng",
        "dailytrust.com", "thisdaylive.com", "nation.africa", "news24.com",
        "variety.com", "hollywoodreporter.com", "rollingstone.com", "billboard.com",
    },
    "social": {
        "twitter.com", "x.com", "facebook.com", "instagram.com", "youtube.com",
        "youtu.be", "linkedin.com", "tiktok.com", "reddit.com", "flickr.com",
        "vimeo.com", "medium.com", "threads.net", "mastodon.social", "bsky.app",
        "soundcloud.com", "spotify.com", "github.com", "t.me",
    },
}

GOVERNMENT_LABELS = {"gov", "gob", "gouv", "govt", "go", "mil", "gv"}
ACADEMIC_LABELS = {"edu", "ac"}


def _matches_domain(host, domains):
    return any(host == d or host.endswith("." + d) for d in domains)


def categorize_source(url):
    """Return (domain, category) for a reference URL using simple domain heuristics."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        host = ""
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return "", "other"

    labels = host.split(".")

    for category in ("archive", "wikimedia", "reference", "academic"):
        if _matches_domain(host, SOURCE_CATEGORY_DOMAINS[category]):
            return host, category

    # .edu, .ac.uk, .edu.ng ...
    if labels[-1] == "edu" or (len(labels) >= 3 and labels[-2] in ACADEMIC_LABELS and len(labels[-1]) == 2):
        return host, "academic"

    # .gov, .mil, .gov.uk, .gov.ng, .gouv.fr, .go.jp, europa.eu ...
    if labels[-1] in ("gov", "mil") or host.endswith("europa.eu") or \
            (len(labels) >= 2 and labels[-2] in GOVERNMENT_LABELS and len(labels[-1]) == 2):
        return host, "government"

    for category in ("news", "social"):
        if _matches_domain(host, SOURCE_CATEGORY_DOMAINS[category]):
            return host, category
    if labels[0] == "news" or (len(labels) >= 2 and "news" in labels[-2]):
        return host, "news"

    return host, "other"


# ---------------------------------------------------
# ROUTES
# ---------------------------------------------------
@app.route("/")
def home():
    return render_template("index.html")


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/use")
def use():
    return render_template("use.html")


@app.route("/report")
def report():
    return render_template("report.html")


@app.route("/search")
def search():
    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify([])

    language = normalize_language(request.args.get("language"))
    params = {
        "action": "wbsearchentities",
        "search": query,
        "language": language,
        "uselang": language,
        "format": "json",
        "limit": 8
    }

    try:
        r = requests.get(WIKIDATA_API, params=params, headers=WIKIDATA_HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            raise ValueError("Unexpected search response")
    except Exception as e:
        print(f"[Search failed] {query}: {e}")
        return jsonify({"error": "Wikidata search is temporarily unavailable."}), 502

    items = [item for item in data.get("search", []) if isinstance(item, dict)]

    # wbsearchentities only returns a label when one exists in the requested
    # language, so fill the gaps from the fallback chain.
    missing_labels = {item.get("id") for item in items if item.get("id") and not item.get("label")}
    fallback_labels = fetch_labels(missing_labels, language) if missing_labels else {}

    results = []
    for item in items:
        item_id = item.get("id")
        results.append({
            "id": item_id,
            "label": item.get("label") or fallback_labels.get(item_id) or item_id,
            "description": item.get("description", "")
        })

    return jsonify(results)


# ---------------------------------------------------
# URL STATUS & WAYBACK CHECK (ASYNC + DEDUPLICATED)
# ---------------------------------------------------

async def check_url(session, semaphore, url):
    async with semaphore:
        head_timeout = aiohttp.ClientTimeout(total=6)
        try:
            async with session.head(url, allow_redirects=True, timeout=head_timeout) as resp:
                if resp.status < 400 or resp.status in (401, 403, 429):
                    return {"url": url, "status": "alive", "httpStatus": resp.status}
        except Exception:
            pass

        get_timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with session.get(url, allow_redirects=True, timeout=get_timeout) as resp:
                if resp.status < 400 or resp.status in (401, 403, 429):
                    return {"url": url, "status": "alive", "httpStatus": resp.status}
                return {"url": url, "status": "dead", "httpStatus": resp.status}
        except Exception:
            return {"url": url, "status": "dead", "httpStatus": None}


async def fetch_wayback_snapshot_async(session, semaphore, url):
    """
    Fetch latest available Wayback Machine snapshot for a dead URL asynchronously.
    Returns None if no archive exists.
    """
    async with semaphore:
        try:
            timeout = aiohttp.ClientTimeout(total=8)
            async with session.get(WAYBACK_API, params={"url": url}, timeout=timeout) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                snapshot = data.get("archived_snapshots", {}).get("closest")
                if snapshot and snapshot.get("available"):
                    return {
                        "archiveUrl": snapshot.get("url"),
                        "timestamp": snapshot.get("timestamp"),
                        "status": snapshot.get("status")
                    }
        except Exception as e:
            print(f"[Wayback] Failed for {url}: {e}")
        return None


# ---------------------------------------------------
# REFERENCE REPLACEMENT CANDIDATE DISCOVERY
# ---------------------------------------------------

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"

# Curated authority domains per property (high-trust sources for replacement candidates)
PROPERTY_AUTHORITY_DOMAINS = {
    "P31": ["wikidata.org", "wikipedia.org", "britannica.com", "academia.edu", "stanford.edu", "oxfordreference.com"],
    "P17": ["un.org", "unmemberstates.un.org", "cia.gov", "state.gov", "gov.uk", "europa.eu", "foreignaffairs.gov.za"],
    "P106": ["viaf.org", "worldcat.org", "id.loc.gov", "isni.org", "orcid.org", "wikidata.org", "wikipedia.org"],
    "P571": ["loc.gov", "archive.org", "worldcat.org", "nationalarchives.gov.uk", "biodiversitylibrary.org"],
    "P625": ["geonames.org", "openstreetmap.org", "nominatim.openstreetmap.org", "geodata.solutions"],
    "P1082": ["census.gov", "worldbank.org", "citypopulation.de", "stat.gov.pl", "inegi.org.mx", "ibge.gov.br"],
    "P30": ["geonames.org", "openstreetmap.org", "stat.gov.pl", "citypopulation.de"],
    "P131": ["geonames.org", "openstreetmap.org", "citypopulation.de", "stat.gov.pl"],
    "P27": ["viaf.org", "worldcat.org", "id.loc.gov", "orcid.org", "isni.org"],
    "P495": ["musicbrainz.org", "discogs.com", "last.fm", "allmusic.com"],
    "P407": ["ethnologue.com", "glottolog.org", "wals.info", "omniglot.com"],
    "P361": ["wikidata.org", "wikipedia.org", "britannica.com"],
    "P364": ["wikidata.org", "wikipedia.org", "cia.gov", "state.gov"],
    "P463": ["un.org", "wto.org", "who.int", "unesco.org", "oecd.org"],
    "P276": ["geonames.org", "openstreetmap.org", "archinform.net", "structurae.net"],
    "P155": ["wikidata.org", "wikipedia.org", "britannica.com", "archive.org"],
    "P156": ["wikidata.org", "wikipedia.org", "britannica.com", "archive.org"],
    "P1376": ["wikidata.org", "wikipedia.org", "britannica.com", "wikisource.org"],
    "P18": ["commons.wikimedia.org", "flickr.com", "inaturalist.org", "gbif.org"],
    "P227": ["viaf.org", "worldcat.org", "id.loc.gov", "isni.org", "d-nb.info", "bnf.fr"],
    "P213": ["viaf.org", "worldcat.org", "id.loc.gov", "isni.org"],
    "P214": ["viaf.org", "worldcat.org", "id.loc.gov", "isni.org", "orcid.org"],
    "P646": ["viaf.org", "worldcat.org", "id.loc.gov", "isni.org", "freebase.com"],
    "P244": ["viaf.org", "worldcat.org", "id.loc.gov", "isni.org", "lcnameauthority.loc.gov"],
    "P268": ["viaf.org", "worldcat.org", "id.loc.gov", "isni.org", "d-nb.info"],
    "P269": ["viaf.org", "worldcat.org", "id.loc.gov", "isni.org", "bnf.fr"],
    "P270": ["viaf.org", "worldcat.org", "id.loc.gov", "isni.org", "cinii.nijp.jp"],
    "P271": ["viaf.org", "worldcat.org", "id.loc.gov", "isni.org", "rdawiki.org"],
    "P281": ["geonames.org", "openstreetmap.org", "worldpostalcode.com", "postalcodes.io"],
    "P291": ["geonames.org", "openstreetmap.org", "worldcat.org", "library.congress.gov"],
    "P345": ["imdb.com", "tmdb.org", "rottentomatoes.com", "metacritic.com", "allmovie.com"],
    "P356": ["doi.org", "crossref.org", "sci-hub.se", "researchgate.net"],
    "P373": ["commons.wikimedia.org", "flickr.com", "wikimedia.org"],
    "P396": ["geonames.org", "openstreetmap.org", "waterinfo.org", "usgs.gov"],
    "P400": ["wikidata.org", "wikipedia.org", "britannica.com", "api.semanticscholar.org"],
    "P460": ["wikidata.org", "wikipedia.org", "britannica.com", "wikisource.org"],
    "P466": ["wikidata.org", "wikipedia.org", "britannica.com", "archive.org"],
    "P486": ["youtube.com", "youtu.be", "vimeo.com", "soundcloud.com", "spotify.com"],
    "P500": ["orcid.org", "researchgate.net", "academia.edu", "scholar.google.com"],
    "P528": ["wikidata.org", "wikipedia.org", "britannica.com", "catalogue.bnf.fr"],
    "P551": ["wikidata.org", "wikipedia.org", "britannica.com", "findagrave.com", "billiongraves.com"],
    "P569": ["viaf.org", "worldcat.org", "id.loc.gov", "isni.org", "d-nb.info", "bnf.fr"],
    "P570": ["viaf.org", "worldcat.org", "id.loc.gov", "isni.org", "findagrave.com", "legacy.com"],
    "P577": ["doi.org", "crossref.org", "arxiv.org", "semanticscholar.org", "pubmed.ncbi.nlm.nih.gov"],
    "P607": ["geonames.org", "openstreetmap.org", "geonames.usgs.gov", "naturalearthdata.com"],
    "P610": ["wikidata.org", "wikipedia.org", "britannica.com", "archive.org"],
    "P629": ["wikidata.org", "wikipedia.org", "britannica.com", "wikisource.org"],
    "P637": ["wikidata.org", "wikipedia.org", "britannica.com", "archive.org"],
    "P641": ["olympedia.org", "sports-reference.com", "fifa.com", "uefa.com", "ioc.org", "worldathletics.org"],
    "P646": ["freebase.com", "wikidata.org", "wikipedia.org"],
    "P735": ["viaf.org", "worldcat.org", "id.loc.gov", "isni.org", "forebears.io"],
    "P736": ["viaf.org", "worldcat.org", "id.loc.gov", "isni.org", "forebears.io"],
    "P742": ["musicbrainz.org", "discogs.com", "last.fm", "spotify.com", "allmusic.com"],
    "P767": ["musicbrainz.org", "discogs.com", "last.fm", "spotify.com", "allmusic.com"],
    "P800": ["wikidata.org", "wikipedia.org", "britannica.com", "everything.explained.today"],
    "P856": ["wikidata.org", "wikipedia.org", "official", "homepage"],
    "P854": [],  # reference URL itself
    "P856": [],  # official website
    "P884": ["archive.org", "web.archive.org", "archive.is", "archive.today"],
    "P910": ["wikidata.org", "wikipedia.org", "britannica.com", "wikisource.org"],
    "P921": ["wikidata.org", "wikipedia.org", "britannica.com", "wikisource.org"],
    "P935": ["wikidata.org", "wikipedia.org", "britannica.com", "wikisource.org"],
    "P973": ["wikidata.org", "wikipedia.org", "britannica.com", "everything.explained.today"],
    "P1082": ["census.gov", "worldbank.org", "citypopulation.de", "stat.gov.pl", "inegi.org.mx", "ibge.gov.br"],
    "P1104": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P1182": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P1198": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P1324": ["wikidata.org", "wikipedia.org", "britannica.com", "archivesportaleurope.net"],
    "P1366": ["wikidata.org", "wikipedia.org", "britannica.com", "wikisource.org"],
    "P1411": ["wikidata.org", "wikipedia.org", "britannica.com", "wikisource.org"],
    "P1448": ["wikidata.org", "wikipedia.org", "britannica.com", "wikisource.org"],
    "P1476": ["doi.org", "crossref.org", "scholar.google.com", "semanticscholar.org", "pubmed.ncbi.nlm.nih.gov"],
    "P1545": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P1559": ["wikidata.org", "wikipedia.org", "britannica.com", "wikisource.org"],
    "P1566": ["geonames.org", "openstreetmap.org", "geonames.usgs.gov", "naturalearthdata.com"],
    "P1619": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P1705": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P1813": ["wikidata.org", "wikipedia.org", "britannica.com", "wikisource.org"],
    "P1889": ["doi.org", "crossref.org", "arxiv.org", "semanticscholar.org", "pubmed.ncbi.nlm.nih.gov"],
    "P1921": ["wikidata.org", "wikipedia.org", "britannica.com", "wikisource.org"],
    "P2044": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2046": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2048": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2049": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2130": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2250": ["wikidata.org", "wikipedia.org", "britannica.com", "wikisource.org"],
    "P2393": ["doi.org", "crossref.org", "pmc.ncbi.nlm.nih.gov", "pubmed.ncbi.nlm.nih.gov"],
    "P2410": ["wikidata.org", "wikipedia.org", "britannica.com", "wikisource.org"],
    "P2537": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2538": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2539": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2540": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2541": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2542": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2543": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2544": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2545": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2546": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2547": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2548": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2549": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2550": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2551": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2552": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2553": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2554": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2555": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2556": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2557": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2558": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2559": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2560": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2561": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2562": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2563": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2564": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2565": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2566": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2567": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2568": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2569": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2570": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2571": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2572": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2573": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2574": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2575": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2576": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2577": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2578": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2579": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2580": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2581": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2582": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2583": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2584": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2585": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2586": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2587": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2588": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2589": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2590": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2591": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2592": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2593": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2594": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2595": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2596": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2597": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2598": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2599": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
    "P2600": ["wikidata.org", "wikipedia.org", "britannica.com", "geonames.org"],
}


async def search_domain(session, semaphore, domain, query, max_results=5):
    """Search a specific domain using site: query via search API or direct fetch."""
    # For now, construct a search URL and fetch results
    # In production, use SerpAPI/Bing/Google CSE
    search_url = f"https://www.google.com/search?q=site:{domain}+{quote(query)}&num={max_results}"
    # This is a placeholder - real implementation would use a search API
    return []


async def find_candidates_from_domains(session, semaphore, claim_ctx, max_results=10):
    """Find candidate URLs from curated authority domains."""
    property_id = claim_ctx.get("property_id", "")
    domains = PROPERTY_AUTHORITY_DOMAINS.get(property_id, PROPERTY_AUTHORITY_DOMAINS.get("default", ["wikidata.org", "wikipedia.org", "britannica.com"]))
    
    # Build search queries
    item_label = claim_ctx.get("item_label", "")
    prop_label = claim_ctx.get("property_label", "")
    value_label = claim_ctx.get("statement_value_label", "")
    
    queries = []
    if item_label and prop_label:
        queries.append(f'"{item_label}" "{prop_label}"')
    if value_label and prop_label:
        queries.append(f'"{value_label}" "{prop_label}"')
    if item_label and value_label:
        queries.append(f'{item_label} {prop_label} {value_label}')
    
    # For now, return empty - would need search API integration
    # This is the structure for when search API is available
    return []


async def find_candidates_from_wikidata_sparql(session, claim_ctx, max_results=10):
    """Find references from similar items via Wikidata SPARQL."""
    property_id = claim_ctx.get("property_id", "")
    statement_value = claim_ctx.get("statement_value", "")
    
    if not property_id or not statement_value:
        return []
    
    # SPARQL to find items with same property-value that have live references
    sparql_query = f"""
    SELECT DISTINCT ?item ?itemLabel ?refUrl ?refTitle WHERE {{
      ?item wdt:{property_id} wd:{statement_value} .
      ?item p:{property_id} ?stmt .
      ?stmt ps:{property_id} wd:{statement_value} .
      ?stmt prov:wasDerivedFrom ?ref .
      ?ref wdt:P854 ?refUrl .
      OPTIONAL {{ ?ref wdt:P1476 ?refTitle . }}
      OPTIONAL {{ ?item rdfs:label ?itemLabel . FILTER(LANG(?itemLabel) = "en") }}
      FILTER(STRSTARTS(STR(?refUrl), "http"))
    }}
    LIMIT {max_results * 3}
    """
    
    try:
        params = {"query": sparql_query, "format": "json"}
        async with session.get(WIKIDATA_SPARQL, params=params, headers=EXTERNAL_HEADERS, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            results = data.get("results", {}).get("bindings", [])
            
            candidates = []
            for r in results:
                url = r.get("refUrl", {}).get("value", "")
                title = r.get("refTitle", {}).get("value", "")
                item_label = r.get("itemLabel", {}).get("value", "")
                if url:
                    candidates.append({
                        "url": url,
                        "title": title,
                        "source_item": item_label,
                        "source": "wikidata_sparql",
                        "property_id": property_id,
                        "statement_value": statement_value
                    })
            return candidates[:max_results]
    except Exception as e:
        print(f"[SPARQL] Failed: {e}")
        return []


# ---------------------------------------------------
# SEARCH API INTEGRATION
# ---------------------------------------------------

# ---------------------------------------------------
# WIKIDATA-BASED CANDIDATE DISCOVERY (NO EXTERNAL APIs)
# ---------------------------------------------------

async def find_candidates_from_wikidata_search(session, claim_ctx, max_results=10):
    """Find candidates using Wikidata's own search and entity data."""
    candidates = []
    
    item_label = claim_ctx.get("item_label", "")
    property_id = claim_ctx.get("property_id", "")
    statement_value = claim_ctx.get("statement_value", "")
    statement_value_label = claim_ctx.get("statement_value_label", "")
    prop_label = claim_ctx.get("property_label", "")
    
    if not item_label or not property_id:
        return candidates
    
    # Strategy 1: Search for items with similar labels that have this property
    search_query = f'"{item_label}"'
    try:
        params = {
            "action": "wbsearchentities",
            "search": item_label,
            "language": "en",
            "format": "json",
            "limit": 10
        }
        async with session.get(WIKIDATA_API, params=params, headers=EXTERNAL_HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                for item in data.get("search", [])[:5]:
                    similar_qid = item.get("id")
                    if similar_qid and similar_qid != claim_ctx.get("qid"):
                        # Get references from this similar item for the same property
                        similar_candidates = await get_references_for_property(session, similar_qid, property_id)
                        candidates.extend(similar_candidates)
    except Exception as e:
        print(f"[Wikidata Search] Failed: {e}")
    
    # Strategy 2: Use the statement value entity's references if it's an entity
    if statement_value and statement_value.startswith("Q"):
        try:
            value_candidates = await get_references_for_property(session, statement_value, property_id)
            candidates.extend(value_candidates)
        except Exception as e:
            print(f"[Value Entity Refs] Failed: {e}")
    
    # Strategy 3: Wikidata property "example" references (P1855/P1856)
    try:
        prop_examples = await get_property_example_references(session, property_id)
        candidates.extend(prop_examples)
    except Exception as e:
        print(f"[Property Examples] Failed: {e}")
    
    # Deduplicate
    seen = set()
    unique = []
    for c in candidates:
        if c["url"] not in seen:
            seen.add(c["url"])
            unique.append(c)
    
    return unique[:max_results]


async def get_references_for_property(session, qid, property_id):
    """Extract reference URLs from a specific entity's claims for a property."""
    try:
        params = {"action": "wbgetentities", "ids": qid, "format": "json", "props": "claims"}
        async with session.get(WIKIDATA_API, params=params, headers=EXTERNAL_HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            entity = data.get("entities", {}).get(qid, {})
            claims = entity.get("claims", {}).get(property_id, [])
            
            candidates = []
            for claim in claims:
                for ref in claim.get("references", []):
                    for snaks in ref.get("snaks", {}).values():
                        for snak in snaks:
                            dv = snak.get("datavalue", {}).get("value")
                            if isinstance(dv, dict) and "uri" in dv:
                                url = dv["uri"]
                                if url.startswith("http"):
                                    # Get title from reference if available (P1476)
                                    title = ""
                                    if "P1476" in ref.get("snaks", {}):
                                        title_snak = ref["snaks"]["P1476"][0]
                                        title = title_snak.get("datavalue", {}).get("value", "")
                                    
                                    candidates.append({
                                        "url": url,
                                        "title": title,
                                        "source_item": qid,
                                        "source": "wikidata_entity_refs",
                                        "property_id": property_id
                                    })
            return candidates
    except Exception:
        return []


async def get_property_example_references(session, property_id):
    """Get example references from property constraints (P1855/P1856)."""
    try:
        params = {"action": "wbgetentities", "ids": property_id, "format": "json", "props": "claims"}
        async with session.get(WIKIDATA_API, params=params, headers=EXTERNAL_HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            entity = data.get("entities", {}).get(property_id, {})
            claims = entity.get("claims", {})
            
            candidates = []
            # P1855 = example of reference (example reference URL)
            for claim in claims.get("P1855", []):
                mainsnak = claim.get("mainsnak", {})
                dv = mainsnak.get("datavalue", {}).get("value")
                if isinstance(dv, str) and dv.startswith("http"):
                    candidates.append({
                        "url": dv,
                        "title": f"Example reference for {property_id}",
                        "source": "wikidata_property_example",
                        "property_id": property_id
                    })
            return candidates
    except Exception:
        return []


async def find_candidates_from_curated_domains_direct(session, semaphore, claim_ctx, max_results=10):
    """Direct fetch from curated domains using known URL patterns."""
    property_id = claim_ctx.get("property_id", "")
    domains = PROPERTY_AUTHORITY_DOMAINS.get(property_id, [])
    
    if not domains:
        return []
    
    candidates = []
    item_label = claim_ctx.get("item_label", "")
    statement_value_label = claim_ctx.get("statement_value_label", "")
    
    # Known URL patterns for common domains
    domain_patterns = {
        "cia.gov": lambda item, val: f"https://www.cia.gov/the-world-factbook/countries/{item.lower().replace(' ', '-')}/",
        "state.gov": lambda item, val: f"https://www.state.gov/countries-areas/{item.lower().replace(' ', '-')}/",
        "un.org": lambda item, val: f"https://www.un.org/en/about-us/member-states/{item.lower().replace(' ', '-')}",
        "britannica.com": lambda item, val: f"https://www.britannica.com/place/{item}",
        "geonames.org": lambda item, val: f"https://www.geonames.org/search.html?q={item}",
        "openstreetmap.org": lambda item, val: f"https://nominatim.openstreetmap.org/search?q={item}&format=json",
        "viaf.org": lambda item, val: f"https://viaf.org/viaf/search?query={item}",
        "worldcat.org": lambda item, val: f"https://www.worldcat.org/search?q={item}",
        "id.loc.gov": lambda item, val: f"https://id.loc.gov/authorities/names/{item}",
        "wikidata.org": lambda item, val: f"https://www.wikidata.org/wiki/Special:EntityData/{item}.json",
        "wikipedia.org": lambda item, val: f"https://en.wikipedia.org/wiki/{item.replace(' ', '_')}",
    }
    
    # Try pattern-based URLs for top domains
    for domain in domains[:5]:
        if domain in domain_patterns and item_label:
            try:
                url = domain_patterns[domain](item_label, statement_value_label)
                # Verify the URL exists
                async with semaphore:
                    async with session.head(url, headers=EXTERNAL_HEADERS, timeout=aiohttp.ClientTimeout(total=5), allow_redirects=True) as resp:
                        if resp.status < 400:
                            candidates.append({
                                "url": str(resp.url),
                                "title": f"{domain} page for {item_label}",
                                "source": "curated_domain_pattern",
                                "property_id": property_id,
                                "domain": domain
                            })
            except Exception:
                pass
    
    return candidates


async def find_candidates_from_curated_domains(session, semaphore, claim_ctx, max_results=10):
    """Wrapper for curated domain discovery."""
    return await find_candidates_from_curated_domains_direct(session, semaphore, claim_ctx, max_results)


# ---------------------------------------------------
# VERIFICATION
# ---------------------------------------------------
    property_patterns = {
        "P31": ["instance of", "type of", "classification"],
        "P17": ["country", "nation", "state"],
        "P106": ["occupation", "profession", "job"],
        "P571": ["founded", "established", "inception"],
        "P625": ["coordinates", "latitude", "longitude"],
        "P1082": ["population", "inhabitants"],
        "P30": ["located in", "located at"],
        "P131": ["located in", "administrative unit"],
        "P27": ["citizen of", "nationality"],
        "P495": ["origin", "country of origin"],
        "P407": ["language", "written in"],
        "P361": ["part of", "subclass of"],
    }
    
    if property_id in property_patterns:
        for pattern in property_patterns[property_id]:
            if item_label:
                queries.append(f'"{item_label}" "{pattern}"')
            if value_label:
                queries.append(f'"{value_label}" "{pattern}"')
    
    # Strategy 5: Wikidata property constraints
    if property_id and value_label and value_label.startswith("Q"):
        queries.append(f'wikidata {property_id} {value_label}')
    
    # Remove duplicates while preserving order
    seen = set()
    unique_queries = []
    for q in queries:
        if q not in seen:
            seen.add(q)
            unique_queries.append(q)
    
    return unique_queries[:6]  # Limit total queries


# ---------------------------------------------------
# CURATED DOMAIN DIRECT FETCH
# ---------------------------------------------------

async def find_candidates_from_curated_domains(session, semaphore, claim_ctx, max_results=10):
    """Direct fetch from high-authority curated domains for specific properties."""
    property_id = claim_ctx.get("property_id", "")
    domains = PROPERTY_AUTHORITY_DOMAINS.get(property_id, [])
    
    if not domains:
        return []
    
    # Build queries
    queries = build_search_queries(claim_ctx)
    if not queries:
        return []
    
    candidates = []
    # Try top 3 domains with top 2 queries
    for domain in domains[:3]:
        for query in queries[:2]:
            search_url = f"https://www.google.com/search?q=site:{domain}+{quote(query)}&num=5"
            # Note: This would need a search API or headless browser
            # For now, skip direct Google scraping to avoid blocks
            pass
    
    return candidates


async def fetch_and_verify_candidate(session, semaphore, url, claim_ctx):
    """Fetch a candidate URL and verify it contains the claim information."""
    async with semaphore:
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with session.get(url, headers=EXTERNAL_HEADERS, timeout=timeout, allow_redirects=True) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text()
                
                # Extract text content
                text = extract_text_from_html(html)
                if not text or len(text) < 100:
                    return None
                
                # Score relevance
                score = score_candidate_relevance(text, claim_ctx)
                if score < 0.3:
                    return None
                
                # Find best text fragment for deep linking
                fragment = find_best_fragment(text, claim_ctx)
                
                domain, category = categorize_source(url)
                
                return {
                    "url": url,
                    "score": score,
                    "snippet": highlight_snippet(text, claim_ctx, fragment),
                    "deep_link": f"{url}#:~:text={fragment}" if fragment else url,
                    "source_category": category,
                    "domain": domain,
                    "fetched_at": int(time.time())
                }
        except Exception as e:
            print(f"[Verify] Failed for {url}: {e}")
            return None


def extract_text_from_html(html):
    """Extract main text content from HTML."""
    try:
        # Simple extraction - in production use trafilatura or readability-lxml
        import re
        # Remove scripts, styles
        html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
        # Get text
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'\s+', ' ', text)
        return text.strip()[:10000]  # Limit size
    except Exception:
        return ""


def score_candidate_relevance(text, claim_ctx):
    """Score how well a page matches the claim."""
    import re
    text_lower = text.lower()
    
    item_label = claim_ctx.get("item_label", "").lower()
    prop_label = claim_ctx.get("property_label", "").lower()
    value_label = claim_ctx.get("statement_value_label", "").lower()
    
    score = 0.0
    weights = {"item": 0.4, "property": 0.3, "value": 0.3}
    
    if item_label and item_label in text_lower:
        score += weights["item"]
    if prop_label and prop_label in text_lower:
        score += weights["property"]
    if value_label and value_label in text_lower:
        score += weights["value"]
    
    # Bonus for proximity
    if item_label and value_label:
        item_pos = text_lower.find(item_label)
        value_pos = text_lower.find(value_label)
        if item_pos >= 0 and value_pos >= 0:
            distance = abs(item_pos - value_pos)
            if distance < 200:
                score += 0.1
            elif distance < 500:
                score += 0.05
    
    return min(score, 1.0)


def find_best_fragment(text, claim_ctx):
    """Find the best text fragment for deep linking."""
    import re
    text_lower = text.lower()
    
    item_label = claim_ctx.get("item_label", "").lower()
    value_label = claim_ctx.get("statement_value_label", "").lower()
    
    # Find position of key terms
    positions = []
    if item_label:
        pos = text_lower.find(item_label)
        if pos >= 0:
            positions.append(pos)
    if value_label:
        pos = text_lower.find(value_label)
        if pos >= 0:
            positions.append(pos)
    
    if not positions:
        return ""
    
    # Center fragment around middle of matches
    center = sum(positions) // len(positions)
    start = max(0, center - 150)
    end = min(len(text), center + 150)
    fragment = text[start:end].strip()
    
    # Clean for URL fragment
    fragment = re.sub(r'\s+', ' ', fragment)
    fragment = fragment[:300]  # URL fragment limit
    return fragment


def highlight_snippet(text, claim_ctx, fragment):
    """Create highlighted snippet for display."""
    # Simple highlighting - wrap key terms in <mark>
    import re
    snippet = fragment if fragment else text[:300]
    
    for term in [claim_ctx.get("item_label", ""), claim_ctx.get("statement_value_label", ""), claim_ctx.get("property_label", "")]:
        if term:
            snippet = re.sub(f'(?i)({re.escape(term)})', r'<mark>\1</mark>', snippet)
    
    return snippet


# ---------------------------------------------------
# LABEL & STATEMENT VALUE HELPERS
# ---------------------------------------------------

def extract_statement_value(mainsnak, all_ids_to_label):
    """
    Extract human-readable or entity ID representation from a statement mainsnak.
    """
    snaktype = mainsnak.get("snaktype")
    if snaktype == "somevalue":
        return "unknown value"
    if snaktype == "novalue":
        return "no value"

    datavalue = mainsnak.get("datavalue", {})
    dv_type = datavalue.get("type")
    val = datavalue.get("value")

    if val is None:
        return ""

    if dv_type == "wikibase-entityid" and isinstance(val, dict):
        eid = val.get("id", "")
        if eid:
            all_ids_to_label.add(eid)
        return eid
    elif dv_type == "string" and isinstance(val, str):
        return val
    elif dv_type == "monolingualtext" and isinstance(val, dict):
        return val.get("text", "")
    elif dv_type == "time" and isinstance(val, dict):
        time_str = val.get("time", "")
        # Format +1879-03-14T00:00:00Z -> 1879-03-14
        m = re.match(r"^[+-]?(\d{4}-\d{2}-\d{2})", time_str)
        if m:
            return m.group(1).replace("-00", "")
        return time_str
    elif dv_type == "quantity" and isinstance(val, dict):
        amount = val.get("amount", "")
        if isinstance(amount, str) and amount.startswith("+"):
            amount = amount[1:]
        return str(amount)
    elif dv_type == "globecoordinate" and isinstance(val, dict):
        lat = val.get("latitude")
        lon = val.get("longitude")
        if lat is not None and lon is not None:
            return f"{round(lat, 4)}, {round(lon, 4)}"

    return ""


def pick_label(terms, fallback="", language=DEFAULT_LANGUAGE):
    """Pick the term in the requested language, then English, then 'mul'."""
    for lang in language_fallbacks(language):
        value = (terms or {}).get(lang, {}).get("value")
        if value:
            return value
    return fallback


def fetch_labels(ids, language=DEFAULT_LANGUAGE):
    labels = {}
    if not ids:
        return labels

    language = normalize_language(language)
    languages = "|".join(language_fallbacks(language))
    id_list = sorted(set(ids))
    chunks = [id_list[i:i + 50] for i in range(0, len(id_list), 50)]

    for chunk in chunks:
        params = {
            "action": "wbgetentities",
            "ids": "|".join(chunk),
            "format": "json",
            "props": "labels",
            "languages": languages
        }

        try:
            r = requests.get(WIKIDATA_API, params=params, headers=WIKIDATA_HEADERS, timeout=10)
            r.raise_for_status()
            entities = r.json().get("entities", {})

            for eid, data in entities.items():
                label = pick_label(data.get("labels") if isinstance(data, dict) else None,
                                   language=language)
                if label:
                    labels[eid] = label

        except Exception as e:
            print(f"[Label fetch failed] {chunk}: {e}")

    return labels


def resolve_qid_from_query(query_str, language=DEFAULT_LANGUAGE):
    """
    If user typed a raw QID (e.g. 'Q937') or a search term without clicking autocomplete,
    resolve it to a valid QID.
    """
    cleaned = query_str.strip()
    if re.match(r"^[Qq]\d+$", cleaned):
        return cleaned.upper()

    language = normalize_language(language)
    params = {
        "action": "wbsearchentities",
        "search": cleaned,
        "language": language,
        "uselang": language,
        "format": "json",
        "limit": 1
    }
    r = requests.get(WIKIDATA_API, params=params, headers=WIKIDATA_HEADERS, timeout=10)
    r.raise_for_status()
    results = r.json().get("search", [])
    if results:
        return results[0].get("id")
    return None


# ---------------------------------------------------
# VALIDATE REFERENCES
# ---------------------------------------------------

@app.route("/validate", methods=["POST"])
def validate_references():
    data = request.get_json(silent=True) or {}
    raw_qid = (data.get("qid") or "").strip()
    raw_query = (data.get("query") or "").strip()

    target_input = raw_qid or raw_query
    if not target_input:
        return jsonify({"error": "Please enter a Wikidata item name or QID (e.g., Q937)."}), 400

    language = normalize_language(data.get("language"))

    try:
        qid = resolve_qid_from_query(target_input, language)
        if not qid:
            return jsonify({"error": f"Could not find a Wikidata item matching '{target_input}'."}), 404

        params = {
            "action": "wbgetentities",
            "ids": qid,
            "format": "json"
        }

        r = requests.get(WIKIDATA_API, params=params, headers=WIKIDATA_HEADERS, timeout=15)
        r.raise_for_status()

        entities = r.json().get("entities", {})
        entity = entities.get(qid, {})
        if not entity or "missing" in entity:
            return jsonify({"error": f"Wikidata item '{qid}' does not exist."}), 404

        entity_label = pick_label(entity.get("labels"), qid, language)
        entity_desc = pick_label(entity.get("descriptions"), "", language)
        claims = entity.get("claims", {})

        refs_data = []
        all_ids_to_label = set()

        for prop, statements in claims.items():
            for stmt in statements:
                mainsnak = stmt.get("mainsnak", {})
                statement_value = extract_statement_value(mainsnak, all_ids_to_label)

                for ref in stmt.get("references", []):
                    for snaks in ref.get("snaks", {}).values():
                        for snak in snaks:
                            dv = snak.get("datavalue", {}).get("value")
                            ref_url = None

                            if isinstance(dv, dict) and "uri" in dv:
                                ref_url = dv["uri"]
                            elif isinstance(dv, str) and dv.startswith("http"):
                                ref_url = dv

                            if ref_url:
                                refs_data.append({
                                    "url": ref_url,
                                    "property": prop,
                                    "statementValue": statement_value
                                })
                                all_ids_to_label.add(prop)

        if not refs_data:
            return jsonify({
                "message": f"No external URL references found on {entity_label} ({qid}).",
                "qid": qid,
                "label": entity_label,
                "language": language
            }), 200

        # Deduplicate URLs so we only check each unique URL once
        unique_urls = list(dict.fromkeys(ref["url"] for ref in refs_data))

        async def run_checks():
            semaphore = asyncio.Semaphore(20)
            connector = aiohttp.TCPConnector(ssl=False, limit=30)
            async with aiohttp.ClientSession(headers=EXTERNAL_HEADERS, connector=connector) as session:
                check_tasks = [check_url(session, semaphore, u) for u in unique_urls]
                check_results = await asyncio.gather(*check_tasks)
                status_map = {item["url"]: item for item in check_results}

                dead_urls = [u for u, info in status_map.items() if info["status"] == "dead"]
                wayback_map = {}
                if dead_urls:
                    wb_tasks = [fetch_wayback_snapshot_async(session, semaphore, u) for u in dead_urls]
                    wb_results = await asyncio.gather(*wb_tasks)
                    wayback_map = {u: wb for u, wb in zip(dead_urls, wb_results) if wb}

                return status_map, wayback_map

        status_map, wayback_map = asyncio.run(run_checks())
        labels = fetch_labels(all_ids_to_label, language)

        results = []
        for ref_data in refs_data:
            url = ref_data["url"]
            status_info = status_map.get(url, {"status": "dead", "httpStatus": None})
            prop_id = ref_data["property"]
            stmt_val = ref_data["statementValue"]
            domain, source_category = categorize_source(url)

            result = {
                "url": url,
                "domain": domain,
                "sourceCategory": source_category,
                "status": status_info["status"],
                "httpStatus": status_info.get("httpStatus"),
                "propertyId": prop_id,
                "propertyLabel": labels.get(prop_id, prop_id),
                "statementValue": labels.get(stmt_val, stmt_val) if stmt_val else ""
            }

            if status_info["status"] == "dead" and url in wayback_map:
                result["suggestedArchive"] = wayback_map[url]

            results.append(result)

        # Sort dead links first so editors see broken references immediately
        results.sort(key=lambda r: (0 if r["status"] == "dead" else 1, r["propertyLabel"]))

        return jsonify({
            "qid": qid,
            "label": entity_label,
            "description": entity_desc,
            "language": language,
            "references": results
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------
# REFERENCE REPLACEMENT API
# ---------------------------------------------------

@app.route("/api/candidates", methods=["POST"])
def find_replacement_candidates():
    """Find replacement candidates for a dead reference."""
    data = request.get_json(silent=True) or {}
    
    qid = (data.get("qid") or "").strip().upper()
    property_id = (data.get("property_id") or "").strip().upper()
    statement_hash = (data.get("statement_hash") or "").strip()
    dead_url = (data.get("dead_url") or "").strip()
    
    if not qid or not property_id:
        return jsonify({"error": "Missing qid or property_id"}), 400
    
    try:
        # Get the item and claim details
        params = {
            "action": "wbgetentities",
            "ids": qid,
            "format": "json"
        }
        r = requests.get(WIKIDATA_API, params=params, headers=WIKIDATA_HEADERS, timeout=15)
        r.raise_for_status()
        
        entity = r.json().get("entities", {}).get(qid, {})
        if not entity or "missing" in entity:
            return jsonify({"error": f"Wikidata item '{qid}' not found"}), 404
        
        item_label = pick_label(entity.get("labels"), qid, DEFAULT_LANGUAGE)
        claims = entity.get("claims", {}).get(property_id, [])
        
        # Find the specific claim
        target_claim = None
        statement_value = ""
        statement_value_label = ""
        
        for claim in claims:
            claim_hash = hashlib.sha256(json.dumps(claim, sort_keys=True).encode()).hexdigest()[:16]
            if claim_hash == statement_hash or not statement_hash:
                target_claim = claim
                mainsnak = claim.get("mainsnak", {})
                datavalue = mainsnak.get("datavalue", {})
                val = datavalue.get("value")
                if isinstance(val, dict) and "id" in val:
                    statement_value = val["id"]
                    # Fetch label for the value
                    value_labels = fetch_labels({statement_value}, DEFAULT_LANGUAGE)
                    statement_value_label = value_labels.get(statement_value, statement_value)
                else:
                    statement_value = str(val)
                    statement_value_label = str(val)
                break
        
        if not target_claim:
            return jsonify({"error": "Claim not found"}), 404
        
        # Build claim context
        claim_ctx = {
            "qid": qid,
            "item_label": item_label,
            "property_id": property_id,
            "property_label": pick_label(
                requests.get(WIKIDATA_API, params={"action": "wbgetentities", "ids": property_id, "format": "json", "props": "labels", "languages": "en"}).json().get("entities", {}).get(property_id, {}).get("labels", {}),
                property_id, DEFAULT_LANGUAGE
            ),
            "statement_value": statement_value,
            "statement_value_label": statement_value_label,
            "dead_url": dead_url
        }
        
        # Run async candidate discovery
        async def run_discovery():
            # Check cache first
            cached = candidate_cache.get(claim_ctx)
            if cached:
                return cached
            
            semaphore = asyncio.Semaphore(10)
            connector = aiohttp.TCPConnector(ssl=False, limit=20)
            async with aiohttp.ClientSession(headers=EXTERNAL_HEADERS, connector=connector) as session:
                all_candidates = []
                
                # 1. Wikidata SPARQL (proven references from similar items)
                sparql_candidates = await find_candidates_from_wikidata_sparql(session, claim_ctx, max_results=15)
                all_candidates.extend(sparql_candidates)
                
                # 2. Wikidata entity search & cross-references (NO external APIs)
                wikidata_candidates = await find_candidates_from_wikidata_search(session, claim_ctx, max_results=15)
                all_candidates.extend(wikidata_candidates)
                
                # 3. Curated domain direct fetch (pattern-based URLs)
                domain_candidates = await find_candidates_from_curated_domains(session, semaphore, claim_ctx, max_results=10)
                all_candidates.extend(domain_candidates)
                
                # Deduplicate by URL
                seen_urls = set()
                unique_candidates = []
                for cand in all_candidates:
                    url = cand.get("url", "")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        unique_candidates.append(cand)
                
                # Verify each candidate
                verified = []
                for cand in unique_candidates:
                    result = await fetch_and_verify_candidate(session, semaphore, cand["url"], claim_ctx)
                    if result:
                        result["title"] = cand.get("title", "")
                        result["source"] = cand.get("source", "unknown")
                        verified.append(result)
                
                # Sort by score (descending)
                verified.sort(key=lambda x: x["score"], reverse=True)
                result = verified[:10]
                
                # Cache the results
                candidate_cache.set(claim_ctx, result)
                return result
        
        candidates = asyncio.run(run_discovery())
        
        return jsonify({
            "candidates": candidates,
            "claim": {
                "qid": qid,
                "property_id": property_id,
                "property_label": claim_ctx["property_label"],
                "statement_value": statement_value,
                "statement_value_label": statement_value_label,
                "dead_url": dead_url
            }
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------
# USER API
# ---------------------------------------------------

@app.route("/api/user")
def api_user():
    """Return current user info."""
    user = session.get("user")
    if user:
        return jsonify(user)
    return jsonify(None), 401


# ---------------------------------------------------
# REFERENCE REPLACEMENT EDIT API
# ---------------------------------------------------

@app.route("/api/replace-reference", methods=["POST"])
def replace_reference():
    """Add a new reference to a Wikidata statement via OAuth."""
    if "user" not in session:
        return jsonify({"error": "Authentication required"}), 401
    
    data = request.get_json(silent=True) or {}
    
    qid = (data.get("qid") or "").strip().upper()
    property_id = (data.get("property_id") or "").strip().upper()
    statement_hash = (data.get("statement_hash") or "").strip()
    new_url = (data.get("new_url") or "").strip()
    dead_url = (data.get("dead_url") or "").strip()
    
    if not qid or not property_id or not new_url:
        return jsonify({"error": "Missing required fields"}), 400
    
    # Get OAuth token from session (would need to be stored during login)
    access_token = session.get("oauth_token")
    if not access_token:
        return jsonify({"error": "OAuth token not found. Please re-login."}), 401
    
    try:
        # Get the item and find the specific claim
        params = {
            "action": "wbgetentities",
            "ids": qid,
            "format": "json"
        }
        r = requests.get(WIKIDATA_API, params=params, headers=WIKIDATA_HEADERS, timeout=15)
        r.raise_for_status()
        
        entity = r.json().get("entities", {}).get(qid, {})
        if not entity or "missing" in entity:
            return jsonify({"error": f"Wikidata item '{qid}' not found"}), 404
        
        claims = entity.get("claims", {}).get(property_id, [])
        if not claims:
            return jsonify({"error": f"No claims found for property {property_id}"}), 404
        
        # Find the claim matching the statement hash
        target_claim = None
        for claim in claims:
            claim_hash = hashlib.sha256(json.dumps(claim, sort_keys=True).encode()).hexdigest()[:16]
            if claim_hash == statement_hash or not statement_hash:
                target_claim = claim
                break
        
        if not target_claim:
            return jsonify({"error": "Claim not found"}), 404
        
        claim_id = target_claim.get("id")
        if not claim_id:
            return jsonify({"error": "Claim ID not found"}), 500
        
        # Get CSRF token
        csrf_params = {
            "action": "query",
            "meta": "tokens",
            "type": "csrf",
            "format": "json"
        }
        csrf_r = requests.get(WIKIDATA_API, params=csrf_params, headers={**WIKIDATA_HEADERS, "Authorization": f"Bearer {access_token}"}, timeout=15)
        csrf_r.raise_for_status()
        csrf_token = csrf_r.json().get("query", {}).get("tokens", {}).get("csrftoken")
        
        if not csrf_token:
            return jsonify({"error": "Failed to get CSRF token"}), 500
        
        # Build reference snaks
        ref_snaks = {
            "P248": [{"snaktype": "value", "datavalue": {"type": "wikibase-entityid", "value": {"id": "Q1104466"}}}],  # stated in: CIA World Factbook (placeholder)
            "P854": [{"snaktype": "value", "datavalue": {"type": "string", "value": new_url}}],  # reference URL
            "P813": [{"snaktype": "value", "datavalue": {"type": "time", "value": {
                "time": datetime.utcnow().strftime("+%Y-%m-%dT%H:%M:%SZ"),
                "timezone": 0,
                "before": 0,
                "after": 0,
                "precision": 11,
                "calendarmodel": "http://www.wikidata.org/entity/Q1985727"
            }}}]  # retrieved date
        }
        
        # Try to determine stated-in (P248) from the new URL domain
        stated_in_qid = get_stated_in_qid(new_url)
        if stated_in_qid:
            ref_snaks["P248"] = [{"snaktype": "value", "datavalue": {"type": "wikibase-entityid", "value": {"id": stated_in_qid}}}]
        
        # Add reference via wbsetreference
        edit_params = {
            "action": "wbsetreference",
            "statement": claim_id,
            "snaks": json.dumps(ref_snaks),
            "token": csrf_token,
            "format": "json"
        }
        
        edit_r = requests.post(WIKIDATA_API, data=edit_params, headers={**WIKIDATA_HEADERS, "Authorization": f"Bearer {access_token}"}, timeout=30)
        edit_r.raise_for_status()
        edit_result = edit_r.json()
        
        if edit_result.get("success") != 1:
            return jsonify({"error": f"Wikidata edit failed: {edit_result}"}), 500
        
        # Log the replacement
        app.logger.info(
            "Reference replaced",
            extra={
                "qid": qid,
                "property": property_id,
                "claim_id": claim_id,
                "old_url": dead_url,
                "new_url": new_url,
                "user": session["user"]["username"]
            }
        )
        
        return jsonify({
            "success": True,
            "claim_id": claim_id,
            "reference_hash": edit_result.get("reference", {}).get("hash"),
            "message": "Reference added successfully"
        })
        
    except Exception as e:
        app.logger.error(f"Reference replacement failed: {e}")
        return jsonify({"error": str(e)}), 500


def get_stated_in_qid(url):
    """Map a URL domain to a Wikidata QID for 'stated in' (P248)."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return None
    
    if host.startswith("www."):
        host = host[4:]
    
    # Common domain -> QID mappings for P248
    domain_to_qid = {
        "cia.gov": "Q1104466",           # CIA World Factbook
        "state.gov": "Q1104466",         # US State Department
        "un.org": "Q1059382",            # United Nations
        "wikidata.org": "Q2013",         # Wikidata
        "wikipedia.org": "Q52",          # Wikipedia
        "britannica.com": "Q5375741",    # Encyclopaedia Britannica
        "geonames.org": "Q584703",       # GeoNames
        "openstreetmap.org": "Q936",     # OpenStreetMap
        "viaf.org": "Q54919",            # VIAF
        "worldcat.org": "Q41513",        # WorldCat
        "id.loc.gov": "Q1063627",        # Library of Congress
        "isni.org": "Q414906",           # ISNI
        "orcid.org": "Q49608",           # ORCID
        "doi.org": "Q25679",             # DOI
        "crossref.org": "Q25679",        # Crossref
        "arxiv.org": "Q1183634",         # arXiv
        "pubmed.ncbi.nlm.nih.gov": "Q544340",  # PubMed
        "census.gov": "Q1059424",        # US Census Bureau
        "worldbank.org": "Q1059424",     # World Bank
        "citypopulation.de": "Q5177871", # CityPopulation
        "commons.wikimedia.org": "Q565", # Wikimedia Commons
        "flickr.com": "Q103204",         # Flickr
        "imdb.com": "Q37312",            # IMDb
        "musicbrainz.org": "Q14005",     # MusicBrainz
        "discogs.com": "Q504063",        # Discogs
        "github.com": "Q364",            # GitHub
        "loc.gov": "Q1063627",           # Library of Congress
        "archive.org": "Q45196",         # Internet Archive
        "nominatim.openstreetmap.org": "Q936",  # Nominatim
        "geodata.solutions": "Q1104466", # placeholder
        "stat.gov.pl": "Q1059424",       # Statistics Poland
        "inegi.org.mx": "Q1059424",      # INEGI
        "ibge.gov.br": "Q1059424",       # IBGE
        "d-nb.info": "Q1059424",         # German National Library
        "bnf.fr": "Q1059424",            # Bibliothèque nationale de France
        "catalogue.bnf.fr": "Q1059424",  # BnF Catalogue
        "nla.gov.au": "Q1059424",        # National Library of Australia
        "trove.nla.gov.au": "Q1059424",  # Trove
        "allmusic.com": "Q1059424",      # AllMusic
        "rottentomatoes.com": "Q1059424",# Rotten Tomatoes
        "themoviedb.org": "Q1059424",    # TMDB
        "goodreads.com": "Q1059424",     # Goodreads
        "librarything.com": "Q1059424",  # LibraryThing
        "britishmuseum.org": "Q1059424", # British Museum
        "europeana.eu": "Q1059424",      # Europeana
        "getty.edu": "Q1059424",         # Getty
        "unesco.org": "Q1059424",        # UNESCO
        "olympedia.org": "Q1059424",     # Olympedia
        "sports-reference.com": "Q1059424", # Sports Reference
        "transfermarkt.com": "Q1059424", # Transfermarkt
        "fifa.com": "Q1059424",          # FIFA
        "census.gov": "Q1059424",        # Census.gov
        "citypopulation.de": "Q1059424", # CityPopulation
    }
    
    return domain_to_qid.get(host)


# ---------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True)
