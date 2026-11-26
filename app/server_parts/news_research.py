"""/research_with_the_news source selection and research helpers."""

from __future__ import annotations

from .core import *

# News sources accepted by /news and completed by news_fetch.php.
NEWS_SOURCES = {
    "wsj": "The Wall Street Journal",
    "cnn": "CNN News",
    "cbslite": "CBC Lite",
    "nprtext": "NPR (Text)",
    "yahoo_finance": "Yahoo Finance",
    "medium": "Medium",
    "kqed": "KQED News",
    "sfgate": "SFGATE Bay Area News",
    "sfbayview": "SF Bay View",
    "mission_local": "Mission Local",
    "richmond_sunset": "Richmond Review",
    "techcrunch": "TechCrunch",
    "krasia": "KrASIA",
    "wccftech": "Wccftech",
    "hackernews": "Hacker News",
}

NEWS_SOURCE_USAGE = "|".join(NEWS_SOURCES)

# /research_with_the_news turns each question into a small news-research task.
# The research model writes two time-neutral search questions, Google News
# supplies the top three full articles for each question, and the same model
# writes a short briefing from that evidence. The mode stays on until the user
# runs "/research_with_the_news off"; every follow-up question is resolved
# against the accumulated research context.
RESEARCH_WITH_NEWS_MODEL = os.environ.get(
    "OLLAMA_RESEARCH_WITH_NEWS_MODEL", "ornith-1.5:35b"
)

RESEARCH_WITH_NEWS_QUESTIONS = 1

RESEARCH_WITH_NEWS_ARTICLES_PER_QUESTION = 4

RESEARCH_WITH_NEWS_DOCTOR_TITLE_CANDIDATES = 20

# The hospital_map in prompts/research_with_the_news.prompt. A doctor's
# articles can live on either the university site or the hospital site, so a
# mapped hospital is searched across every listed domain instead of only the
# one domain the profile model happened to name.
RESEARCH_WITH_NEWS_HOSPITAL_MAP = {
    "ucsf": ("ucsf.edu", "ucsfhealth.org"),
    "university of california san francisco": (
        "ucsf.edu",
        "ucsfhealth.org",
    ),
}

RESEARCH_WITH_NEWS_HOSPITAL_STOPWORDS = {
    "the",
    "of",
    "and",
    "at",
    "for",
    "university",
    "medical",
    "center",
    "centre",
    "hospital",
    "health",
    "healthcare",
    "system",
    "school",
    "medicine",
    "childrens",
    "children",
}

RESEARCH_WITH_NEWS_ARTICLE_CHARS = 1800

RESEARCH_WITH_NEWS_CONTEXT_CHARS = 14000

RESEARCH_WITH_NEWS_QUESTION_PROMPT = """You are planning a news search about a topic.

<accumulated_research_context>
{accumulated_context}
</accumulated_research_context>

<new_question>
{topic}
</new_question>

Write exactly one question that would find the most relevant news about this
question. Follow these rules:
- The question must focus tightly on the new question.
- When the new question is a follow-up (for example it uses "he", "she",
  "it", "they", "that company", or an elliptical phrase), resolve the
  reference from the accumulated research context and make the search
  question standalone.
- Keep the search question related to the current accumulated context; do not
  jump to an unrelated subject.
- Do not mention any time, date, year, month, week, season, or clock time.
- Do not use recency words such as latest, newest, recent, current, today, or
  now.
- Do not answer the question and do not add outside facts.
- Do not number the question.
- Return only the one question.
"""

RESEARCH_WITH_NEWS_SOURCE_PROMPT = """Decide where a research question should look for evidence.

<accumulated_research_context>
{accumulated_context}
</accumulated_research_context>

<question>
{topic}
</question>

Answer with exactly one word:
- doctor: the question is about a specific named medical doctor or physician,
  their hospital, patients, research, statements, or medical opinion.
- wikipedia: the question asks for a stable fact such as a birthday, biography,
  definition, history, what a company does, or how a concept works, and it is
  not about a specific doctor.
- news: the question asks about recent events, current developments,
  announcements, prices, or what happened lately, and it is not about a
  specific doctor.

Answer:"""

RESEARCH_WITH_NEWS_DOCTOR_PROMPT = """Identify the specific medical doctor and the hospital where the doctor works.

<accumulated_research_context>
{accumulated_context}
</accumulated_research_context>

<question>
{topic}
</question>

<wikipedia_article>
{article}
</wikipedia_article>

Return exactly four lines:
doctor: <the doctor's full name without the Dr. title>
hospital: <the hospital or health system name>
website: <the hospital's official website domain, for example ucsfhealth.org>
specialty: <the doctor's medical specialty>

Use the question, the Wikipedia article, and well-known public facts. Prefer
the Wikipedia article when it names the doctor's hospital. Write "unknown"
for any value you cannot establish, and never invent a website.
"""

RESEARCH_WITH_NEWS_HOSPITAL_WEBSITE_PROMPT = """Find the official website of the hospital where this doctor works.

<doctor>
{doctor}
</doctor>

<hospital>
{hospital}
</hospital>

<hospital_wikipedia_article>
{article}
</hospital_wikipedia_article>

Return only the hospital's official website domain, for example ucsfhealth.org.
Do not include a path, a search engine, Wikipedia, a news site, or social media.
If you are not confident, return unknown.
"""

RESEARCH_WITH_NEWS_ANSWER_PROMPT = """You are a careful news research assistant. Using only the news context below,
write a factual briefing about the question.

<accumulated_research_context>
{accumulated_context}
</accumulated_research_context>

<current_question>
{topic}
</current_question>

News context:
{context}

Use the accumulated research context only to understand what the current
question refers to. The factual evidence must come from the news context.

Write a coherent briefing in 100 to 150 words. Synthesize the relevant stories
instead of listing them mechanically. When reports disagree, prefer the more
specific and more recent report. Do not mention chunk numbers, citations, or
the names of these instructions. If the context leaves part of the topic
uncovered, omit that part rather than inventing details.
"""

RESEARCH_WITH_NEWS_SUFFICIENCY_PROMPT = """You are checking whether a Wikipedia article contains enough information to answer a question.

<question>
{topic}
</question>

<wikipedia_article>
{context}
</wikipedia_article>

Answer with exactly one word:
- sufficient: the article directly contains the facts needed to answer the question.
- insufficient: the article is about the right subject but does not contain the facts needed, or it is about a different subject.

Answer:"""

# Questions generated by a small local model sometimes still include a year or
# a recency word despite the prompt. Reject those lines and ask once more, then
# fall back to the topic itself so the research flow always runs.
RESEARCH_WITH_NEWS_TIME_RE = re.compile(
    r"\b(?:19|20)\d{2}\b|"
    r"\b(?:today|tonight|yesterday|tomorrow|latest|newest|recent|recently|"
    r"current|currently|now|this\s+(?:week|weekend|month|year)|last\s+"
    r"(?:week|weekend|month|year)|next\s+(?:week|weekend|month|year)|"
    r"\d{4}s)\b",
    re.IGNORECASE,
)

# /news search <keywords> <website> restricts a Google News search to one
# site. Accept a bare hostname (howstuffworks.com), a www host, or a full URL;
# the site: operator uses the host plus an optional path.
NEWS_SEARCH_WEBSITE_RE = re.compile(
    r"^(?:https?://)?(?P<host>(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+"
    r"[a-z]{2,63})(?P<path>/[^\s]*)?$",
    re.IGNORECASE,
)

# A trailing token such as "node.js" or "notes.md" is far more likely to be a
# keyword than a website, so these file-like endings only count as a site when
# the user is explicit (a scheme, www., or a path).
NEWS_SEARCH_FILE_EXTENSIONS = {
    "js", "jsx", "ts", "tsx", "py", "rb", "go", "rs", "php", "java",
    "c", "h", "cpp", "cs", "sh", "md", "html", "htm", "css", "json",
    "xml", "yaml", "yml", "toml", "csv", "sql", "txt", "ipynb", "pdf",
    "zip", "tar", "gz", "jpg", "jpeg", "png", "gif", "svg", "webp",
    "mp3", "mp4", "wav",
}

def split_news_search_website(keywords: str) -> tuple[str, str]:
    """Split a trailing website off a /news search query.

    ``/news search <keywords> <website>`` searches Google News with a
    ``site:<website>`` restriction. Only the final token is considered, and
    only when at least one keyword precedes it, so ordinary searches (and a
    lone ``node.js`` keyword) are left untouched.

    Returns ``(keywords, website)`` where ``website`` is an empty string when
    no site was requested.
    """
    stripped = keywords.strip()
    parts = stripped.split()
    if len(parts) < 2:
        return stripped, ""

    candidate = parts[-1]
    match = NEWS_SEARCH_WEBSITE_RE.match(candidate)
    if match is None:
        return stripped, ""

    host = match.group("host")
    path = (match.group("path") or "").rstrip("/")
    extension = host.rsplit(".", 1)[-1].lower()
    explicit = (
        candidate.lower().startswith(("http://", "https://", "www."))
        or "/" in candidate
    )
    if extension in NEWS_SEARCH_FILE_EXTENSIONS and not explicit:
        return stripped, ""

    return " ".join(parts[:-1]), host + path

def _research_with_news_clean_questions(
    response: str, limit: int = RESEARCH_WITH_NEWS_QUESTIONS
) -> list[str]:
    """Extract time-neutral search questions from a model response."""
    questions: list[str] = []
    seen: set[str] = set()
    for raw_line in chat.THINK_PATTERN.sub("", response).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", line)
        line = re.sub(
            r"^(?:question|q)\s*\d*\s*[:.)-]\s*", "", line, flags=re.IGNORECASE
        )
        line = line.strip().strip("\"'`“”").strip()
        if not line or len(line) > 300:
            continue
        if RESEARCH_WITH_NEWS_TIME_RE.search(line):
            continue
        key = line.casefold()
        if key in seen:
            continue
        seen.add(key)
        questions.append(line)
        if len(questions) >= limit:
            break
    return questions

_GENERAL_FACT_TOPIC_RE = re.compile(
    r"\b(?:birthday|date of birth|born|biography|what is|what are|who is|"
    r"who was|define|definition|meaning of|what does .* do|how does .* work|"
    r"history of)\b",
    re.IGNORECASE,
)

# A named doctor is a strong signal for hospital-website research. The local
# model makes the real decision; this fallback covers a question such as
# "what does Dr. Jane Smith think about the new treatment?" when Ollama is
# unavailable. Obvious biography questions still go to Wikipedia first.
_DOCTOR_TOPIC_RE = re.compile(
    r"\b(?:dr\.?\s+[a-z][\w'’.-]*|"
    r"(?:doctor|physician|surgeon|cardiologist|oncologist|neurologist|"
    r"dermatologist|pediatrician|psychiatrist|radiologist|"
    r"gastroenterologist|endocrinologist|immunologist|rheumatologist|"
    r"nephrologist|pulmonologist|urologist|obstetrician|gynecologist|"
    r"orthopedic surgeon|primary care physician))\b",
    re.IGNORECASE,
)

_DOCTOR_BIOGRAPHY_RE = re.compile(
    r"\b(?:birthday|date of birth|born|age|biography|who is|wikipedia)\b",
    re.IGNORECASE,
)

def classify_research_with_news_source(
    topic: str,
    log: Callable[[str], None],
    accumulated_context: str = "",
) -> str:
    """Return ``doctor``, ``wikipedia``, or ``news`` for a research topic.

    A specific doctor needs a different evidence path from both a stable
    general fact and a breaking-news question: the next step is to identify
    the doctor's hospital and search that hospital's own website. The local
    model makes the decision; if it is unavailable, small keyword fallbacks
    keep the obvious cases working.
    """
    context = accumulated_context.strip() or "(no previous research context)"
    try:
        response = chat.generate_text(
            RESEARCH_WITH_NEWS_SOURCE_PROMPT.format(
                topic=topic, accumulated_context=context
            ),
            model=RESEARCH_WITH_NEWS_MODEL,
            temperature=0.0,
            num_predict=8,
            stop=["\n"],
        )
    except RuntimeError as exc:
        log(
            "[research_with_the_news: could not classify the question "
            f"source: {exc}; using the keyword fallback]"
        )
        response = ""
    choice = chat.THINK_PATTERN.sub("", response).casefold()
    if "doctor" in choice:
        return "doctor"
    if "wikipedia" in choice:
        return "wikipedia"
    if re.search(r"\bnews\b", choice):
        return "news"
    if (
        _DOCTOR_TOPIC_RE.search(topic)
        and not _DOCTOR_BIOGRAPHY_RE.search(topic)
    ):
        return "doctor"
    if _GENERAL_FACT_TOPIC_RE.search(topic):
        return "wikipedia"
    return "news"

def is_research_with_news_general_fact(
    topic: str,
    log: Callable[[str], None],
    accumulated_context: str = "",
) -> bool:
    """Compatibility helper: True when the topic is a Wikipedia fact."""
    return (
        classify_research_with_news_source(topic, log, accumulated_context)
        == "wikipedia"
    )

def _research_with_wikipedia_paragraphs(text: str) -> list[str]:
    """Split a fetched Wikipedia article into clean, deduplicated paragraphs."""
    paragraphs: list[str] = []
    seen: set[str] = set()
    for block in re.split(r"\n\s*\n", str(text or "")):
        for line in block.splitlines():
            paragraph = " ".join(line.split())
            if not paragraph:
                continue
            key = paragraph.casefold()
            if key in seen:
                continue
            seen.add(key)
            paragraphs.append(paragraph)
    return paragraphs

def search_research_with_wikipedia(
    question: str,
    limit: int = 1,
    log: Callable[[str], None] | None = None,
) -> list[dict]:
    """Fetch the relevant English Wikipedia article for a general question."""
    report = log or (lambda _text: None)
    limit = max(1, int(limit))
    entities = chat.wiki_related_entities(
        question, model=RESEARCH_WITH_NEWS_MODEL, limit=4
    )
    if not entities:
        entities = [question]
    articles: list[dict] = []
    seen: set[str] = set()
    for entity in entities:
        resolved = chat.wiki_disambiguate_entity(entity, question)
        report(
            "[research_with_the_news: fetching Wikipedia article "
            f"{resolved!r} from en.wikipedia.org]"
        )
        article = chat.fetch_wiki_full_article(resolved)
        if article is None:
            report(
                "[research_with_the_news: no Wikipedia article found for "
                f"{resolved!r}]"
            )
            continue
        title = " ".join(
            str(article.get("title") or resolved).split()
        )
        link = str(article.get("url") or "").strip()
        paragraphs = _research_with_wikipedia_paragraphs(
            str(article.get("text") or "")
        )
        if not paragraphs:
            report(
                "[research_with_the_news: Wikipedia article "
                f"{title!r} had no readable text]"
            )
            continue
        key = (link or title).casefold()
        if key in seen:
            continue
        seen.add(key)
        articles.append(
            {
                "title": title,
                "link": link,
                "published": "",
                "source": "Wikipedia",
                "summary": paragraphs[0],
                "paragraphs": paragraphs,
                "question": question,
            }
        )
        report(
            "[research_with_the_news: using Wikipedia article: "
            f"{title} ({link or 'no URL'})]"
        )
        if len(articles) >= limit:
            break
    if not articles:
        raise RuntimeError(
            f"no readable Wikipedia article was found ({question!r})"
        )
    return articles

def research_with_news_context_is_sufficient(
    topic: str,
    context: str,
    log: Callable[[str], None],
) -> bool:
    """Return True when the fetched Wikipedia context answers the topic.

    A matching Wikipedia article is not automatically sufficient: a broad
    biography may mention a person without answering a specific question about
    them. Ask the research model for a binary verdict before committing to the
    Wikipedia path, so the caller can fall back to news when the article lacks
    the needed facts. On an unclear model response or a checker failure, keep
    the Wikipedia evidence rather than discarding a usable article.
    """
    try:
        response = chat.generate_text(
            RESEARCH_WITH_NEWS_SUFFICIENCY_PROMPT.format(
                topic=topic, context=context
            ),
            model=RESEARCH_WITH_NEWS_MODEL,
            temperature=0.0,
            num_predict=12,
            stop=["\n"],
        )
    except RuntimeError as exc:
        log(
            "[research_with_the_news: could not check whether the Wikipedia "
            f"article is sufficient: {exc}; keeping the Wikipedia evidence]"
        )
        return True
    choice = chat.THINK_PATTERN.sub("", response).casefold()
    if (
        "insufficient" in choice
        or "not enough" in choice
        or "does not contain" in choice
    ):
        return False
    if "sufficient" in choice or "enough" in choice:
        return True
    log(
        "[research_with_the_news: Wikipedia sufficiency check returned an "
        "unclear answer; keeping the Wikipedia evidence]"
    )
    return True

_RESEARCH_WITH_NEWS_WIKIDATA_API_URL = "https://www.wikidata.org/w/api.php"

_RESEARCH_WITH_NEWS_WEBSITE_PROPERTY = "P856"

_RESEARCH_WITH_NEWS_BLOCKED_WEBSITES = {
    "wikipedia.org",
    "en.wikipedia.org",
    "wikidata.org",
    "google.com",
    "news.google.com",
    "facebook.com",
    "twitter.com",
    "x.com",
    "instagram.com",
    "linkedin.com",
    "youtube.com",
    "tiktok.com",
}

def _research_with_news_clean_profile_value(value: str) -> str:
    """Normalize one line value from the doctor-profile model output."""
    cleaned = " ".join(str(value or "").split()).strip("\"'`“”")
    cleaned = cleaned.rstrip(".").strip()
    if cleaned.casefold() in {
        "",
        "unknown",
        "none",
        "n/a",
        "na",
        "not known",
        "-",
    }:
        return ""
    return cleaned

def _research_with_news_normalize_website(value: str) -> str:
    """Return a bare, safe website domain from a model or Wikidata value."""
    website = _research_with_news_clean_profile_value(value)
    if not website:
        return ""
    website = re.sub(r"^[a-z][a-z0-9+.-]*://", "", website, flags=re.I)
    website = website.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    website = website.strip().rstrip(".")
    website = re.sub(r"^www\.", "", website, flags=re.I).casefold()
    if not re.fullmatch(
        r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}",
        website,
        re.IGNORECASE,
    ):
        return ""
    if website in _RESEARCH_WITH_NEWS_BLOCKED_WEBSITES:
        return ""
    if any(website.endswith("." + blocked) for blocked in (
        "wikipedia.org",
        "wikidata.org",
        "google.com",
        "facebook.com",
        "twitter.com",
        "instagram.com",
        "linkedin.com",
        "youtube.com",
    )):
        return ""
    return website

def _research_with_news_hospital_websites(hospital: str) -> list[str]:
    """Return the domains listed for a hospital in the prompt's hospital_map.

    Hospital names come from a local model, so they may include credentials
    and generic words ("UCSF Medical Center", "University of California, San
    Francisco Health"). Match on the significant name tokens and keep the
    prompt's domain order. A hospital that is not in the map returns no
    domains; callers then keep the single website from the doctor profile.
    """
    normalized = " ".join(
        re.findall(r"[a-z0-9]+", str(hospital or "").casefold())
    )
    if not normalized:
        return []
    hospital_tokens = set(normalized.split())
    matches: list[tuple[int, tuple[str, ...]]] = []
    for name, domains in RESEARCH_WITH_NEWS_HOSPITAL_MAP.items():
        name_tokens = {
            token
            for token in re.findall(r"[a-z0-9]+", name.casefold())
            if token not in RESEARCH_WITH_NEWS_HOSPITAL_STOPWORDS
        }
        if name_tokens and name_tokens <= hospital_tokens:
            matches.append((len(name_tokens), domains))
    if not matches:
        return []
    # Prefer the most specific map entry (the full university name over the
    # UCSF acronym when both are present).
    domains = max(matches, key=lambda item: item[0])[1]
    websites: list[str] = []
    for domain in domains:
        website = _research_with_news_normalize_website(domain)
        if website and website not in websites:
            websites.append(website)
    return websites

def _wikipedia_official_website(title: str) -> str:
    """Look up a Wikipedia page's official website through Wikidata P856."""
    title = " ".join(str(title or "").split())
    if not title:
        return ""
    try:
        response = requests.get(
            WIKIPEDIA_API_URL,
            params={
                "action": "query",
                "prop": "pageprops",
                "titles": title,
                "format": "json",
                "redirects": 1,
            },
            headers=chat.HTTP_HEADERS,
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError):
        return ""
    pages = ((data.get("query") or {}).get("pages") or {})
    item = ""
    for page in pages.values():
        if not isinstance(page, dict):
            continue
        item = str(((page.get("pageprops") or {}).get("wikibase_item")) or "")
        if item:
            break
    if not item:
        return ""
    try:
        response = requests.get(
            _RESEARCH_WITH_NEWS_WIKIDATA_API_URL,
            params={
                "action": "wbgetentities",
                "ids": item,
                "props": "claims",
                "format": "json",
            },
            headers=chat.HTTP_HEADERS,
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError):
        return ""
    entity = ((data.get("entities") or {}).get(item) or {})
    claims = ((entity.get("claims") or {}).get(
        _RESEARCH_WITH_NEWS_WEBSITE_PROPERTY
    ) or [])
    for claim in claims:
        try:
            raw_value = claim["mainsnak"]["datavalue"]["value"]
        except (KeyError, TypeError):
            continue
        website = _research_with_news_normalize_website(str(raw_value))
        if website:
            return website
    return ""

def _research_with_news_parse_doctor_profile(response: str) -> dict[str, str]:
    """Parse the four ``key: value`` lines from the doctor-profile model."""
    profile = {"doctor": "", "hospital": "", "website": "", "specialty": ""}
    for raw_line in chat.THINK_PATTERN.sub("", response).splitlines():
        match = re.match(
            r"^\s*(doctor|hospital|website|specialty)\s*[:=\-]\s*(.*?)\s*$",
            raw_line,
            re.IGNORECASE,
        )
        if match is None:
            continue
        key = match.group(1).casefold()
        value = _research_with_news_clean_profile_value(match.group(2))
        if key == "doctor":
            value = re.sub(
                r"^(?:dr\.?|doctor)\s+", "", value, flags=re.IGNORECASE
            ).strip()
            value = re.sub(
                r",?\s*(?:M\.?D\.?|D\.?O\.?|PhD|MBBS)$",
                "",
                value,
                flags=re.IGNORECASE,
            ).strip()
        elif key == "website":
            value = _research_with_news_normalize_website(value)
        profile[key] = value
    return profile

def _research_with_news_find_doctor_article(
    topic: str, report: Callable[[str], None]
) -> tuple[dict, list[str]]:
    """Fetch a Wikipedia article that can reveal the doctor's hospital."""
    entities = chat.wiki_related_entities(
        topic, model=RESEARCH_WITH_NEWS_MODEL, limit=4
    )
    if not entities:
        entities = [topic]
    for entity in entities[:4]:
        resolved = chat.wiki_disambiguate_entity(entity, topic)
        article = chat.fetch_wiki_full_article(resolved)
        if article is None:
            continue
        paragraphs = _research_with_wikipedia_paragraphs(
            str(article.get("text") or "")
        )
        if not paragraphs:
            continue
        return article, paragraphs
    return {}, []

def _research_with_news_hospital_website(
    doctor: str,
    hospital: str,
    report: Callable[[str], None],
) -> str:
    """Resolve a hospital's official website from Wikipedia/Wikidata."""
    if not hospital:
        return ""
    article = chat.fetch_wiki_full_article(hospital)
    title = str((article or {}).get("title") or hospital)
    website = _wikipedia_official_website(title)
    if website:
        report(
            "[research_with_the_news: found the hospital website through "
            f"Wikipedia/Wikidata: {title} -> {website}]"
        )
        return website
    if article is None:
        return ""
    paragraphs = _research_with_wikipedia_paragraphs(
        str(article.get("text") or "")
    )
    if not paragraphs:
        return ""
    try:
        response = chat.generate_text(
            RESEARCH_WITH_NEWS_HOSPITAL_WEBSITE_PROMPT.format(
                doctor=doctor or "unknown",
                hospital=hospital,
                article="\n\n".join(paragraphs)[:5000],
            ),
            model=RESEARCH_WITH_NEWS_MODEL,
            temperature=0.0,
            num_predict=24,
            stop=["\n"],
        )
    except RuntimeError as exc:
        report(
            "[research_with_the_news: could not ask for the hospital "
            f"website: {exc}]"
        )
        return ""
    for line in chat.THINK_PATTERN.sub("", response).splitlines():
        website = _research_with_news_normalize_website(line)
        if website:
            report(
                "[research_with_the_news: found the hospital website from "
                f"the article: {hospital} -> {website}]"
            )
            return website
    return ""

def plan_research_with_doctor_profile(
    topic: str,
    log: Callable[[str], None],
    accumulated_context: str = "",
) -> dict[str, str]:
    """Identify the doctor, hospital, and hospital website for a question.

    The doctor's Wikipedia article grounds the extraction, and a hospital's
    Wikipedia/Wikidata entry supplies the official website when the model does
    not know it. If the website cannot be established, the caller falls back
    to an unrestricted news search for the doctor and hospital.
    """
    report = log or (lambda _text: None)
    article, paragraphs = _research_with_news_find_doctor_article(topic, report)
    article_title = str((article or {}).get("title") or "")
    article_text = "\n\n".join(paragraphs)[:6000]
    if article_title:
        report(
            "[research_with_the_news: using the Wikipedia article "
            f"{article_title!r} to identify the doctor's hospital]"
        )
    context = accumulated_context.strip() or "(no previous research context)"
    try:
        response = chat.generate_text(
            RESEARCH_WITH_NEWS_DOCTOR_PROMPT.format(
                topic=topic,
                accumulated_context=context,
                article=article_text or "(no Wikipedia article available)",
            ),
            model=RESEARCH_WITH_NEWS_MODEL,
            temperature=0.0,
            num_predict=160,
            stop=["\n\nQuestion:", "\n\nArticle:"],
        )
        profile = _research_with_news_parse_doctor_profile(response)
    except RuntimeError as exc:
        report(
            "[research_with_the_news: could not identify the doctor's "
            f"hospital: {exc}]"
        )
        profile = {
            "doctor": "",
            "hospital": "",
            "website": "",
            "specialty": "",
            "websites": [],
        }
    if (
        not profile.get("doctor")
        and article_title
        and not re.search(
            r"\b(?:hospital|medical center|clinic|health|university|"
            r"institute|school)\b",
            article_title,
            re.IGNORECASE,
        )
    ):
        profile["doctor"] = re.sub(
            r"\s*\([^)]*\)\s*$", "", article_title
        ).strip()
    mapped_websites = _research_with_news_hospital_websites(
        profile.get("hospital", "")
    )
    if mapped_websites:
        profile["websites"] = mapped_websites
        profile["website"] = mapped_websites[0]
        report(
            "[research_with_the_news: hospital_map matched "
            f"{profile.get('hospital')!r} -> "
            + ", ".join(mapped_websites)
            + "]"
        )
    else:
        if profile.get("hospital") and not profile.get("website"):
            profile["website"] = _research_with_news_hospital_website(
                profile.get("doctor", ""), profile["hospital"], report
            )
        profile["websites"] = (
            [profile["website"]] if profile.get("website") else []
        )
    profile["website_used"] = False
    details = [
        f"doctor={profile.get('doctor') or 'unknown'}",
        f"hospital={profile.get('hospital') or 'unknown'}",
        "website="
        + (
            ", ".join(profile.get("websites") or [])
            or str(profile.get("website") or "unknown")
        ),
    ]
    report(
        "[research_with_the_news: doctor profile: " + ", ".join(details) + "]"
    )
    return profile

def _research_with_news_name_tokens(value: str) -> list[str]:
    """Normalize a person's name for matching against an article title."""
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    normalized = "".join(
        char for char in normalized if not unicodedata.combining(char)
    )
    return [
        token
        for token in re.split(r"[^0-9a-z]+", normalized.casefold())
        if token
    ]

def _research_with_news_title_names_doctor(title: str, doctor: str) -> bool:
    """True when an article title contains the doctor's name.

    Google News titles often add credentials (``, MD``), drop a middle
    initial (``Daniel Knott, MD``), or reorder a name (``Knott, P. Daniel``).
    Match the full name first, then require every non-initial name token so
    those forms are still selected.
    """
    title_tokens = _research_with_news_name_tokens(title)
    doctor_tokens = _research_with_news_name_tokens(doctor)
    if not title_tokens or not doctor_tokens:
        return False
    title_text = " ".join(title_tokens)
    if f" {' '.join(doctor_tokens)} " in f" {title_text} ":
        return True
    significant_tokens = [
        token for token in doctor_tokens if len(token) > 1
    ] or doctor_tokens
    return all(token in title_tokens for token in significant_tokens)

def _research_with_news_doctor_title_candidates(
    doctor: str,
    website: str,
    limit: int,
    report: Callable[[str], None],
) -> list[dict]:
    """List hospital-website stories whose titles contain the doctor's name."""
    report(
        "[research_with_the_news: searching the hospital website "
        f"{website!r} for article titles containing {doctor!r}]"
    )
    try:
        candidates = search_research_with_news(
            f'"{doctor}"',
            limit=limit,
            website=website,
            lazy=True,
            require_content=False,
        )
    except RuntimeError as exc:
        report(
            "[research_with_the_news: could not read the hospital "
            f"website's article titles: {exc}]"
        )
        return []
    titles = [
        " ".join(str(item.get("title") or "").split())
        for item in candidates
    ]
    if titles:
        report(
            "[research_with_the_news: found "
            f"{len(titles)} hospital-website article title(s): "
            + " | ".join(title[:120] for title in titles if title)
            + "]"
        )
    matching = [
        item
        for item in candidates
        if _research_with_news_title_names_doctor(
            str(item.get("title") or ""), doctor
        )
    ]
    report(
        "[research_with_the_news: "
        f"{len(matching)} of {len(candidates)} article title(s) contain "
        f"the doctor's name {doctor!r}]"
    )
    return matching

def _research_with_news_fetch_doctor_article(
    candidate: dict,
    report: Callable[[str], None],
) -> dict | None:
    """Load the full body of one doctor title candidate."""
    link = str(candidate.get("link") or "").strip()
    title = " ".join(str(candidate.get("title") or "").split())
    if not link:
        return None
    try:
        payload = fetch_news_article(link, title)
    except (OSError, RuntimeError, ValueError) as exc:
        report(
            "[research_with_the_news: could not read the article "
            f"{title!r}: {exc}]"
        )
        return None
    paragraphs = _news_clean_paragraphs(payload.get("paragraphs") or [])
    summary = " ".join(str(candidate.get("summary") or "").split())
    if not paragraphs and summary:
        paragraphs = [summary]
    if not paragraphs:
        return None
    article = dict(candidate)
    article["title"] = str(payload.get("title") or title).strip() or title
    article["link"] = str(payload.get("link") or link).strip() or link
    article["paragraphs"] = paragraphs
    return article

def search_research_with_doctor(
    question: str,
    profile: dict[str, str],
    limit: int = RESEARCH_WITH_NEWS_ARTICLES_PER_QUESTION,
    log: Callable[[str], None] | None = None,
) -> list[dict]:
    """Search the doctor's hospital websites, then the news index."""
    report = log or (lambda _text: None)
    doctor = str(profile.get("doctor") or "").strip()
    hospital = str(profile.get("hospital") or "").strip()
    websites = [
        website
        for website in (
            _research_with_news_normalize_website(value)
            for value in (profile.get("websites") or [])
        )
        if website
    ]
    website = _research_with_news_normalize_website(
        str(profile.get("website") or "")
    )
    if website and not websites:
        websites.append(website)
    if not websites:
        websites = _research_with_news_hospital_websites(hospital)
    if websites:
        profile["websites"] = websites
        profile["website"] = websites[0]
    # The task requires the doctor's name in the title. Read each hospital
    # website's article-title list first, keep only titles that name the
    # doctor, and fetch those bodies. A hospital website can carry a doctor's
    # page without the name in the headline, so the question-based site search
    # below remains as a fallback when no title matches.
    if websites and doctor:
        selected: list[dict] = []
        saw_matching_titles = False
        for website in websites:
            if len(selected) >= limit:
                break
            matching_titles = _research_with_news_doctor_title_candidates(
                doctor,
                website,
                RESEARCH_WITH_NEWS_DOCTOR_TITLE_CANDIDATES,
                report,
            )
            if matching_titles:
                saw_matching_titles = True
            for candidate in matching_titles:
                if len(selected) >= limit:
                    break
                article = _research_with_news_fetch_doctor_article(
                    candidate, report
                )
                if article is None:
                    continue
                article["research_source"] = "hospital_website"
                selected.append(article)
        if selected:
            profile["website_used"] = True
            report(
                "[research_with_the_news: using "
                f"{len(selected)} hospital-website article(s) whose title "
                f"contains the doctor's name for {question!r}]"
            )
            return selected
        if saw_matching_titles:
            report(
                "[research_with_the_news: every matching hospital-website "
                "title had no readable article body; falling back to a "
                "question search on the hospital website]"
            )
    site_queries: list[str] = []
    if doctor:
        if doctor.casefold() in question.casefold():
            site_queries.append(question)
        else:
            site_queries.append(f'"{doctor}" {question}'.strip())
        site_queries.append(f'"{doctor}"')
    elif hospital:
        if hospital.casefold() in question.casefold():
            site_queries.append(question)
        else:
            site_queries.append(f'"{hospital}" {question}'.strip())
        site_queries.append(f'"{hospital}"')
    else:
        site_queries.append(question)
    for website in websites:
        seen_queries: set[str] = set()
        for site_query in site_queries:
            key = site_query.casefold()
            if not site_query or key in seen_queries:
                continue
            seen_queries.add(key)
            report(
                "[research_with_the_news: searching the hospital website "
                f"{website!r} for {site_query!r}]"
            )
            try:
                results = search_research_with_news(
                    site_query, limit=limit, website=website
                )
            except RuntimeError as exc:
                report(
                    "[research_with_the_news: no readable article on "
                    f"{website!r} for {site_query!r}: {exc}]"
                )
                continue
            profile["website_used"] = True
            for article in results:
                article["research_source"] = "hospital_website"
            return results
    query_parts = [
        value for value in (doctor, hospital, question) if value
    ]
    query = " ".join(query_parts) or question
    if websites:
        report(
            "[research_with_the_news: no hospital-website story was found; "
            f"falling back to the news index for {query!r}]"
        )
    results = search_research_with_news(query, limit=limit)
    for article in results:
        article["research_source"] = "news_index"
    return results

def plan_research_with_news_questions(
    topic: str,
    log: Callable[[str], None],
    accumulated_context: str = "",
) -> list[str]:
    """Ask the research model for one focused, time-neutral news question.

    ``accumulated_context`` is the conversation built by earlier turns in
    RESEARCH_WITH_NEWS_MODE. Passing it lets a follow-up such as "what does he
    think about AI?" become a concrete search question instead of an isolated,
    ambiguous query.
    """
    context = accumulated_context.strip() or "(no previous research context)"
    prompt = RESEARCH_WITH_NEWS_QUESTION_PROMPT.format(
        topic=topic, accumulated_context=context
    )
    questions: list[str] = []
    for attempt in range(2):
        try:
            response = chat.generate_text(
                prompt,
                model=RESEARCH_WITH_NEWS_MODEL,
                temperature=0.2,
                num_predict=160,
                stop=["\n\nTopic:", "\n\nQuestions:"],
            )
        except RuntimeError as exc:
            log(
                "[research_with_the_news: could not plan search questions: "
                f"{exc}]"
            )
            break
        questions = _research_with_news_clean_questions(response)
        if len(questions) >= RESEARCH_WITH_NEWS_QUESTIONS:
            return questions[:RESEARCH_WITH_NEWS_QUESTIONS]
        if attempt == 0:
            log(
                "[research_with_the_news: the planner used a time reference; "
                "asking again for time-neutral questions]"
            )
            prompt = (
                RESEARCH_WITH_NEWS_QUESTION_PROMPT.format(
                    topic=topic, accumulated_context=context
                )
                + "\nDo not include a year, month, weekday, season, clock "
                "time, or any word about recency. Return one question only.\n"
            )

    # The fallbacks are deliberately plain and contain no time reference, so
    # the news-research answer can still be produced if the planner is
    # unavailable or keeps adding dates.
    seen = {question.casefold() for question in questions}
    for fallback in (topic, f"overview of {topic}"):
        fallback = " ".join(fallback.split())
        if not fallback or fallback.casefold() in seen:
            continue
        questions.append(fallback)
        seen.add(fallback.casefold())
        if len(questions) >= RESEARCH_WITH_NEWS_QUESTIONS:
            break
    return questions[:RESEARCH_WITH_NEWS_QUESTIONS]

def search_research_with_news(
    question: str,
    limit: int = RESEARCH_WITH_NEWS_ARTICLES_PER_QUESTION,
    website: str = "",
    lazy: bool = False,
    require_content: bool = True,
) -> list[dict]:
    """Fetch the top Google News stories and their full article bodies.

    ``website`` restricts the Google News query to one site with the
    ``site:`` operator. This is how doctor research reads a hospital's own
    website while still using the existing article fetcher and parser.
    ``lazy`` asks news_fetch.php for the list without waiting on every article
    page. ``require_content`` may be turned off for that title-only list.
    """
    script = SCRIPT_DIR / "news_fetch.php"
    if not script.exists():
        raise RuntimeError("missing news_fetch.php")
    query = question
    website = _research_with_news_normalize_website(website)
    if website:
        query = f"site:{website} {question}"
    command = [
        "php",
        str(script),
        "--json",
        "--source",
        "googlenews",
        "--search",
        query,
        "--limit",
        str(limit),
    ]
    if lazy:
        command.append("--lazy")
    try:
        result = subprocess.run(
            command,
            cwd=SCRIPT_DIR,
            capture_output=True,
            text=True,
            timeout=240,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"could not run news_fetch.php: {exc}") from exc

    articles: list[dict] = []
    seen: set[str] = set()
    for item in _read_news_ndjson(result.stdout):
        title = " ".join(str(item.get("title") or "").split())
        link = str(item.get("link") or "").strip()
        if not title:
            continue
        paragraphs = _news_clean_paragraphs(item.get("paragraphs") or [])
        summary = " ".join(str(item.get("summary") or "").split())
        if not paragraphs and summary:
            paragraphs = [summary]
        if require_content and not paragraphs:
            continue
        key = (link or title).casefold()
        if key in seen:
            continue
        seen.add(key)
        articles.append(
            {
                "title": title,
                "link": link,
                "published": str(item.get("published") or "").strip(),
                "source": str(item.get("source") or "").strip(),
                "summary": summary,
                "paragraphs": paragraphs,
            }
        )
        if len(articles) >= limit:
            break
    if not articles:
        detail = (result.stderr or result.stdout or "").strip()
        detail = detail.splitlines()[-1] if detail else "no results"
        raise RuntimeError(f"no readable stories were found ({detail})")
    return articles

def build_research_with_news_context(
    articles: list[dict],
    total_chars: int = RESEARCH_WITH_NEWS_CONTEXT_CHARS,
) -> str:
    """Format fetched stories into a bounded context for the research model."""
    blocks: list[str] = []
    remaining = max(0, int(total_chars))
    for index, article in enumerate(articles, start=1):
        if remaining <= 0:
            break
        title = " ".join(str(article.get("title") or "").split())
        body = "\n\n".join(article.get("paragraphs") or [])
        body = body.strip()
        if not body:
            continue
        budget = min(RESEARCH_WITH_NEWS_ARTICLE_CHARS, remaining)
        if len(body) > budget:
            body = body[:budget].rstrip()
            cut = body.rfind(" ")
            if cut > budget // 2:
                body = body[:cut].rstrip()
            body += " ..."
        if not body:
            continue
        question = " ".join(str(article.get("question") or "").split())
        lines = [f'<article id="{index}">', f"<title>{title}</title>"]
        if question:
            lines.append(f"<search_question>{question}</search_question>")
        published = str(article.get("published") or "").strip()
        if published:
            lines.append(f"<published>{published}</published>")
        link = str(article.get("link") or "").strip()
        if link:
            lines.append(f"<url>{link}</url>")
        lines.extend(["<content>", body, "</content>", "</article>"])
        block = "\n".join(lines)
        blocks.append(block)
        remaining -= len(body) + 120
    return "\n\n".join(blocks)


# Cross-component imports are placed after the definitions so a
# feature module can depend on another feature module safely.
from .books import _read_news_ndjson
from .core import SCRIPT_DIR
from .map import WIKIPEDIA_API_URL
from .news import fetch_news_article
from .retrieval import _news_clean_paragraphs
