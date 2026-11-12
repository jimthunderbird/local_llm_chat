#!/usr/bin/env php
<?php

declare(strict_types=1);

/**
 * Fetch the latest stories from every news source supported by /news and
 * stream each story's title + paragraphs + source images, source by source.
 *
 * Sources:
 *   wsj           Wall Street Journal markets, business, tech, world, US and
 *                 social-economy RSS feeds published by Dow Jones
 *   cnn           CNN RSS feeds served by openrss.org (top/world/us/politics/business)
 *   cbslite       CBC Lite homepage (Next.js story list), with full CBC
 *                 article pages fetched for the detail view
 *   nprtext       NPR's text-only homepage, with full article pages fetched
 *                 for the detail view
 *   yahoo_finance Yahoo Finance stock market news via Google News RSS
 *   medium        Medium stories via the Google News RSS site:medium.com feed
 *   kqed          KQED News RSS feed
 *   sfgate        SFGATE Bay Area News RSS feed
 *   sfbayview     SF Bay View WordPress REST API
 *   mission_local Mission Local WordPress REST API
 *   richmond_sunset Richmond Review / Richmond & Sunset news RSS feed
 *   techcrunch    TechCrunch WordPress REST API
 *   krasia        KrASIA RSS feed (Asian tech and startup news)
 *   wccftech      Wccftech RSS feed (hardware, mobile and gaming news)
 *   hackernews    Hacker News RSS feed (front-page technology stories)
 *   medium_author The Medium author feed configured in ~/.medium/username
 *                 (https://medium.com/feed/@username), used by /medium
 *   googlenews    A Google News RSS search created with --search <keywords>,
 *                 used by /googlenews <keywords>
 *
 * Default output is a human-readable stream for the terminal. The web chat
 * passes --json and receives one JSON event per line (NDJSON), which
 * app/chat_server.py relays to app/chat.html as it arrives.
 *
 * Usage:
 *   ./news_fetch.php [--json] [--source SOURCE ...] [--limit N]
 *   ./news_fetch.php [--json] --search <keywords> [--limit N]
 *   ./news_fetch.php [--json] --article <url> [--title <headline>]
 *
 * --source accepts wsj, cnn, cbslite, nprtext, yahoo_finance, medium, kqed,
 * sfgate, sfbayview, mission_local, richmond_sunset, techcrunch, krasia,
 * wccftech, hackernews, medium_author, or googlenews and may be repeated (or
 * given as a comma separated list).
 * --article fetches one story body on demand; --title lets it match a
 * syndicated full-text copy when the publisher page is blocked.
 * --search creates the dynamic googlenews source for the given keywords. It
 * is selected automatically when no --source is given.
 * Without --source, every static source is fetched and emitted as its own
 * section; medium_author and googlenews are only used when explicitly
 * selected.
 * --limit caps the total number of stories across all selected sources (each
 * source has its own default when --limit is omitted).
 */

const SOURCES = [
    'wsj' => [
        'label' => 'The Wall Street Journal',
        'kind' => 'rss',
        // The WSJ RSS feeds carry only a short deck, and the article pages
        // themselves are paywalled/blocked (HTTP 401), so use the feed deck
        // as the detail body instead of forcing a full-article fetch.
        'default_limit' => 60,
        // Each WSJ feed is large, so take a balanced sample from every
        // section instead of letting the Markets feed fill the whole quota.
        'per_feed_limit' => 10,
        'feeds' => [
            'markets' => [
                'label' => 'Markets',
                'url' => 'https://feeds.content.dowjones.io/public/rss/RSSMarketsMain',
            ],
            'business' => [
                'label' => 'US Business',
                'url' => 'https://feeds.content.dowjones.io/public/rss/WSJcomUSBusiness',
            ],
            'tech' => [
                'label' => 'Tech',
                'url' => 'https://feeds.content.dowjones.io/public/rss/RSSWSJD',
            ],
            'world' => [
                'label' => 'World News',
                'url' => 'https://feeds.content.dowjones.io/public/rss/RSSWorldNews',
            ],
            'us' => [
                'label' => 'US News',
                'url' => 'https://feeds.content.dowjones.io/public/rss/RSSUSnews',
            ],
            'economy' => [
                'label' => 'Social Economy',
                'url' => 'https://feeds.content.dowjones.io/public/rss/socialeconomyfeed',
            ],
        ],
    ],
    'cnn' => [
        'label' => 'CNN News',
        'kind' => 'rss',
        'default_limit' => 50,
        'feeds' => [
            'top' => [
                'label' => 'Top Stories',
                'url' => 'https://openrss.org/feed/www.cnn.com',
            ],
            'world' => [
                'label' => 'World',
                'url' => 'https://openrss.org/feed/www.cnn.com/world',
            ],
            'us' => [
                'label' => 'US',
                'url' => 'https://openrss.org/feed/www.cnn.com/us',
            ],
            'politics' => [
                'label' => 'Politics',
                'url' => 'https://openrss.org/feed/www.cnn.com/politics',
            ],
            'business' => [
                'label' => 'Business',
                'url' => 'https://openrss.org/feed/www.cnn.com/business',
            ],
        ],
    ],
    'cbslite' => [
        'label' => 'CBC Lite',
        'kind' => 'cbc_lite',
        // The CBC Lite homepage is a Next.js page whose __NEXT_DATA__ payload
        // lists the latest stories. Fetch each linked CBC article page for the
        // detail view because the list only carries headlines and metadata.
        'full_article' => true,
        'default_limit' => 20,
        'feeds' => [
            'top' => [
                'label' => 'Top Stories',
                'url' => 'https://www.cbc.ca/lite/news',
            ],
        ],
    ],
    'nprtext' => [
        'label' => 'NPR (Text)',
        'kind' => 'npr_text',
        // The text-only homepage lists headlines that link to lightweight
        // article pages; fetch those pages for the full body in the detail
        // view because the list carries no story text.
        'full_article' => true,
        'default_limit' => 20,
        'feeds' => [
            'top' => [
                'label' => 'Top Stories',
                'url' => 'https://text.npr.org/',
            ],
        ],
    ],
    'yahoo_finance' => [
        'label' => 'Yahoo Finance',
        'kind' => 'rss',
        'default_limit' => 20,
        'feeds' => [
            'latest' => [
                'label' => 'Stock Market',
                'url' => 'https://news.google.com/rss/search?q=site:finance.yahoo.com+stock+market&hl=en-US&gl=US&ceid=US:en',
            ],
        ],
    ],
    'medium' => [
        'label' => 'Medium',
        'kind' => 'rss',
        // Google News only carries the headline, and Medium challenges direct
        // page requests, so every story is completed through the reader
        // mirror for the detail view.
        'full_article' => true,
        'default_limit' => 20,
        'feeds' => [
            'latest' => [
                'label' => 'Medium stories',
                'url' => 'https://news.google.com/rss/search?q=site:medium.com&hl=en-US&gl=US&ceid=US:en',
            ],
        ],
    ],
    'kqed' => [
        'label' => 'KQED News',
        'kind' => 'rss',
        'default_limit' => 20,
        'feeds' => [
            'news' => [
                'label' => 'KQED News',
                'url' => 'https://ww2.kqed.org/news/feed/',
            ],
        ],
    ],
    'sfgate' => [
        'label' => 'SFGATE Bay Area News',
        'kind' => 'rss',
        'default_limit' => 20,
        'feeds' => [
            'bayarea' => [
                'label' => 'Bay Area News',
                'url' => 'https://www.sfgate.com/bayarea/feed/Bay-Area-News-429.php',
            ],
        ],
    ],
    'sfbayview' => [
        'label' => 'SF Bay View',
        'kind' => 'wp_json',
        'default_limit' => 10,
        'feeds' => [
            'latest' => [
                'label' => 'Latest Stories',
                'url' => 'https://sfbayview.com/wp-json/wp/v2/posts',
            ],
        ],
    ],
    'mission_local' => [
        'label' => 'Mission Local',
        'kind' => 'wp_json',
        'default_limit' => 10,
        'feeds' => [
            'latest' => [
                'label' => 'Latest Stories',
                'url' => 'https://missionlocal.org/wp-json/wp/v2/posts',
            ],
        ],
    ],
    'richmond_sunset' => [
        'label' => 'Richmond Review',
        'kind' => 'rss',
        'default_limit' => 20,
        'feeds' => [
            'latest' => [
                'label' => 'Latest Stories',
                'url' => 'https://richmondsunsetnews.com/feed/',
            ],
        ],
    ],
    'techcrunch' => [
        'label' => 'TechCrunch',
        'kind' => 'wp_json',
        'default_limit' => 10,
        'feeds' => [
            'latest' => [
                'label' => 'Latest Stories',
                'url' => 'https://techcrunch.com/wp-json/wp/v2/posts',
            ],
        ],
    ],
    'krasia' => [
        'label' => 'KrASIA',
        'kind' => 'rss',
        // KrASIA's feed only carries a shortened excerpt, so always fetch the
        // article page for the detail view instead of trusting feed content.
        'full_article' => true,
        'default_limit' => 20,
        'feeds' => [
            'latest' => [
                'label' => 'Latest Stories',
                'url' => 'https://console.kr-asia.com/feed',
            ],
        ],
    ],
    'wccftech' => [
        'label' => 'Wccftech',
        'kind' => 'rss',
        // Wccftech's feed truncates each story and links to the full article,
        // so fetch the article page for the detail view.
        'full_article' => true,
        'default_limit' => 20,
        'feeds' => [
            'latest' => [
                'label' => 'Latest Stories',
                'url' => 'https://wccftech.com/feed/',
            ],
        ],
    ],
    'hackernews' => [
        'label' => 'Hacker News',
        'kind' => 'rss',
        // The Hacker News feed carries only the headline and a Comments
        // link, so fetch the linked article for the detail view.
        'full_article' => true,
        'default_limit' => 10,
        'feeds' => [
            'latest' => [
                'label' => 'Front Page',
                'url' => 'https://news.ycombinator.com/rss',
            ],
        ],
    ],
];

const USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
    . 'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';

// Some publisher WAFs reject a specific browser identity (PBS answers the
// Chrome 131 UA with an HTTP 202 JavaScript challenge) while serving the same
// article to another mainstream browser. Retry a blocked article page with
// these identities before giving up on the direct publisher page.
const ARTICLE_FALLBACK_USER_AGENTS = [
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        . 'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15',
    'Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0',
    'Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)',
];

/**
 * Read the Medium author handle from ~/.medium/username.
 *
 * A leading "@" is optional; the value is stored as-is by the user.
 */
function mediumUsername(): string
{
    $home = getenv('HOME');
    if ($home === false || $home === '') {
        $home = (string)($_SERVER['HOME'] ?? '');
    }
    if ($home === '') {
        return '';
    }
    $path = rtrim($home, DIRECTORY_SEPARATOR) . DIRECTORY_SEPARATOR
        . '.medium' . DIRECTORY_SEPARATOR . 'username';
    if (!is_readable($path)) {
        return '';
    }
    $username = trim((string)file_get_contents($path));
    return ltrim($username, '@');
}

/**
 * Build the dynamic Medium author source used by /medium.
 *
 * Returns null when ~/.medium/username is missing or empty so that /news can
 * keep working with only the static sources.
 */
function mediumSource(): ?array
{
    $username = mediumUsername();
    if ($username === '') {
        return null;
    }
    return [
        'label' => 'Medium',
        'kind' => 'rss',
        // The author feed carries only a snippet plus a "Continue reading"
        // link, so always complete the story from the reader mirror.
        'full_article' => true,
        'default_limit' => 10,
        'feeds' => [
            'latest' => [
                'label' => '@' . $username,
                'url' => 'https://medium.com/feed/@' . rawurlencode($username),
            ],
        ],
    ];
}

/**
 * Return every source available at runtime: the static feeds plus the
 * configured Medium author feed.
 */
function googleNewsQuery(): string
{
    return trim((string)($GLOBALS['NEWS_GOOGLE_NEWS_QUERY'] ?? ''));
}

/**
 * Build the dynamic Google News RSS source used by /googlenews <keywords>.
 *
 * Returns null until --search supplies a query, so ordinary /news output and
 * the static source list are unchanged.
 */
function googleNewsSource(): ?array
{
    $query = googleNewsQuery();
    if ($query === '') {
        return null;
    }
    return [
        'label' => 'Google News',
        'kind' => 'rss',
        'default_limit' => 20,
        'feeds' => [
            'latest' => [
                'label' => $query,
                'url' => 'https://news.google.com/rss/search?q='
                    . rawurlencode($query)
                    . '&hl=en-US&gl=US&ceid=US:en',
            ],
        ],
    ];
}

/**
 * Build exact-phrase matchers for quoted text in a Google News query.
 *
 * A query such as /news search "jensen huang" asks for the complete phrase,
 * but the Google News RSS ranking is fuzzy and can return stories that only
 * mention one of the words. The query is still sent to Google with its quotes
 * (for better ranking); these patterns are then used to keep only stories
 * whose title or summary really contains every quoted phrase.
 *
 * @return list<string> PCRE patterns
 */
function quotedSearchPhrasePatterns(string $query): array
{
    if ($query === '') {
        return [];
    }
    if (!preg_match_all(
        '/"([^"]+)"|\x{201C}([^\x{201D}]+)\x{201D}|\x{2018}([^\x{2019}]+)\x{2019}|\'([^\']+)\'/u',
        $query,
        $matches,
        PREG_SET_ORDER
    )) {
        return [];
    }

    $patterns = [];
    foreach ($matches as $match) {
        $phrase = '';
        foreach (array_slice($match, 1) as $candidate) {
            if ($candidate !== '') {
                $phrase = $candidate;
                break;
            }
        }
        $words = preg_split('/\s+/u', trim($phrase), -1, PREG_SPLIT_NO_EMPTY);
        if ($words === false || $words === []) {
            continue;
        }
        // Words in a quoted phrase must stay adjacent, but punctuation between
        // them (Huang's, Jensen-Huang) is allowed.
        $body = implode(
            '[^\\p{L}\\p{N}]+',
            array_map(
                static fn(string $word): string => preg_quote($word, '/'),
                $words
            )
        );
        $pattern = '/(?<![\\p{L}\\p{N}])' . $body . '(?![\\p{L}\\p{N}])/iu';
        if (!in_array($pattern, $patterns, true)) {
            $patterns[] = $pattern;
        }
    }
    return $patterns;
}

/**
 * Normalise story text before checking it against quoted query phrases.
 */
function normalizeQuotedSearchText(string $text): string
{
    $text = html_entity_decode(strip_tags($text), ENT_QUOTES | ENT_HTML5, 'UTF-8');
    $text = str_replace(
        ["\xE2\x80\x98", "\xE2\x80\x99", "\xCA\xBC"],
        "'",
        $text
    );
    $text = str_replace(
        ["\xE2\x80\x9C", "\xE2\x80\x9D"],
        '"',
        $text
    );
    $text = mb_strtolower($text, 'UTF-8');
    $text = preg_replace('/\s+/u', ' ', $text) ?? $text;
    return trim($text);
}

/**
 * True when a story contains every quoted phrase from the search query.
 *
 * @param list<string> $patterns
 */
function storyMatchesQuotedPhrases(array $story, array $patterns): bool
{
    if ($patterns === []) {
        return true;
    }
    $haystack = normalizeQuotedSearchText(
        (string)($story['title'] ?? '') . ' ' . (string)($story['summary'] ?? '')
    );
    foreach ($patterns as $pattern) {
        if (preg_match($pattern, $haystack) !== 1) {
            return false;
        }
    }
    return true;
}

/**
 * Return every source available at runtime: the static feeds plus the
 * configured Medium author feed and Google News query.
 */
function newsSources(): array
{
    static $sources = null;
    if ($sources === null) {
        $sources = SOURCES;
        $medium = mediumSource();
        if ($medium !== null) {
            $sources['medium_author'] = $medium;
        }
        $googleNews = googleNewsSource();
        if ($googleNews !== null) {
            $sources['googlenews'] = $googleNews;
        }
    }
    return $sources;
}

const CONTENT_NS = 'http://purl.org/rss/1.0/modules/content/';
const MAX_ATTEMPTS = 4;
const MAX_WP_PER_PAGE = 100;
const MAX_STORY_IMAGES = 6;
const IMAGE_CACHE_TTL = 604800;       // one week for successful lookups
const IMAGE_CACHE_FAILURE_TTL = 86400; // one day for missing/broken images
const IMAGE_FETCH_CONCURRENCY = 6;
const ARTICLE_CACHE_TTL = 21600;       // six hours for successfully fetched articles
const ARTICLE_CACHE_FAILURE_TTL = 3600; // one hour for articles we could not load
// A reader mirror is a remote, occasionally rate-limited service. Retry a
// failed mirror lookup on the next news request instead of hiding the story
// for a full hour.
const ARTICLE_READER_FAILURE_TTL = 120;
const ARTICLE_EXTRACTOR_VERSION = 9;   // bump when article extraction logic changes
const ARTICLE_MIN_BODY_CHARS = 1200;   // a container this large is a real body, not metadata
const ARTICLE_PARTIAL_BODY_CHARS = 1200; // shorter fallback bodies may be previews
const ARTICLE_FETCH_CONCURRENCY = 6;
const MAX_ARTICLE_CACHE_ENTRIES = 500;
// Some publishers put the complete story in a public RSS feed while the
// article page itself is behind a bot challenge. Feeds also give us the lead
// image and a usable summary when the full body stays blocked. Keyed by host
// suffix; every domain may expose more than one feed.
const ARTICLE_FEED_FALLBACKS = [
    'axios.com' => ['https://api.axios.com/feed/'],
    'nytimes.com' => [
        'https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml',
        'https://rss.nytimes.com/services/xml/rss/nyt/Business.xml',
        'https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml',
    ],
    'ft.com' => ['https://www.ft.com/rss/home'],
    'newsnationnow.com' => ['https://www.newsnationnow.com/feed/'],
];
// Some publishers block article pages entirely (Cloudflare) and their public
// feeds only carry a preview, so a reader mirror that renders the full story
// is used as a last resort. Keyed by host suffix; {url} is the article URL.
const ARTICLE_READER_FALLBACKS = [
    'medium.com' => [
        'https://freedium-mirror.cfd/{url}',
    ],
];

/**
 * Print a diagnostic line to stderr without disturbing the event stream.
 */
function diag(string $message): void
{
    fwrite(STDERR, $message . "\n");
}

/**
 * Write one NDJSON event and flush immediately so the consumer can render it.
 */
function emitEvent(array $event): void
{
    fwrite(
        STDOUT,
        json_encode($event, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES) . "\n"
    );
    fflush(STDOUT);
}

/**
 * Download a feed or JSON endpoint with a browser-like Accept header.
 *
 * openrss.org rate-limits bursts, so HTTP 429/5xx responses are retried with
 * exponential backoff. The same retry policy also covers transient network
 * failures from the other sources.
 */
function fetchUrl(
    string $url,
    string $accept,
    string $sourceKey,
    int $attempt = 0,
    bool $quiet = false,
    int $timeout = 30,
    string $userAgent = USER_AGENT
): ?string
{
    $context = stream_context_create([
        'http' => [
            'method' => 'GET',
            'header' => 'User-Agent: ' . $userAgent . "\r\n"
                . 'Accept: ' . $accept . "\r\n"
                . "Accept-Language: en-US,en;q=0.9\r\n",
            'ignore_errors' => true,
            'timeout' => $timeout,
        ],
    ]);

    $body = @file_get_contents($url, false, $context);
    $headers = $http_response_header ?? [];
    $statusLine = $headers[0] ?? '';
    preg_match('{HTTP/\S*\s(\d{3})}', $statusLine, $match);
    $status = isset($match[1]) ? (int)$match[1] : ($body === false ? 500 : 200);

    if ($body !== false && $status >= 200 && $status < 400) {
        return $body;
    }

    $retryable = $status === 429 || $status >= 500 || $body === false;
    if ($retryable && $attempt + 1 < MAX_ATTEMPTS) {
        $wait = 2 ** ($attempt + 1); // 2s, 4s, 8s
        if (!$quiet) {
            diag("news_fetch ({$sourceKey}): {$url} returned HTTP {$status}; retrying in {$wait}s");
        }
        sleep($wait);
        return fetchUrl(
            $url,
            $accept,
            $sourceKey,
            $attempt + 1,
            $quiet,
            $timeout,
            $userAgent
        );
    }

    if (!$quiet) {
        diag("news_fetch ({$sourceKey}): could not download {$url} (HTTP {$status})");
    }
    return null;
}

/**
 * Collapse HTML entities and whitespace into clean text.
 */
function normalizeText(string $html): string
{
    $text = html_entity_decode($html, ENT_QUOTES | ENT_HTML5, 'UTF-8');
    $text = str_replace("\xC2\xA0", ' ', $text); // non-breaking spaces
    $text = preg_replace('/\s+/u', ' ', $text) ?? $text;
    return trim($text);
}

/**
 * Normalise a title that may contain HTML markup.
 */
function cleanTitle(string $html): string
{
    $text = normalizeText(strip_tags($html));
    return $text !== '' ? $text : '(untitled)';
}

/**
 * Normalise an image URL and resolve relative references against a page URL.
 */
function normalizeImageUrl(string $url, string $baseUrl = ''): string
{
    $url = html_entity_decode(trim($url), ENT_QUOTES | ENT_HTML5, 'UTF-8');
    if ($url === '' || preg_match('{^(?:data|blob|javascript|mailto):}i', $url)) {
        return '';
    }
    if (str_starts_with($url, '//')) {
        return 'https:' . $url;
    }
    if (!preg_match('{^https?://}i', $url)) {
        if ($baseUrl === '') {
            return '';
        }
        $parts = parse_url($baseUrl);
        if (!is_array($parts) || empty($parts['scheme']) || empty($parts['host'])) {
            return '';
        }
        $origin = $parts['scheme'] . '://' . $parts['host'];
        if (isset($parts['port'])) {
            $origin .= ':' . $parts['port'];
        }
        if (str_starts_with($url, '/')) {
            return $origin . $url;
        }
        $path = str_replace('\\', '/', $parts['path'] ?? '/');
        $directory = str_ends_with($path, '/')
            ? rtrim($path, '/')
            : rtrim(dirname($path), '/');
        return $origin . ($directory === '' || $directory === '.' ? '' : $directory)
            . '/' . ltrim($url, '/');
    }
    return $url;
}

/**
 * Reject obvious non-image media URLs while allowing extension-less CDN URLs.
 */
function looksLikeImageUrl(string $url): bool
{
    if (preg_match('/\.(?:jpe?g|png|gif|webp|avif|bmp|svg)(?:[?#].*)?$/i', $url)) {
        return true;
    }
    return !preg_match(
        '/\.(?:mp4|m3u8|mp3|webm|mov|pdf)(?:[?#].*)?$/i',
        $url
    );
}

/**
 * Pick the largest candidate from an HTML srcset attribute.
 */
function largestSrcsetUrl(string $srcset): string
{
    $best = '';
    $bestWeight = -1;
    foreach (explode(',', $srcset) as $candidate) {
        $parts = preg_split('/\s+/', trim($candidate));
        if (!$parts || $parts[0] === '') {
            continue;
        }
        $weight = 0;
        if (isset($parts[1]) && preg_match('/^(\d+)w$/', $parts[1], $match)) {
            $weight = (int)$match[1];
        } elseif (isset($parts[1]) && preg_match('/^([\d.]+)x$/', $parts[1], $match)) {
            $weight = (int)((float)$match[1] * 1000);
        }
        if ($weight > $bestWeight) {
            $bestWeight = $weight;
            $best = $parts[0];
        }
    }
    return $best;
}

/**
 * Pull image URLs from a story's HTML (feed content or article body).
 *
 * @return list<string>
 */
function imageUrlsFromHtml(
    string $html,
    string $baseUrl = '',
    int $limit = MAX_STORY_IMAGES
): array {
    if (trim($html) === '') {
        return [];
    }

    $previous = libxml_use_internal_errors(true);
    $document = new DOMDocument();
    $loaded = $document->loadHTML(
        '<?xml encoding="utf-8"?><div>' . $html . '</div>',
        LIBXML_NONET | LIBXML_NOERROR | LIBXML_NOWARNING
    );
    libxml_clear_errors();
    libxml_use_internal_errors($previous);
    if (!$loaded) {
        return [];
    }

    $images = [];
    $seen = [];
    foreach ($document->getElementsByTagName('img') as $node) {
        $width = $node->getAttribute('width');
        $height = $node->getAttribute('height');
        if (
            (ctype_digit($width) && (int)$width <= 2)
            || (ctype_digit($height) && (int)$height <= 2)
        ) {
            continue;
        }

        $candidates = [
            $node->getAttribute('src'),
            $node->getAttribute('data-src'),
            $node->getAttribute('data-original'),
            $node->getAttribute('data-lazy-src'),
            $node->getAttribute('data-url'),
        ];
        $candidate = '';
        foreach ($candidates as $value) {
            if (trim($value) !== '') {
                $candidate = $value;
                break;
            }
        }
        if ($candidate === '') {
            $candidate = largestSrcsetUrl($node->getAttribute('srcset'));
        }

        $url = normalizeImageUrl($candidate, $baseUrl);
        if ($url === '' || isset($seen[$url])) {
            continue;
        }
        $seen[$url] = true;
        $images[] = $url;
        if (count($images) >= $limit) {
            break;
        }
    }
    return $images;
}

/**
 * Pull image URLs from RSS media:content, media:thumbnail, and enclosures.
 *
 * @return list<string>
 */
function mediaImageUrls(SimpleXMLElement $item, string $baseUrl = ''): array
{
    $xml = $item->asXML();
    if ($xml === false || trim($xml) === '') {
        return [];
    }

    $previous = libxml_use_internal_errors(true);
    $document = new DOMDocument();
    $loaded = $document->loadXML(
        $xml,
        LIBXML_NONET | LIBXML_NOERROR | LIBXML_NOWARNING
    );
    libxml_clear_errors();
    libxml_use_internal_errors($previous);
    if (!$loaded) {
        return [];
    }

    $xpath = new DOMXPath($document);
    $nodes = $xpath->query(
        '//*[local-name()="content" or local-name()="thumbnail" '
        . 'or local-name()="enclosure"]'
    );
    if ($nodes === false) {
        return [];
    }

    $images = [];
    $seen = [];
    foreach ($nodes as $node) {
        if (!$node instanceof DOMElement) {
            continue;
        }
        $url = normalizeImageUrl($node->getAttribute('url'), $baseUrl);
        if ($url === '') {
            continue;
        }
        $type = strtolower(trim($node->getAttribute('type')));
        $medium = strtolower(trim($node->getAttribute('medium')));
        if ($type !== '' && !str_starts_with($type, 'image/')) {
            continue;
        }
        if ($medium !== '' && $medium !== 'image') {
            continue;
        }
        if (!looksLikeImageUrl($url) || isset($seen[$url])) {
            continue;
        }
        $seen[$url] = true;
        $images[] = $url;
        if (count($images) >= MAX_STORY_IMAGES) {
            break;
        }
    }
    return $images;
}

/**
 * Merge image lists, dropping duplicates and capping the result.
 *
 * @param list<string> ...$lists
 * @return list<string>
 */
function mergeImageUrls(array ...$lists): array
{
    $merged = [];
    $seen = [];
    foreach ($lists as $list) {
        foreach ($list as $url) {
            $url = normalizeImageUrl((string)$url);
            if ($url === '' || isset($seen[$url])) {
                continue;
            }
            $seen[$url] = true;
            $merged[] = $url;
            if (count($merged) >= MAX_STORY_IMAGES) {
                return $merged;
            }
        }
    }
    return $merged;
}

/**
 * Find the social/hero image on an article page (og:image first, then a
 * reasonable in-article fallback).
 */
function extractArticleImageFromHtml(string $html, string $pageUrl): string
{
    if (trim($html) === '') {
        return '';
    }

    $previous = libxml_use_internal_errors(true);
    $document = new DOMDocument();
    $loaded = $document->loadHTML(
        '<?xml encoding="utf-8"?>' . $html,
        LIBXML_NONET | LIBXML_NOERROR | LIBXML_NOWARNING
    );
    libxml_clear_errors();
    libxml_use_internal_errors($previous);
    if (!$loaded) {
        return '';
    }

    foreach ($document->getElementsByTagName('meta') as $meta) {
        $name = strtolower(
            $meta->getAttribute('property') !== ''
                ? $meta->getAttribute('property')
                : $meta->getAttribute('name')
        );
        if (!in_array(
            $name,
            ['og:image', 'og:image:url', 'og:image:secure_url',
             'twitter:image', 'twitter:image:src'],
            true
        )) {
            continue;
        }
        $url = normalizeImageUrl($meta->getAttribute('content'), $pageUrl);
        if ($url !== '' && looksLikeImageUrl($url)) {
            return $url;
        }
    }

    foreach ($document->getElementsByTagName('link') as $link) {
        if (strtolower($link->getAttribute('rel')) !== 'image_src') {
            continue;
        }
        $url = normalizeImageUrl($link->getAttribute('href'), $pageUrl);
        if ($url !== '' && looksLikeImageUrl($url)) {
            return $url;
        }
    }

    $xpath = new DOMXPath($document);
    foreach (['//article//img', '//main//img'] as $query) {
        $nodes = $xpath->query($query);
        if ($nodes === false) {
            continue;
        }
        foreach ($nodes as $node) {
            if (!$node instanceof DOMElement) {
                continue;
            }
            $url = normalizeImageUrl($node->getAttribute('src'), $pageUrl);
            if ($url !== '' && looksLikeImageUrl($url)) {
                return $url;
            }
        }
    }
    return '';
}

/**
 * Locate the on-disk cache of article hero images.
 */
function imageCacheFile(): string
{
    $home = getenv('HOME');
    if ($home === false || $home === '') {
        $home = sys_get_temp_dir();
    }
    $directory = rtrim($home, '/') . '/.cache/news_fetch';
    if (!is_dir($directory)) {
        @mkdir($directory, 0775, true);
    }
    return $directory . '/article_images.json';
}

/**
 * @return array<string, array{image:string, ts:int}>
 */
function loadImageCache(): array
{
    $file = imageCacheFile();
    if (!is_file($file)) {
        return [];
    }
    $raw = @file_get_contents($file);
    if ($raw === false || trim($raw) === '') {
        return [];
    }
    $data = json_decode($raw, true);
    return is_array($data) ? $data : [];
}

/**
 * @param array<string, array{image:string, ts:int}> $cache
 */
function saveImageCache(array $cache): void
{
    $file = imageCacheFile();
    @file_put_contents(
        $file,
        json_encode($cache, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE),
        LOCK_EX
    );
}

/**
 * Fetch several article pages in parallel and extract one hero image from
 * each. Results are keyed by the original array index.
 *
 * @param array<int, string> $links
 * @return array<int, string>
 */
function fetchArticleImages(array $links, string $sourceKey): array
{
    $results = [];
    foreach (array_chunk($links, IMAGE_FETCH_CONCURRENCY, true) as $chunk) {
        $multi = curl_multi_init();
        $handles = [];
        foreach ($chunk as $index => $link) {
            $handle = curl_init($link);
            if ($handle === false) {
                $results[$index] = '';
                continue;
            }
            curl_setopt_array($handle, [
                CURLOPT_RETURNTRANSFER => true,
                CURLOPT_FOLLOWLOCATION => true,
                CURLOPT_MAXREDIRS => 3,
                CURLOPT_CONNECTTIMEOUT => 8,
                CURLOPT_TIMEOUT => 15,
                CURLOPT_USERAGENT => USER_AGENT,
                CURLOPT_ENCODING => '',
                CURLOPT_HTTPHEADER => [
                    'Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Accept-Language: en-US,en;q=0.9',
                ],
            ]);
            curl_multi_add_handle($multi, $handle);
            $handles[$index] = $handle;
        }

        $running = null;
        do {
            $status = curl_multi_exec($multi, $running);
            if ($running > 0) {
                if (curl_multi_select($multi, 1.0) === -1) {
                    usleep(100000);
                }
            }
        } while ($running > 0 && $status === CURLM_OK);

        foreach ($handles as $index => $handle) {
            $body = curl_multi_getcontent($handle);
            $httpCode = (int)curl_getinfo($handle, CURLINFO_RESPONSE_CODE);
            $error = curl_error($handle);
            curl_multi_remove_handle($multi, $handle);
            curl_close($handle);

            if (
                is_string($body) && $body !== ''
                && $httpCode >= 200 && $httpCode < 400
            ) {
                $results[$index] = extractArticleImageFromHtml(
                    $body,
                    $chunk[$index]
                );
            } else {
                $detail = $error !== '' ? ", {$error}" : '';
                diag(
                    "news_fetch ({$sourceKey}): could not load images from "
                    . "{$chunk[$index]} (HTTP {$httpCode}{$detail})"
                );
                $results[$index] = '';
            }
        }
        curl_multi_close($multi);
    }
    return $results;
}

/**
 * Give stories without a usable feed image a source image discovered from
 * their article page. Successful and failed lookups are cached on disk so
 * repeated /news calls stay fast.
 */
function enrichStoryImages(array $stories, string $sourceKey, bool $cacheOnly = false): array
{
    if (!$stories) {
        return $stories;
    }

    $cache = loadImageCache();
    $requests = [];
    $now = time();

    foreach ($stories as $index => &$story) {
        if (!empty($story['images'])) {
            continue;
        }
        $link = trim((string)($story['link'] ?? ''));
        if ($link === '') {
            continue;
        }

        $entry = $cache[$link] ?? null;
        if (is_array($entry)) {
            $image = (string)($entry['image'] ?? '');
            $ttl = $image !== ''
                ? IMAGE_CACHE_TTL
                : IMAGE_CACHE_FAILURE_TTL;
            if ((int)($entry['ts'] ?? 0) + $ttl > $now) {
                if ($image !== '') {
                    $story['images'] = mergeImageUrls(
                        $story['images'] ?? [],
                        [$image]
                    );
                }
                continue;
            }
        }
        if ($cacheOnly) {
            continue;
        }
        $requests[$index] = $link;
    }
    unset($story);

    if (!$requests) {
        return $stories;
    }

    $found = fetchArticleImages($requests, $sourceKey);
    foreach ($requests as $index => $link) {
        $image = (string)($found[$index] ?? '');
        if ($image !== '' && isset($stories[$index])) {
            $stories[$index]['images'] = mergeImageUrls(
                $stories[$index]['images'] ?? [],
                [$image]
            );
        }
        $cache[$link] = ['image' => $image, 'ts' => $now];
    }
    saveImageCache($cache);

    return $stories;
}

/**
 * Whether a story link points at a Google News redirect wrapper.
 */
function isGoogleNewsLink(string $url): bool
{
    return (bool)preg_match(
        '{^https?://news\.google\.com/(?:rss/)?articles/}i',
        $url
    );
}

/**
 * Extract the opaque article id from a Google News redirect link.
 */
function googleNewsArticleId(string $url): string
{
    $path = parse_url($url, PHP_URL_PATH);
    if (!is_string($path)) {
        return '';
    }
    if (preg_match('{/articles/([^/?#]+)}', $path, $match)) {
        return $match[1];
    }
    return '';
}

/**
 * Run a set of HTTP requests concurrently and return their responses keyed by
 * the caller's array index.
 *
 * @param array<int, array{url:string, method?:string, body?:string, headers?:list<string>, accept?:string}> $requests
 * @return array<int, array{body:string, status:int, url:string, error:string}>
 */
function fetchUrlsConcurrently(array $requests, string $sourceKey): array
{
    $results = [];
    foreach (array_chunk($requests, ARTICLE_FETCH_CONCURRENCY, true) as $chunk) {
        $multi = curl_multi_init();
        $handles = [];
        foreach ($chunk as $index => $request) {
            $url = (string)($request['url'] ?? '');
            $handle = curl_init($url);
            if ($handle === false) {
                $results[$index] = [
                    'body' => '',
                    'status' => 0,
                    'url' => $url,
                    'error' => 'could not initialize curl',
                ];
                continue;
            }

            $headers = [
                'Accept: ' . ($request['accept']
                    ?? 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'),
                'Accept-Language: en-US,en;q=0.9',
            ];
            $options = [
                CURLOPT_RETURNTRANSFER => true,
                CURLOPT_FOLLOWLOCATION => true,
                CURLOPT_MAXREDIRS => 5,
                CURLOPT_CONNECTTIMEOUT => 8,
                CURLOPT_TIMEOUT => 20,
                CURLOPT_USERAGENT => USER_AGENT,
                CURLOPT_ENCODING => '',
            ];
            if (($request['method'] ?? 'GET') === 'POST') {
                $options[CURLOPT_POST] = true;
                $options[CURLOPT_POSTFIELDS] = (string)($request['body'] ?? '');
            }
            foreach (($request['headers'] ?? []) as $header) {
                $headers[] = $header;
            }
            $options[CURLOPT_HTTPHEADER] = $headers;
            curl_setopt_array($handle, $options);

            curl_multi_add_handle($multi, $handle);
            $handles[$index] = $handle;
        }

        $running = null;
        do {
            $status = curl_multi_exec($multi, $running);
            if ($running > 0) {
                if (curl_multi_select($multi, 1.0) === -1) {
                    usleep(100000);
                }
            }
        } while ($running > 0 && $status === CURLM_OK);

        foreach ($handles as $index => $handle) {
            $body = curl_multi_getcontent($handle);
            $httpCode = (int)curl_getinfo($handle, CURLINFO_RESPONSE_CODE);
            $effectiveUrl = (string)curl_getinfo($handle, CURLINFO_EFFECTIVE_URL);
            $error = curl_error($handle);
            curl_multi_remove_handle($multi, $handle);
            curl_close($handle);

            $results[$index] = [
                'body' => is_string($body) ? $body : '',
                'status' => $httpCode,
                'url' => $effectiveUrl,
                'error' => $error,
            ];
        }
        curl_multi_close($multi);
    }

    return $results;
}

/**
 * Decode the publisher URL returned by Google's batchexecute endpoint.
 */
function parseGoogleNewsResponse(string $body): string
{
    if (trim($body) === '') {
        return '';
    }
    if (str_starts_with($body, ")]}'")) {
        $body = ltrim(substr($body, 4));
    }
    $decoded = json_decode($body, true);
    if (!is_array($decoded)) {
        return '';
    }
    foreach ($decoded as $entry) {
        if (!is_array($entry)) {
            continue;
        }
        if (($entry[0] ?? '') !== 'wrb.fr' || ($entry[1] ?? '') !== 'Fbv4je') {
            continue;
        }
        $inner = json_decode((string)($entry[2] ?? ''), true);
        if (
            is_array($inner)
            && ($inner[0] ?? '') === 'garturlres'
            && is_string($inner[1] ?? null)
        ) {
            return $inner[1];
        }
    }
    return '';
}

/**
 * Resolve Google News redirect links to their real publisher URLs.
 *
 * Google News RSS hands out a news.google.com/rss/articles/... wrapper; the
 * wrapper page exposes a timestamp + signature that must be exchanged through
 * Google's batchexecute endpoint to obtain the underlying article URL.
 *
 * @param array<int, string> $links
 * @return array<int, string>
 */
function resolveGoogleNewsLinks(array $links, string $sourceKey): array
{
    $resolved = [];
    $ids = [];
    foreach ($links as $index => $link) {
        $id = googleNewsArticleId($link);
        if ($id !== '') {
            $ids[$index] = $id;
        }
    }
    if (!$ids) {
        return $resolved;
    }

    $requests = [];
    foreach ($ids as $index => $id) {
        $requests[$index] = [
            'url' => 'https://news.google.com/rss/articles/' . $id,
        ];
    }
    $pages = fetchUrlsConcurrently($requests, $sourceKey);

    $posts = [];
    foreach ($ids as $index => $id) {
        $page = $pages[$index] ?? null;
        if (!is_array($page) || $page['body'] === '') {
            continue;
        }
        if (!preg_match('/data-n-a-ts="(\d+)"/', $page['body'], $timestampMatch)) {
            continue;
        }
        if (!preg_match('/data-n-a-sg="([^"]+)"/', $page['body'], $signatureMatch)) {
            continue;
        }

        $garturlreq = [
            'garturlreq',
            [
                ['X', 'X', ['X', 'X'], null, null, 1, 1, 'US:en', null, 1,
                 null, null, null, null, null, 0, 1],
                'X',
                'X',
                1,
                [1, 1, 1],
                1,
                1,
                null,
                0,
                0,
                null,
                0,
            ],
            $id,
            (int)$timestampMatch[1],
            $signatureMatch[1],
        ];
        $inner = json_encode($garturlreq, JSON_UNESCAPED_SLASHES);
        $outer = json_encode([[["Fbv4je", $inner]]], JSON_UNESCAPED_SLASHES);
        if ($inner === false || $outer === false) {
            continue;
        }
        $posts[$index] = [
            'url' => 'https://news.google.com/_/DotsSplashUi/data/batchexecute',
            'method' => 'POST',
            'body' => http_build_query(['f.req' => $outer]),
            'headers' => [
                'Content-Type: application/x-www-form-urlencoded;charset=UTF-8',
            ],
            'accept' => '*/*',
        ];
    }

    if (!$posts) {
        return $resolved;
    }

    $responses = fetchUrlsConcurrently($posts, $sourceKey);
    foreach ($responses as $index => $response) {
        $url = parseGoogleNewsResponse((string)$response['body']);
        if ($url !== '' && preg_match('{^https?://}i', $url)) {
            $resolved[$index] = $url;
        }
    }
    return $resolved;
}

/**
 * Locate the on-disk cache of fetched article bodies.
 */
function articleCacheFile(): string
{
    $home = getenv('HOME');
    if ($home === false || $home === '') {
        $home = sys_get_temp_dir();
    }
    $directory = rtrim($home, '/') . '/.cache/news_fetch';
    if (!is_dir($directory)) {
        @mkdir($directory, 0775, true);
    }
    return $directory . '/article_content.json';
}

/**
 * @return array<string, array{link:string, paragraphs:list<string>, image:string, ts:int}>
 */
function loadArticleCache(): array
{
    $file = articleCacheFile();
    if (!is_file($file)) {
        return [];
    }
    $raw = @file_get_contents($file);
    if ($raw === false || trim($raw) === '') {
        return [];
    }
    $data = json_decode($raw, true);
    return is_array($data) ? $data : [];
}

/**
 * @param array<string, array{link:string, paragraphs:list<string>, image:string, ts:int}> $cache
 */
function saveArticleCache(array $cache): void
{
    if (count($cache) > MAX_ARTICLE_CACHE_ENTRIES) {
        uasort(
            $cache,
            static fn(array $a, array $b): int =>
                (int)($b['ts'] ?? 0) <=> (int)($a['ts'] ?? 0)
        );
        $cache = array_slice($cache, 0, MAX_ARTICLE_CACHE_ENTRIES, true);
    }
    @file_put_contents(
        articleCacheFile(),
        json_encode($cache, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE),
        LOCK_EX
    );
}

/**
 * Boilerplate lines that appear on full article pages rather than in feed
 * snippets. Kept separate so feed extraction stays untouched.
 */
function isArticleBoilerplate(string $text): bool
{
    if (isBoilerplate($text)) {
        return true;
    }
    if (preg_match(
        '/^(?:skip to|sign in|log in|subscribe|advertisement|ad feedback|'
        . 'we use cookies|this site uses cookies|cookie policy|'
        . 'by continuing|terms of service|privacy policy|follow us|'
        . 'download (?:the|our) app|get the app|enable javascript|'
        . 'javascript is (?:disabled|not)|share this|related:|'
        . 'recommended|most popular|click here|watch:|image:|'
        . 'caption:|photo:|all rights reserved|©)/iu',
        $text
    )) {
        return true;
    }
    // Publishers frequently place cross-promotion links between body
    // paragraphs (for example "READ MORE: ..."). These are not the end of
    // the article; dropping them here prevents the end-of-body trimmer from
    // truncating everything that follows them.
    if (preg_match(
        '/^(?:read more|read on|see also|related(?: coverage| stories|'
        . 'articles?| content)?|more (?:from|coverage|stories|news)|'
        . 'you may also like|latest news|recommended(?: for you)?)\b/iu',
        $text
    )) {
        return true;
    }
    // Standalone photo captions ("... Getty", "... Reuters") live in <p>
    // tags on some publishers but are not article prose.
    if (mb_strlen($text) <= 240
        && preg_match(
            '/\b(?:getty images?|getty|istockphoto|shutterstock|'
            . 'associated press|ap|afp|reuters|epa|pa media)\s*$/iu',
            $text
        )
    ) {
        return true;
    }
    return false;
}

/**
 * True when an element is page chrome rather than article prose.
 *
 * Some publishers (for example the English edition of Financial News) put the
 * whole body in a container as bare text separated by <br> tags instead of
 * <p> elements. Walking those containers must skip navigation, ad slots,
 * sharing controls, related-story rails, and footers, otherwise the extracted
 * body turns into the whole page.
 */
function isArticleChromeNode(DOMNode $node): bool
{
    if (!($node instanceof DOMElement)) {
        return false;
    }

    static $skipTags = [
        'script' => true,
        'style' => true,
        'noscript' => true,
        'template' => true,
        'svg' => true,
        'iframe' => true,
        'form' => true,
        'button' => true,
        'input' => true,
        'select' => true,
        'textarea' => true,
        'figure' => true,
        'figcaption' => true,
        'video' => true,
        'audio' => true,
        'canvas' => true,
        'nav' => true,
        'footer' => true,
        'aside' => true,
    ];
    $tag = strtolower($node->tagName);
    if (isset($skipTags[$tag])) {
        return true;
    }

    $attributes = strtolower(
        $node->getAttribute('class') . ' ' . $node->getAttribute('id')
    );
    if ($attributes === ' ') {
        return false;
    }

    static $skipTokens = [
        'ad' => true,
        'ads' => true,
        'advert' => true,
        'adverts' => true,
        'advertisement' => true,
        'advertisements' => true,
        'advertising' => true,
        'adsense' => true,
        'sponsor' => true,
        'sponsored' => true,
        'sponsorship' => true,
        'share' => true,
        'shares' => true,
        'sharing' => true,
        'social' => true,
        'sns' => true,
        'related' => true,
        'recommend' => true,
        'recommended' => true,
        'recommendation' => true,
        'recommendations' => true,
        'newsletter' => true,
        'subscribe' => true,
        'subscription' => true,
        'comment' => true,
        'comments' => true,
        'breadcrumb' => true,
        'breadcrumbs' => true,
        'nav' => true,
        'navigation' => true,
        'menu' => true,
        'footer' => true,
        'promo' => true,
        'promotion' => true,
        'popup' => true,
        'modal' => true,
        'cookie' => true,
        'cookies' => true,
        'consent' => true,
        'paywall' => true,
        'sidebar' => true,
        'toolbar' => true,
    ];
    $tokens = preg_split('/[^a-z0-9]+/', $attributes, -1, PREG_SPLIT_NO_EMPTY);
    foreach ($tokens ?: [] as $token) {
        if (isset($skipTokens[$token])) {
            return true;
        }
    }

    // Common generated byline classes include the word inside a longer token
    // ("customByline"), so a plain token check is not enough.
    return str_contains($attributes, 'byline');
}

/**
 * Collect prose segments from a container whose body uses <br> line breaks
 * instead of <p> elements. Inline markup stays inside its line; block
 * elements and <br> both close the current segment.
 *
 * @return list<string>
 */
function collectLineSegmentNodes(DOMNode $root): array
{
    static $blockTags = [
        'address' => true,
        'article' => true,
        'aside' => true,
        'blockquote' => true,
        'dd' => true,
        'details' => true,
        'div' => true,
        'dl' => true,
        'dt' => true,
        'fieldset' => true,
        'figcaption' => true,
        'figure' => true,
        'footer' => true,
        'form' => true,
        'h1' => true,
        'h2' => true,
        'h3' => true,
        'h4' => true,
        'h5' => true,
        'h6' => true,
        'header' => true,
        'hr' => true,
        'li' => true,
        'main' => true,
        'nav' => true,
        'ol' => true,
        'p' => true,
        'pre' => true,
        'section' => true,
        'table' => true,
        'tbody' => true,
        'td' => true,
        'tfoot' => true,
        'th' => true,
        'thead' => true,
        'tr' => true,
        'ul' => true,
    ];

    $segments = [];
    $seen = [];
    $buffer = '';
    $flush = static function () use (&$buffer, &$segments, &$seen): void {
        $text = normalizeText($buffer);
        $buffer = '';
        if ($text === '' || mb_strlen($text) < 25) {
            return;
        }
        // Drop bare CSS/JS payloads that some sites leave in the body.
        if (preg_match('/^[.#@][\w-]+\s*\{/', $text)
            || (substr_count($text, '{') >= 2 && substr_count($text, '}') >= 2)
        ) {
            return;
        }
        if (isArticleBoilerplate($text) || isset($seen[$text])) {
            return;
        }
        $seen[$text] = true;
        $segments[] = $text;
    };

    $walk = static function (DOMNode $node) use (
        &$walk,
        &$buffer,
        &$flush,
        $blockTags
    ): void {
        if ($node instanceof DOMText) {
            $buffer .= $node->nodeValue . ' ';
            return;
        }
        if (!($node instanceof DOMElement) || isArticleChromeNode($node)) {
            return;
        }
        $tag = strtolower($node->tagName);
        if ($tag === 'br') {
            $flush();
            return;
        }
        $isBlock = isset($blockTags[$tag]);
        if ($isBlock) {
            $flush();
        }
        foreach ($node->childNodes as $child) {
            $walk($child);
        }
        if ($isBlock) {
            $flush();
        }
    };

    $walk($root);
    $flush();
    return $segments;
}

/**
 * Collect readable paragraphs from a DOMNodeList, preserving page order.
 *
 * @return list<string>
 */
function collectParagraphNodes(iterable $nodes): array
{
    $paragraphs = [];
    $seen = [];
    foreach ($nodes as $node) {
        $text = normalizeText($node->textContent);
        if ($text === '' || mb_strlen($text) < 25) {
            continue;
        }
        // Drop bare CSS/JS payloads that some sites leave inside <p> tags.
        if (preg_match('/^[.#@][\w-]+\s*\{/', $text)
            || (substr_count($text, '{') >= 2 && substr_count($text, '}') >= 2)
        ) {
            continue;
        }
        if (isArticleBoilerplate($text) || isset($seen[$text])) {
            continue;
        }
        $seen[$text] = true;
        $paragraphs[] = $text;
    }
    return $paragraphs;
}

/**
 * Extract the readable body paragraphs from a full article page.
 *
 * Prefer semantic article containers and fall back to every paragraph when
 * the page does not expose one. This covers Yahoo Finance (article-body) as
 * well as the other publishers used by /news.
 *
 * @return list<string>
 */
function extractArticleParagraphs(string $html, string $pageUrl = ''): array
{
    if (trim($html) === '') {
        return [];
    }

    $previous = libxml_use_internal_errors(true);
    $document = new DOMDocument();
    $loaded = $document->loadHTML(
        '<?xml encoding="utf-8"?>' . $html,
        LIBXML_NONET | LIBXML_NOERROR | LIBXML_NOWARNING
    );
    libxml_clear_errors();
    libxml_use_internal_errors($previous);
    if (!$loaded) {
        return [];
    }

    $xpath = new DOMXPath($document);
    $queries = [
        '//*[@itemprop="articleBody"]//p',
        '//*[@data-testid="article-body"]//p',
        // NPR's text-only article pages wrap the prose in a paragraphs-container
        // div; selecting it avoids the headline, byline, and timestamp <p> tags
        // that also live under <article>.
        '//*[contains(concat(" ", normalize-space(@class), " "), " paragraphs-container ")]//p',
        '//article//p',
        // CBC News renders article prose with an "html-paragraph" class but
        // no semantic article container, so select those paragraphs directly.
        '//p[contains(concat(" ", normalize-space(@class), " "), " html-paragraph ")]',
        '//*[contains(concat(" ", normalize-space(@class), " "), " article-body ")]//p',
        '//*[contains(concat(" ", normalize-space(@class), " "), " article-content ")]//p',
        '//*[contains(concat(" ", normalize-space(@class), " "), " entry-content ")]//p',
        '//*[contains(concat(" ", normalize-space(@class), " "), " post-content ")]//p',
        '//main//p',
    ];

    $candidates = [];
    foreach ($queries as $query) {
        $nodes = $xpath->query($query);
        if ($nodes === false) {
            continue;
        }
        $candidate = collectParagraphNodes($nodes);
        if ($candidate) {
            $candidates[] = $candidate;
        }
    }

    // Some publishers use bare text separated by <br> instead of <p> tags
    // (for example Financial News English). Walk the semantic body containers
    // directly so those pages still expose their complete article text.
    $segmentQueries = [
        '//*[@itemprop="articleBody"]',
        '//*[@data-testid="article-body"]',
        '//*[contains(concat(" ", normalize-space(@class), " "), " paragraphs-container ")]',
        '//article',
        '//*[contains(concat(" ", normalize-space(@class), " "), " article-body ")]',
        '//*[contains(concat(" ", normalize-space(@class), " "), " article-content ")]',
        '//*[contains(concat(" ", normalize-space(@class), " "), " entry-content ")]',
        '//*[contains(concat(" ", normalize-space(@class), " "), " post-content ")]',
        '//*[contains(concat(" ", normalize-space(@class), " "), " view_cont ")]',
        '//main',
    ];
    foreach ($segmentQueries as $query) {
        $nodes = $xpath->query($query);
        if ($nodes === false) {
            continue;
        }
        foreach ($nodes as $node) {
            $candidate = collectLineSegmentNodes($node);
            if ($candidate) {
                $candidates[] = $candidate;
            }
        }
    }

    $nodes = $xpath->query('//p');
    if ($nodes !== false) {
        $candidate = collectParagraphNodes($nodes);
        if ($candidate) {
            $candidates[] = $candidate;
        }
    }

    // Pick the candidate that carries the most prose. Some sites put only the
    // byline inside <article> and render the article body in a sibling
    // container, so stopping at the first container with two paragraphs can
    // return just author metadata. Semantic containers still win when they
    // hold the body on their own or account for most of the best candidate.
    $best = [];
    $bestLength = 0;
    foreach ($candidates as $candidate) {
        $length = 0;
        foreach ($candidate as $paragraph) {
            $length += mb_strlen($paragraph);
        }
        if ($length > $bestLength) {
            $best = $candidate;
            $bestLength = $length;
        }
    }
    if (!$best) {
        return [];
    }

    $coverageFloor = (int)ceil($bestLength * 0.6);
    foreach ($candidates as $candidate) {
        if (count($candidate) < 2) {
            continue;
        }
        $length = 0;
        foreach ($candidate as $paragraph) {
            $length += mb_strlen($paragraph);
        }
        if ($length >= ARTICLE_MIN_BODY_CHARS || $length >= $coverageFloor) {
            return $candidate;
        }
    }

    return $best;
}

/**
 * Return the public feeds that may carry full story bodies for a publisher.
 *
 * @return list<string>
 */
function articleFeedFallbackUrls(string $url): array
{
    $host = strtolower((string)parse_url($url, PHP_URL_HOST));
    if ($host === '') {
        return [];
    }
    foreach (ARTICLE_FEED_FALLBACKS as $domain => $feedUrls) {
        if ($host === $domain || str_ends_with($host, '.' . $domain)) {
            $feeds = is_array($feedUrls) ? $feedUrls : [$feedUrls];
            return array_values(array_filter(array_map('strval', $feeds)));
        }
    }
    return [];
}

/**
 * Normalise an article URL for matching a page link against a feed <link>.
 * Query strings and fragments are ignored because feeds use canonical paths.
 */
function articleUrlKey(string $url): string
{
    $parts = parse_url(trim($url));
    if (!is_array($parts) || empty($parts['host'])) {
        return '';
    }
    $path = rtrim((string)($parts['path'] ?? '/'), '/');
    return strtolower($parts['host']) . ($path === '' ? '/' : $path);
}

/**
 * Pick the richest body field from a feed item (content:encoded preferred).
 */
function feedArticleBodyHtml(DOMElement $item): string
{
    $best = '';
    foreach ($item->childNodes as $child) {
        if (!($child instanceof DOMElement)) {
            continue;
        }
        $name = strtolower($child->localName);
        if ($name !== 'encoded' && $name !== 'description') {
            continue;
        }
        $text = (string)$child->textContent;
        if (mb_strlen($text) > mb_strlen($best)) {
            $best = $text;
        }
    }
    return $best;
}

/**
 * Extract story prose from the HTML embedded in a feed item. Paragraphs,
 * list items, and section headings are kept in document order.
 *
 * @return list<string>
 */
function extractFeedArticleParagraphs(string $html): array
{
    if (trim($html) === '') {
        return [];
    }

    $previous = libxml_use_internal_errors(true);
    $document = new DOMDocument();
    $loaded = $document->loadHTML(
        '<?xml encoding="utf-8"?><div id="feed-article">' . $html . '</div>',
        LIBXML_NONET | LIBXML_NOERROR | LIBXML_NOWARNING
    );
    libxml_clear_errors();
    libxml_use_internal_errors($previous);
    if (!$loaded) {
        return [];
    }

    $xpath = new DOMXPath($document);
    $nodes = $xpath->query(
        '//*[@id="feed-article"]//p'
        . ' | //*[@id="feed-article"]//li'
        . ' | //*[@id="feed-article"]//h2'
        . ' | //*[@id="feed-article"]//h3'
        . ' | //*[@id="feed-article"]//h4'
    );
    if ($nodes === false) {
        return [];
    }

    $paragraphs = [];
    $seen = [];
    foreach ($nodes as $node) {
        $text = normalizeText($node->textContent);
        if ($text === '' || mb_strlen($text) < 12) {
            continue;
        }
        if (preg_match('/^[.#@][\w-]+\s*\{/', $text)
            || (substr_count($text, '{') >= 2 && substr_count($text, '}') >= 2)
        ) {
            continue;
        }
        if (isArticleBoilerplate($text) || isset($seen[$text])) {
            continue;
        }
        $seen[$text] = true;
        $paragraphs[] = $text;
    }
    // Some feeds expose a plain-text description with no <p>/<li> markup.
    if (!$paragraphs) {
        $text = normalizeText(strip_tags($html));
        if ($text !== ''
            && mb_strlen($text) >= 25
            && !isArticleBoilerplate($text)
        ) {
            $paragraphs[] = $text;
        }
    }
    return $paragraphs;
}

/**
 * Pick the lead image advertised by a feed item.
 */
function feedArticleImage(DOMElement $item, string $baseUrl): string
{
    foreach ($item->childNodes as $child) {
        if (!($child instanceof DOMElement)) {
            continue;
        }
        $name = strtolower($child->localName);
        if ($name !== 'content' && $name !== 'thumbnail') {
            continue;
        }
        $url = normalizeImageUrl($child->getAttribute('url'), $baseUrl);
        if ($url !== '' && looksLikeImageUrl($url)) {
            return $url;
        }
    }
    return '';
}

/**
 * Return public reader-mirror URLs that render the full story for a blocked
 * publisher page.
 *
 * @return list<string>
 */
function articleReaderFallbackUrls(string $url): array
{
    $host = strtolower((string)parse_url($url, PHP_URL_HOST));
    if ($host === '') {
        return [];
    }
    foreach (ARTICLE_READER_FALLBACKS as $domain => $templates) {
        if ($host === $domain || str_ends_with($host, '.' . $domain)) {
            $urls = [];
            foreach ($templates as $template) {
                $urls[] = str_replace('{url}', $url, $template);
            }
            return $urls;
        }
    }
    return [];
}

/**
 * Recover a blocked article through a public reader mirror.
 *
 * @return array{paragraphs:list<string>, image:string}|null
 */
function fetchArticleReaderFallback(string $url, string $sourceKey): ?array
{
    // Profile/landing pages have no story body for the mirror to render.
    if (isNonArticleUrl($url)) {
        return null;
    }

    // A mirror can be briefly rate-limited or return an interstitial page.
    // Try every mirror URL twice, and also retry with tracking parameters
    // stripped, because Medium links copied from feeds often carry an
    // ?source=rss-... suffix that the mirror does not index.
    $targets = articleReaderFallbackUrls($url);
    $cleanUrl = articleUrlWithoutTracking($url);
    if ($cleanUrl !== '' && $cleanUrl !== $url) {
        $targets = array_merge(
            $targets,
            articleReaderFallbackUrls($cleanUrl)
        );
    }
    $targets = array_values(array_unique($targets));

    foreach ($targets as $readerUrl) {
        for ($attempt = 0; $attempt < 2; $attempt++) {
            $body = fetchUrl(
                $readerUrl,
                'text/html, application/xhtml+xml;q=0.9, */*;q=0.8',
                $sourceKey
            );
            if (!is_string($body) || trim($body) === '') {
                if ($attempt === 0) {
                    usleep(300000);
                }
                continue;
            }
            // Resolve relative image URLs against the mirror, which proxies
            // the story's images, rather than the blocked publisher page.
            $paragraphs = trimArticleChrome(
                extractArticleParagraphs($body, $readerUrl)
            );
            if ($paragraphs && !articleBodyIsPlaceholder($paragraphs)) {
                return [
                    'paragraphs' => $paragraphs,
                    'image' => extractArticleImageFromHtml($body, $readerUrl),
                ];
            }
            if ($attempt === 0) {
                usleep(300000);
            }
        }
    }

    return null;
}

/**
 * Total readable prose length of a paragraph list.
 *
 * @param list<string> $paragraphs
 */
function articleParagraphsLength(array $paragraphs): int
{
    $length = 0;
    foreach ($paragraphs as $paragraph) {
        $length += mb_strlen(trim((string)$paragraph));
    }
    return $length;
}

/**
 * A short feed/mirror body is usually a publisher preview rather than the
 * complete story, even when it does not contain an explicit "continue
 * reading" marker.
 *
 * @param list<string> $paragraphs
 */
function articleFallbackIsPreview(array $paragraphs): bool
{
    if (articleParagraphsLength($paragraphs) < ARTICLE_PARTIAL_BODY_CHARS) {
        return true;
    }
    return feedBodyIsPreview($paragraphs);
}

/**
 * Detect bot-challenge / paywall pages that a reader proxy returned as HTTP
 * 200. These must not be cached as the article body.
 *
 * @param list<string> $paragraphs
 */
function articleBodyLooksBlocked(array $paragraphs): bool
{
    if (!$paragraphs) {
        return false;
    }
    $text = strtolower(implode(' ', array_slice($paragraphs, 0, 4)));
    if ($text === '') {
        return false;
    }
    $needles = [
        'just a moment',
        'security verification',
        'access to this page has been denied',
        'please enable js and disable any ad blocker',
        'enable javascript and cookies',
        'verify you are human',
        'px-captcha',
        'attention required',
        'you have been blocked',
        'the action you just performed triggered the security solution',
        'this page is not available',
        'request id',
        'error code cg',
        'accessibility help',
        'skip to navigation',
        'skip to main content',
        'open side navigation menu',
        'barrier-page',
        'subscription required',
        'already a subscriber',
        'sign in to continue',
        'create a free account to continue',
        'register to continue reading',
        'this content is unavailable',
        'get unlimited access',
        'to continue reading',
        'already a subscriber',
        'subscribers only',
        'premium content',
        'register to continue',
        'sign in to continue',
        'keep reading for',
        'complete digital access',
        'cancel anytime during your trial',
        'what’s included',
        "what's included",
        'first ft:',
        'follow topics & set alerts',
        'ft journalism on any device',
    ];
    foreach ($needles as $needle) {
        if (str_contains($text, $needle)) {
            return true;
        }
    }
    // Footer/legal pages are sometimes served instead of the story. Only
    // treat them as blocked when there is almost no prose around them.
    if (articleParagraphsLength($paragraphs) < ARTICLE_PARTIAL_BODY_CHARS) {
        foreach ([
            'all rights reserved',
            'select market data',
            'provided by ice data',
            'terms of service',
            'privacy policy',
            'subscribe to continue',
            'sign in to continue',
        ] as $needle) {
            if (str_contains($text, $needle)) {
                return true;
            }
        }
    }
    return false;
}

/**
 * Significant words in a headline, used to match syndicated copies.
 *
 * @return list<string>
 */
function articleTitleWords(string $title): array
{
    $title = html_entity_decode(strip_tags($title), ENT_QUOTES | ENT_HTML5, 'UTF-8');
    $title = strtolower($title);
    $title = preg_replace('/[^a-z0-9]+/', ' ', $title) ?? '';
    $stop = array_flip([
        'the', 'a', 'an', 'and', 'or', 'for', 'to', 'of', 'in', 'on',
        'with', 'as', 'at', 'by', 'from', 'is', 'are', 'was', 'were',
        'be', 'been', 'being', 'it', 'its', 'this', 'that', 'these',
        'those', 'his', 'her', 'their', 'he', 'she', 'they', 'we', 'you',
        'i', 'not', 'no', 'new', 'says', 'said', 'after', 'before', 'over',
        'under', 'about', 'into', 'than', 'then', 'also', 'will', 'would',
        'can', 'could', 'may', 'might', 'has', 'have', 'had', 'do', 'does',
        'did', 'but', 'if', 'so', 'up', 'out', 'more', 'most', 'least',
        'one', 'two', 'three', 'first', 'last', 'year', 'years', 'day',
        'days', 'update', 'updated', 'report', 'reports', 'reportedly',
    ]);
    $words = [];
    foreach (preg_split('/\s+/', trim($title)) ?: [] as $word) {
        if (strlen($word) < 3 || isset($stop[$word]) || ctype_digit($word)) {
            continue;
        }
        $words[$word] = true;
    }
    return array_keys($words);
}

/**
 * Similarity between an original headline and a possible syndicated copy.
 */
function articleTitleSimilarity(string $original, string $candidate): float
{
    $a = articleTitleWords($original);
    $b = articleTitleWords($candidate);
    if (!$a || !$b) {
        return 0.0;
    }
    $common = count(array_intersect($a, $b));
    if ($common === 0) {
        return 0.0;
    }
    $containment = $common / max(1, min(count($a), count($b)));
    $union = count(array_unique(array_merge($a, $b)));
    $jaccard = $common / max(1, $union);
    return max($containment, $jaccard);
}

/**
 * Convert a Jina Reader markdown response (or an HTML response) into
 * article paragraphs.
 *
 * @return list<string>
 */
function articleMarkdownParagraphs(string $body, string $pageUrl = ''): array
{
    if (trim($body) === '') {
        return [];
    }
    if (str_starts_with(ltrim($body), '{')) {
        $error = json_decode($body, true);
        if (is_array($error) && isset($error['code'])) {
            return [];
        }
    }
    if (preg_match('/<(?:p|article|main|div)\b/i', $body)) {
        $paragraphs = extractArticleParagraphs($body, $pageUrl);
        if ($paragraphs) {
            return $paragraphs;
        }
    }

    $marker = strpos($body, 'Markdown Content:');
    if ($marker !== false) {
        $body = substr($body, $marker + strlen('Markdown Content:'));
    }

    $paragraphs = [];
    $seen = [];
    foreach (preg_split('/\R/u', $body) ?: [] as $line) {
        $line = trim($line);
        if ($line === '') {
            continue;
        }
        if (preg_match('/^(?:Title|URL Source|Published Time|Warning|Markdown Content):/i', $line)) {
            continue;
        }
        if (preg_match('/^#{1,6}\s/', $line)) {
            continue;
        }
        if (preg_match('/^!\[[^\]]*\]\([^)]+\)\s*$/', $line)
            || preg_match('/^\[[^\]]*\]\([^)]+\)\s*$/', $line)
        ) {
            continue;
        }
        $line = preg_replace('/!\[[^\]]*\]\([^)]*\)/u', '', $line) ?? $line;
        $line = preg_replace('/\[([^\]]*)\]\([^)]+\)/u', '$1', $line) ?? $line;
        $line = strip_tags($line);
        $line = preg_replace('/[*_`~]+/u', '', $line) ?? $line;
        $line = preg_replace('/^\s*(?:[-*+]|\d+[.)])\s+/u', '', $line) ?? $line;
        $line = normalizeText($line);
        if ($line === '' || mb_strlen($line) < 30) {
            continue;
        }
        if (preg_match('/^(?:share|advertisement|sign up|subscribe|follow us|cookie|privacy policy|terms of|all rights reserved|read more|continue reading)/i', $line)) {
            continue;
        }
        if (isset($seen[$line])) {
            continue;
        }
        $seen[$line] = true;
        $paragraphs[] = $line;
    }
    return trimArticleChrome($paragraphs);
}

/**
 * Pick the lead image from a markdown reader response.
 */
function extractMarkdownImage(string $body, string $baseUrl = ''): string
{
    if (preg_match_all('/!\[[^\]]*\]\((https?:\/\/[^)\s]+)[^)]*\)/i', $body, $matches)) {
        foreach ($matches[1] as $url) {
            $url = normalizeImageUrl($url, $baseUrl);
            if ($url !== '' && looksLikeImageUrl($url)) {
                return $url;
            }
        }
    }
    return '';
}

/**
 * Remove navigation, ad rails, related-link lists, and legal footers that
 * broad paragraph extraction picks up on syndication/news aggregation pages.
 *
 * @param list<string> $paragraphs
 * @return list<string>
 */
function trimArticleChrome(array $paragraphs): array
{
    $count = count($paragraphs);
    if ($count < 4) {
        return $paragraphs;
    }

    $start = 0;
    foreach ($paragraphs as $index => $paragraph) {
        $text = trim((string)$paragraph);
        if (mb_strlen($text) >= 80 && preg_match('/[.!?][\"\']?$/u', $text)) {
            $start = $index;
            break;
        }
    }
    if ($start > 0) {
        $previous = trim((string)$paragraphs[$start - 1]);
        if (mb_strlen($previous) > 0
            && mb_strlen($previous) <= 100
            && !preg_match('/^(?:price|brokers?|best |forex|tu score|#|advertisement)/i', $previous)
        ) {
            $start--;
        }
    }

    $end = $count;
    for ($index = $start; $index < $count; $index++) {
        $text = trim((string)$paragraphs[$index]);
        if (preg_match(
            '/^(?:this material may contain|risk disclosure|disclaimer|all rights reserved|©|copyright|accept all cookies|by clicking|when you visit|strictly necessary cookies|our experts ranked|tu score|registration number|follow us|share this|related stories|more from|advertisement|by entering your email|sign up for our newsletter|this article originally appeared in|originally appeared in|this article was originally published|read more|related coverage|more coverage|you may also like|more stories|latest news)/iu',
            $text
        ) || preg_match('/\ball rights reserved\b/iu', $text)) {
            $end = $index;
            break;
        }
    }

    $trimmed = array_values(array_slice($paragraphs, $start, $end - $start));
    if (articleParagraphsLength($trimmed) < 300) {
        return $paragraphs;
    }
    return $trimmed;
}

/**
 * Turn a fetched HTML article page into the fallback result shape.
 *
 * @return array{paragraphs:list<string>, image:string}|null
 */
function articleResultFromBody(string $body, string $baseUrl): ?array
{
    $paragraphs = trimArticleChrome(extractArticleParagraphs($body, $baseUrl));
    if (!$paragraphs
        || articleBodyLooksBlocked($paragraphs)
        || articleBodyIsPlaceholder($paragraphs)
    ) {
        return null;
    }
    return [
        'paragraphs' => $paragraphs,
        'image' => extractArticleImageFromHtml($body, $baseUrl),
    ];
}

/**
 * Retry a blocked article page with alternate mainstream browser identities.
 *
 * WAFs sometimes fingerprint a single user agent and answer it with a bot
 * challenge while returning the unchanged article to another browser. Only
 * the direct publisher page is retried; the normal feed/reader/archive
 * fallbacks still run if every identity is blocked.
 *
 * @return array{paragraphs:list<string>, image:string}|null
 */
function fetchArticleWithAlternateBrowsers(
    string $url,
    string $sourceKey
): ?array {
    foreach (ARTICLE_FALLBACK_USER_AGENTS as $userAgent) {
        $body = fetchUrl(
            $url,
            'text/html, application/xhtml+xml;q=0.9, */*;q=0.8',
            $sourceKey,
            MAX_ATTEMPTS - 1,
            true,
            20,
            $userAgent
        );
        if (!is_string($body) || trim($body) === '') {
            continue;
        }
        $result = articleResultFromBody($body, $url);
        if ($result !== null) {
            return $result;
        }
    }
    return null;
}

/**
 * Generic public readers that render a blocked publisher page from a
 * different network. Google Translate's proxy is especially useful for AP
 * and other sites that block plain bot requests but allow the translate
 * crawler; Jina Reader covers sites whose rendered content survives its
 * extraction.
 *
 * @return list<array{url:string, kind:string, label:string}>
 */
function articleProxyFallbackUrls(string $url): array
{
    $parts = parse_url(trim($url));
    if (!is_array($parts) || empty($parts['host'])) {
        return [];
    }
    $host = strtolower((string)$parts['host']);
    $path = (string)($parts['path'] ?? '/');
    if ($path === '') {
        $path = '/';
    }
    $query = isset($parts['query']) && $parts['query'] !== ''
        ? '&' . $parts['query']
        : '';
    $translateHost = str_replace('.', '-', str_replace('-', '--', $host));
    $translateUrl = 'https://' . $translateHost . '.translate.goog' . $path
        . '?_x_tr_sl=auto&_x_tr_tl=en&_x_tr_hl=en&_x_tr_pto=wapp'
        . $query;

    return [
        ['url' => $translateUrl, 'kind' => 'html', 'label' => 'google_translate'],
        ['url' => 'https://r.jina.ai/' . $url, 'kind' => 'markdown', 'label' => 'jina_reader'],
    ];
}

/**
 * Recover a blocked article through a generic public reader proxy.
 *
 * @return array{paragraphs:list<string>, image:string}|null
 */
function fetchArticleProxyFallback(string $url, string $sourceKey): ?array
{
    foreach (articleProxyFallbackUrls($url) as $proxy) {
        $accept = $proxy['kind'] === 'markdown'
            ? 'text/plain, text/markdown;q=0.9, text/html;q=0.8, */*;q=0.7'
            : 'text/html, application/xhtml+xml;q=0.9, */*;q=0.8';
        $body = fetchUrl(
            $proxy['url'],
            $accept,
            $sourceKey,
            MAX_ATTEMPTS - 1,
            true,
            10
        );
        if (!is_string($body) || trim($body) === '') {
            continue;
        }

        if ($proxy['kind'] === 'markdown') {
            $paragraphs = articleMarkdownParagraphs($body, $url);
            $image = extractMarkdownImage($body, $url);
        } else {
            $paragraphs = trimArticleChrome(
                extractArticleParagraphs($body, $url)
            );
            $image = extractArticleImageFromHtml($body, $url);
        }
        if ($paragraphs
            && !articleBodyLooksBlocked($paragraphs)
            && !articleBodyIsPlaceholder($paragraphs)
        ) {
            return [
                'paragraphs' => $paragraphs,
                'image' => $image,
            ];
        }
    }
    return null;
}

/**
 * Recover a story from a public WordPress REST endpoint. Many news sites
 * (NewsNation among them) expose the full post body at /wp-json/wp/v2/posts
 * even when the rendered page is guarded by a bot challenge.
 *
 * @return array{paragraphs:list<string>, image:string}|null
 */
function fetchArticleWordPressFallback(string $url, string $sourceKey): ?array
{
    $parts = parse_url(trim($url));
    if (!is_array($parts) || empty($parts['host'])) {
        return null;
    }
    $scheme = strtolower((string)($parts['scheme'] ?? 'https')) === 'http'
        ? 'http'
        : 'https';
    $host = strtolower((string)$parts['host']);
    $path = (string)($parts['path'] ?? '/');
    $slug = basename(rtrim(rawurldecode($path), '/'));
    $slug = preg_replace('/\.(?:html?|php|aspx?)$/i', '', $slug) ?? $slug;
    if ($slug === ''
        || in_array(strtolower($slug), ['index', 'article', 'news'], true)
    ) {
        return null;
    }

    $apiUrl = $scheme . '://' . $host
        . '/wp-json/wp/v2/posts?slug=' . rawurlencode($slug)
        . '&_fields=link,title,content,jetpack_featured_media_url,featured_media';
    $body = fetchUrl(
        $apiUrl,
        'application/json, text/plain;q=0.9, */*;q=0.8',
        $sourceKey,
        MAX_ATTEMPTS - 1,
        true,
        10
    );
    if (!is_string($body) || trim($body) === '' || !str_starts_with(ltrim($body), '[')) {
        return null;
    }
    $posts = json_decode($body, true);
    if (!is_array($posts)) {
        return null;
    }

    foreach ($posts as $post) {
        if (!is_array($post)) {
            continue;
        }
        $content = is_array($post['content'] ?? null)
            ? (string)($post['content']['rendered'] ?? '')
            : '';
        if (trim($content) === '') {
            continue;
        }
        $link = trim((string)($post['link'] ?? $url));
        $paragraphs = trimArticleChrome(
            extractArticleParagraphs($content, $link)
        );
        if (!$paragraphs
            || articleBodyLooksBlocked($paragraphs)
            || articleBodyIsPlaceholder($paragraphs)
        ) {
            continue;
        }
        $image = trim((string)($post['jetpack_featured_media_url'] ?? ''));
        if ($image === '') {
            $image = extractArticleImageFromHtml($content, $link);
        }
        return [
            'paragraphs' => $paragraphs,
            'image' => $image,
        ];
    }
    return null;
}

/**
 * Fetch one possible syndicated copy, trying the direct page and then the
 * generic recovery paths. Used only after the original publisher failed.
 *
 * @return array{paragraphs:list<string>, image:string}|null
 */
function fetchArticleCopy(string $url, string $sourceKey): ?array
{
    // Syndicated copies are usually directly accessible. Keep this path fast:
    // if a copy is blocked, try the next matching result instead of spending
    // minutes on the full recovery chain for every candidate.
    $page = fetchUrl(
        $url,
        'text/html, application/xhtml+xml;q=0.9, */*;q=0.8',
        $sourceKey,
        MAX_ATTEMPTS - 1,
        true,
        10
    );
    if (is_string($page) && trim($page) !== '') {
        return articleResultFromBody($page, $url);
    }
    return null;
}

/**
 * Recover a blocked story from a syndicated copy of the same report. Reuters,
 * FT, and NYT articles are often republished in full by partners while the
 * original page remains behind Cloudflare/Datadome or a paywall.
 *
 * @return array{paragraphs:list<string>, image:string}|null
 */
function fetchArticleSyndicationFallback(
    string $url,
    string $title,
    string $sourceKey
): ?array {
    $title = normalizeText(strip_tags($title));
    if (mb_strlen($title) < 15) {
        $parts = parse_url(trim($url));
        $slug = is_array($parts)
            ? basename(rtrim((string)($parts['path'] ?? '/'), '/'))
            : '';
        $title = normalizeText(str_replace(['-', '_'], ' ', $slug));
    }
    $words = articleTitleWords($title);
    if (count($words) < 3) {
        return null;
    }

    $query = implode(' ', array_slice($words, 0, 14));
    $feedUrl = 'https://news.google.com/rss/search?q=' . rawurlencode($query)
        . '&hl=en-US&gl=US&ceid=US:en';
    $feed = fetchUrl(
        $feedUrl,
        'application/rss+xml, application/xml;q=0.9, */*;q=0.8',
        $sourceKey,
        0,
        true
    );
    if (!is_string($feed) || trim($feed) === '') {
        return null;
    }

    $previous = libxml_use_internal_errors(true);
    $document = new DOMDocument();
    $loaded = $document->loadXML($feed, LIBXML_NONET | LIBXML_NOERROR | LIBXML_NOWARNING);
    libxml_clear_errors();
    libxml_use_internal_errors($previous);
    if (!$loaded) {
        return null;
    }

    $xpath = new DOMXPath($document);
    $items = $xpath->query('//item');
    if ($items === false) {
        return null;
    }

    $originalKey = articleUrlKey($url);
    $originalHost = strtolower((string)parse_url($url, PHP_URL_HOST));
    $candidates = [];
    foreach ($items as $item) {
        $candidateTitle = normalizeText((string)$item->getElementsByTagName('title')->item(0)?->textContent);
        $score = articleTitleSimilarity($title, $candidateTitle);
        if ($score < 0.50) {
            continue;
        }
        $link = trim((string)$item->getElementsByTagName('link')->item(0)?->textContent);
        if ($link === '' || articleUrlKey($link) === $originalKey) {
            continue;
        }
        $candidates[] = [
            'score' => $score,
            'link' => $link,
            'title' => $candidateTitle,
        ];
    }
    if (!$candidates) {
        return null;
    }
    usort(
        $candidates,
        static fn(array $a, array $b): int => $b['score'] <=> $a['score']
    );
    $candidates = array_slice($candidates, 0, 5);

    $links = [];
    foreach ($candidates as $index => $candidate) {
        $links[$index] = $candidate['link'];
    }
    $resolved = resolveGoogleNewsLinks($links, $sourceKey);
    $bestPartial = null;
    $deadline = microtime(true) + 35.0;

    // First pass: direct pages. This is fast and finds most syndicated copies.
    foreach ($candidates as $index => $candidate) {
        if (microtime(true) > $deadline) {
            break;
        }
        $candidateUrl = $resolved[$index] ?? '';
        if ($candidateUrl === '' || isGoogleNewsLink($candidateUrl)) {
            continue;
        }
        if (articleUrlKey($candidateUrl) === $originalKey) {
            continue;
        }
        $candidateHost = strtolower((string)parse_url($candidateUrl, PHP_URL_HOST));
        if ($candidateHost === $originalHost && $originalHost !== '') {
            continue;
        }
        $result = fetchArticleCopy($candidateUrl, $sourceKey);
        if ($result === null) {
            continue;
        }
        if (!articleFallbackIsPreview($result['paragraphs'])) {
            return $result;
        }
        $current = $bestPartial['paragraphs'] ?? [];
        if ($bestPartial === null
            || articleParagraphsLength($result['paragraphs'])
                > articleParagraphsLength($current)
        ) {
            $bestPartial = $result;
        }
    }

    // Second pass: the copy may itself be blocked to a plain request but still
    // render through a reader proxy (Traders Union, for example). Only run this
    // when the faster direct pass did not find a complete body.
    $deadline = microtime(true) + 40.0;
    foreach ($candidates as $index => $candidate) {
        if (microtime(true) > $deadline) {
            break;
        }
        $candidateUrl = $resolved[$index] ?? '';
        if ($candidateUrl === '' || isGoogleNewsLink($candidateUrl)) {
            continue;
        }
        if (articleUrlKey($candidateUrl) === $originalKey) {
            continue;
        }
        $candidateHost = strtolower((string)parse_url($candidateUrl, PHP_URL_HOST));
        if ($candidateHost === $originalHost && $originalHost !== '') {
            continue;
        }
        $result = fetchArticleProxyFallback($candidateUrl, $sourceKey);
        if ($result === null) {
            continue;
        }
        if (!articleFallbackIsPreview($result['paragraphs'])) {
            return $result;
        }
        $current = $bestPartial['paragraphs'] ?? [];
        if ($bestPartial === null
            || articleParagraphsLength($result['paragraphs'])
                > articleParagraphsLength($current)
        ) {
            $bestPartial = $result;
        }
    }

    return $bestPartial;
}

/**
 * Keep the longest fallback body while later recovery strategies run. A feed
 * preview or short mirror result is used only when no complete body can be
 * recovered, so blocked stories still open with usable text and their image
 * instead of showing a hard error.
 */
function considerArticleFallback(
    array &$entry,
    ?array $fallback,
    ?array &$partial
): void {
    if ($fallback === null) {
        return;
    }
    $image = trim((string)($fallback['image'] ?? ''));
    if ($entry['image'] === '' && $image !== '') {
        $entry['image'] = $image;
    }
    $paragraphs = [];
    foreach (($fallback['paragraphs'] ?? []) as $paragraph) {
        $paragraph = trim((string)$paragraph);
        if ($paragraph !== '') {
            $paragraphs[] = $paragraph;
        }
    }
    if (!$paragraphs) {
        return;
    }
    if (articleFallbackIsPreview($paragraphs)) {
        $current = $partial['paragraphs'] ?? [];
        if ($partial === null
            || articleParagraphsLength($paragraphs) > articleParagraphsLength($current)
        ) {
            $partial = [
                'paragraphs' => $paragraphs,
                'image' => $image,
            ];
        }
        return;
    }
    $entry['paragraphs'] = $paragraphs;
}

/**
 * Return an article URL without query/fragment noise, or an empty string when
 * the URL cannot be normalized.
 */
function articleUrlWithoutTracking(string $url): string
{
    $parts = parse_url(trim($url));
    if (!is_array($parts) || empty($parts['host'])) {
        return '';
    }
    $scheme = strtolower((string)($parts['scheme'] ?? 'https'));
    if ($scheme !== 'http' && $scheme !== 'https') {
        $scheme = 'https';
    }
    $host = strtolower((string)$parts['host']);
    $port = isset($parts['port']) ? ':' . (int)$parts['port'] : '';
    $path = (string)($parts['path'] ?? '/');
    if ($path === '') {
        $path = '/';
    }
    return $scheme . '://' . $host . $port . $path;
}

/**
 * Find a Wayback Machine snapshot for a URL.
 *
 * Some publishers (Cloudflare-protected sites, for example) reject both the
 * live page and generic reader mirrors but have a public archived copy. The
 * availability API returns the closest snapshot; the "id_" form of the URL
 * serves the original page without the archive toolbar so article extraction
 * sees only the publisher's markup.
 */
function articleArchiveSnapshotUrl(string $url, string $sourceKey): string
{
    $cleanUrl = articleUrlWithoutTracking($url);
    if ($cleanUrl === '') {
        return '';
    }

    $apiUrl = 'https://archive.org/wayback/available?url=' . rawurlencode($cleanUrl);
    $body = fetchUrl(
        $apiUrl,
        'application/json, text/plain;q=0.9, */*;q=0.8',
        $sourceKey
    );
    if (!is_string($body) || trim($body) === '') {
        return '';
    }

    $payload = json_decode($body, true);
    $snapshot = $payload['archived_snapshots']['closest'] ?? null;
    if (!is_array($snapshot) || empty($snapshot['available'])) {
        return '';
    }
    $snapshotUrl = trim((string)($snapshot['url'] ?? ''));
    if ($snapshotUrl === '') {
        return '';
    }

    // Prefer the raw archived document (no injected banner/toolbar).
    $rawUrl = preg_replace(
        '#^(https?://web\.archive\.org/web/\d+[a-z_]*?)/#i',
        '$1id_/',
        $snapshotUrl,
        1
    );
    return is_string($rawUrl) && $rawUrl !== '' ? $rawUrl : $snapshotUrl;
}

/**
 * Split a Wayback Machine URL into a raw snapshot URL and the original URL.
 *
 * A feed can point directly at an archived copy (for example
 * http://web.archive.org/web/20260211100204id_/https://example.com/story).
 * Such a URL must be fetched as-is; asking the availability API about the
 * archive URL itself either returns nothing or nests one archive page inside
 * another, so a transient fetch failure never recovers. Parsing the Wayback
 * shape lets us retry the snapshot and, if needed, the embedded original URL.
 *
 * @return array{snapshot:string, original:string}|null
 */
function parseWaybackUrl(string $url): ?array
{
    $parts = parse_url(trim($url));
    if (!is_array($parts)) {
        return null;
    }
    $host = strtolower((string)($parts['host'] ?? ''));
    if ($host !== 'web.archive.org') {
        return null;
    }

    $path = (string)($parts['path'] ?? '');
    if (!preg_match('#^/web/(\d{4,14})[a-z_]*/(.+)$#i', $path, $match)) {
        return null;
    }
    $timestamp = $match[1];
    $original = $match[2];
    // Some archived links use /http:/example.com or /http:///example.com.
    $original = preg_replace('#^(https?):/*#i', '$1://', $original);
    if (!is_string($original) || !preg_match('#^https?://#i', $original)) {
        return null;
    }

    // parse_url() treats a query after the embedded URL as the archive URL's
    // own query, so put it back on the original before rebuilding the link.
    $query = isset($parts['query']) ? '?' . $parts['query'] : '';
    $scheme = strtolower((string)($parts['scheme'] ?? 'https')) === 'http'
        ? 'http'
        : 'https';

    return [
        'snapshot' => $scheme . '://web.archive.org/web/' . $timestamp
            . 'id_/' . $original . $query,
        'original' => $original . $query,
    ];
}

/**
 * Recover a blocked article from a Wayback Machine snapshot.
 *
 * @return array{paragraphs:list<string>, image:string}|null
 */
function fetchArticleArchiveFallback(string $url, string $sourceKey): ?array
{
    if (isNonArticleUrl($url)) {
        return null;
    }

    // When the story already points at a Wayback URL, retry that snapshot
    // directly. If it is unavailable, also look up a snapshot of the URL
    // embedded in the archive path instead of querying the archive URL itself.
    $candidates = [];
    $wayback = parseWaybackUrl($url);
    if ($wayback !== null) {
        $candidates[] = $wayback['snapshot'];
        $embeddedSnapshot = articleArchiveSnapshotUrl(
            $wayback['original'],
            $sourceKey
        );
        if ($embeddedSnapshot !== '') {
            $candidates[] = $embeddedSnapshot;
        }
    } else {
        $snapshotUrl = articleArchiveSnapshotUrl($url, $sourceKey);
        if ($snapshotUrl !== '') {
            $candidates[] = $snapshotUrl;
        }
    }

    foreach (array_values(array_unique($candidates)) as $snapshotUrl) {
        for ($attempt = 0; $attempt < 2; $attempt++) {
            $body = fetchUrl(
                $snapshotUrl,
                'text/html, application/xhtml+xml;q=0.9, */*;q=0.8',
                $sourceKey
            );
            if (!is_string($body) || trim($body) === '') {
                if ($attempt === 0) {
                    usleep(300000);
                }
                continue;
            }
            $paragraphs = trimArticleChrome(
                extractArticleParagraphs($body, $snapshotUrl)
            );
            if ($paragraphs && !articleBodyIsPlaceholder($paragraphs)) {
                return [
                    'paragraphs' => $paragraphs,
                    'image' => extractArticleImageFromHtml($body, $snapshotUrl),
                ];
            }
            if ($attempt === 0) {
                usleep(300000);
            }
        }
    }

    return null;
}

/**
 * Recover a blocked article from its publisher's public feed.
 *
 * @return array{paragraphs:list<string>, image:string}|null
 */
function fetchArticleFeedFallback(string $url, string $sourceKey): ?array
{
    static $feeds = [];

    $feedUrls = articleFeedFallbackUrls($url);
    $targetKey = articleUrlKey($url);
    if (!$feedUrls || $targetKey === '') {
        return null;
    }

    foreach ($feedUrls as $feedUrl) {
        if (!array_key_exists($feedUrl, $feeds)) {
            $feeds[$feedUrl] = fetchUrl(
                $feedUrl,
                'application/rss+xml, application/xml;q=0.9, */*;q=0.8',
                $sourceKey,
                0,
                true
            );
        }
        $feed = $feeds[$feedUrl];
        if (!is_string($feed) || trim($feed) === '') {
            continue;
        }

        $previous = libxml_use_internal_errors(true);
        $document = new DOMDocument();
        $loaded = $document->loadXML($feed, LIBXML_NONET | LIBXML_NOERROR | LIBXML_NOWARNING);
        libxml_clear_errors();
        libxml_use_internal_errors($previous);
        if (!$loaded) {
            continue;
        }

        $xpath = new DOMXPath($document);
        $items = $xpath->query('//item');
        if ($items === false) {
            continue;
        }

        foreach ($items as $item) {
            $matches = false;
            foreach ($item->childNodes as $child) {
                if (!($child instanceof DOMElement)) {
                    continue;
                }
                $name = strtolower($child->localName);
                if ($name !== 'link' && $name !== 'guid') {
                    continue;
                }
                if (articleUrlKey(trim((string)$child->textContent)) === $targetKey) {
                    $matches = true;
                    break;
                }
            }
            if (!$matches) {
                continue;
            }

            $bodyHtml = feedArticleBodyHtml($item);
            $paragraphs = extractFeedArticleParagraphs($bodyHtml);
            if (!$paragraphs) {
                continue;
            }
            $image = feedArticleImage($item, $url);
            if ($image === '') {
                $image = extractArticleImageFromHtml($bodyHtml, $url);
            }
            return [
                'paragraphs' => $paragraphs,
                'image' => $image,
            ];
        }
    }

    return null;
}

/**
 * Detect a feed body that is only a publisher preview rather than the story.
 *
 * Many RSS feeds (Medium is the canonical case) publish a lead excerpt plus a
 * "Continue reading ..." link while the article page itself is behind a bot
 * challenge. Such a story must be enriched from the page/reader mirror even
 * though the feed technically supplied a paragraph or two.
 *
 * @param list<string> $paragraphs
 */
function feedBodyIsPreview(array $paragraphs, string $rawHtml = ''): bool
{
    $text = '';
    foreach ($paragraphs as $paragraph) {
        $paragraph = trim((string)$paragraph);
        if ($paragraph !== '') {
            $text .= ($text === '' ? '' : ' ') . $paragraph;
        }
    }
    if ($text === '') {
        return true;
    }
    $pattern = '/\b(?:continue reading|read (?:the )?(?:full|rest(?: of the)?) '
        . '(?:story|article)|read on)\b/iu';
    if (preg_match($pattern, $text)) {
        return true;
    }
    // The continuation link is often stripped as boilerplate before this
    // check, so also inspect the raw feed HTML that carried the preview.
    if ($rawHtml !== '') {
        $rawText = html_entity_decode(strip_tags($rawHtml), ENT_QUOTES | ENT_HTML5, 'UTF-8');
        if (preg_match($pattern, normalizeText($rawText))) {
            return true;
        }
    }
    return false;
}

/**
 * Detect a reader-mirror "not found" page.
 *
 * Reader mirrors answer HTTP 200 for a deleted or moved story and render a
 * short placeholder such as "We couldn't find the article you're looking for."
 * That text must never be cached or shown as the article body.
 *
 * @param list<string> $paragraphs
 */
function articleBodyIsPlaceholder(array $paragraphs): bool
{
    $text = '';
    foreach (array_slice($paragraphs, 0, 3) as $paragraph) {
        $text .= ' ' . normalizeText((string)$paragraph);
    }
    $text = strtolower(trim($text));
    if ($text === '') {
        return false;
    }
    $needles = [
        "couldn't find the article",
        'could not find the article',
        "can't find the article",
        'cannot find the article',
        "couldn't find that story",
        "can't find that story",
        'article not found',
        'story not found',
        'page not found',
        'this story is no longer available',
        'this article is no longer available',
    ];
    foreach ($needles as $needle) {
        if (str_contains($text, $needle)) {
            return true;
        }
    }
    return false;
}

/**
 * Detect a publisher URL that is a profile or landing page, not an article.
 *
 * Google News' site:medium.com feed occasionally indexes an author profile
 * (for example https://medium.com/@handle). There is no story body to load and
 * no point retrying the blocked page or a reader mirror.
 */
function isNonArticleUrl(string $url): bool
{
    $parts = parse_url(trim($url));
    if (!is_array($parts) || empty($parts['host'])) {
        return false;
    }
    $host = strtolower((string)$parts['host']);
    $path = (string)($parts['path'] ?? '/');
    if ($host === 'medium.com' || str_ends_with($host, '.medium.com')) {
        return (bool)preg_match('{^/@[^/]+/?$}', $path);
    }
    return false;
}

/**
 * A feed story needs a page fetch when it carries no usable body text, only a
 * publisher preview, or when its source explicitly asks for the complete
 * article page.
 */
function storyNeedsArticleContent(
    array $story,
    bool $forceFullArticle = false
): bool {
    if (trim((string)($story['link'] ?? '')) === '') {
        return false;
    }
    if ($forceFullArticle) {
        return true;
    }
    if (!empty($story['feed_preview'])) {
        return true;
    }
    if (feedBodyIsPreview($story['paragraphs'] ?? [])) {
        return true;
    }
    return false;
}

/**
 * Apply a cached article body to a story in place.
 */
function applyArticleContent(array &$story, array $entry): void
{
    $link = trim((string)($entry['link'] ?? ''));
    if ($link !== '') {
        $story['link'] = $link;
    }

    $paragraphs = [];
    foreach (($entry['paragraphs'] ?? []) as $paragraph) {
        $text = trim((string)$paragraph);
        if ($text !== '') {
            $paragraphs[] = $text;
        }
    }
    if ($paragraphs) {
        $story['paragraphs'] = $paragraphs;
        // Keep the feed's concise list summary when it has one; the fetched
        // article body is for the detail view and may start with a byline.
        if (trim((string)($story['summary'] ?? '')) === '') {
            $story['summary'] = makeSummary($paragraphs[0]);
        }
    }

    $image = trim((string)($entry['image'] ?? ''));
    if ($image !== '') {
        $story['images'] = mergeImageUrls([$image], $story['images'] ?? []);
    }
}

/**
 * Give stories that only contain a feed headline/summary a real article body.
 *
 * Google News feeds are the main case: their <item> has no content and its
 * link is a redirect wrapper. Resolving the link and fetching the publisher
 * page makes the "details" popup show the actual story instead of an empty
 * body. Successful pages are cached on disk to keep repeated /news calls fast.
 *
 * With $cacheOnly the function never touches the network: list views reuse
 * bodies that are already cached and leave the rest for the on-demand detail
 * request. This keeps /news, /news search, and /medium fast instead of
 * downloading every article up front.
 */
function enrichStoryContent(
    array $stories,
    string $sourceKey,
    bool $retryFailedCache = false,
    bool $cacheOnly = false
): array {
    if (!$stories) {
        return $stories;
    }

    $sources = newsSources();
    $forceFullArticle = !empty($sources[$sourceKey]['full_article']);
    $cache = loadArticleCache();
    $now = time();
    $requests = [];
    $googleLinks = [];
    $originals = [];
    $titles = [];

    foreach ($stories as $index => &$story) {
        if (!storyNeedsArticleContent($story, $forceFullArticle)) {
            continue;
        }
        $link = trim((string)($story['link'] ?? ''));
        if ($link === '') {
            continue;
        }

        $entry = $cache[$link] ?? null;
        if (is_array($entry)) {
            $paragraphs = $entry['paragraphs'] ?? [];
            // A reader-mirror placeholder cached by an older run is not a
            // valid article body; refetch it instead of serving "not found".
            $placeholderCache = $paragraphs && articleBodyIsPlaceholder($paragraphs);
            if ($placeholderCache) {
                $paragraphs = [];
                $entry['paragraphs'] = [];
            }
            $ttl = $paragraphs ? ARTICLE_CACHE_TTL : ARTICLE_CACHE_FAILURE_TTL;
            if (!$paragraphs) {
                $entryLink = trim((string)($entry['link'] ?? ''));
                if (articleReaderFallbackUrls($link)
                    || articleReaderFallbackUrls($entryLink)
                ) {
                    $ttl = min($ttl, ARTICLE_READER_FAILURE_TTL);
                }
            }
            // Do not reuse a feed-excerpt entry when the source requires the
            // full article body; let it be fetched once and cached again.
            $cacheIsUsable = !$forceFullArticle
                || !empty($entry['full_article']);
            $cacheIsUsable = $cacheIsUsable
                && !$placeholderCache
                && (int)($entry['extractor'] ?? 0) === ARTICLE_EXTRACTOR_VERSION;
            // A Wayback story that previously failed before the archive-aware
            // fallback existed must be retried instead of serving an empty
            // failure entry for the rest of the hour.
            if (!$paragraphs && parseWaybackUrl($link) !== null) {
                $cacheIsUsable = false;
            }
            // A detail request explicitly asks for the article body, so an
            // empty cached failure must not suppress a fresh mirror retry.
            if ($retryFailedCache && !$paragraphs) {
                $cacheIsUsable = false;
            }
            if ($cacheIsUsable && (int)($entry['ts'] ?? 0) + $ttl > $now) {
                applyArticleContent($story, $entry);
                continue;
            }
        }

        // Lazy/list mode only reuses cached bodies. The detail modal fetches
        // the full article on demand through --article when it is opened.
        if ($cacheOnly) {
            // The feed body was only a headline/preview and the full article
            // is not cached yet. Drop the preview so the detail modal does not
            // treat the snippet as the complete story and fetches it on demand.
            $story['paragraphs'] = [];
            continue;
        }

        $originals[$index] = $link;
        $titles[$index] = trim((string)($story['title'] ?? ''));
        $requests[$index] = $link;
        if (isGoogleNewsLink($link)) {
            $googleLinks[$index] = $link;
        }
    }
    unset($story);

    if (!$requests) {
        return $stories;
    }

    $resolved = $googleLinks
        ? resolveGoogleNewsLinks($googleLinks, $sourceKey)
        : [];

    $pageRequests = [];
    foreach ($requests as $index => $link) {
        $fetchLink = $resolved[$index] ?? $link;
        // An unresolved Google News wrapper has no readable article body.
        if (isGoogleNewsLink($fetchLink)) {
            continue;
        }
        // A Google News entry that resolves to a profile/landing page is not
        // a story; drop it instead of reporting a failed article fetch.
        if (isNonArticleUrl($fetchLink)) {
            unset($stories[$index], $originals[$index]);
            continue;
        }
        $pageRequests[$index] = ['url' => $fetchLink];
    }

    $pages = $pageRequests
        ? fetchUrlsConcurrently($pageRequests, $sourceKey)
        : [];

    foreach ($originals as $index => $originalLink) {
        $finalLink = $resolved[$index] ?? $originalLink;
        $storyTitle = $titles[$index] ?? '';
        $page = $pages[$index] ?? null;
        $entry = [
            'link' => $finalLink,
            'paragraphs' => [],
            'image' => '',
            'full_article' => $forceFullArticle,
            'extractor' => ARTICLE_EXTRACTOR_VERSION,
            'ts' => $now,
        ];
        $partial = null;

        $pageFailed = false;
        $pageStatus = 0;
        if (is_array($page) && $page['body'] !== '') {
            $pageStatus = (int)$page['status'];
            if ($pageStatus < 200 || $pageStatus >= 400) {
                // A bot challenge can still be recovered by a feed or reader
                // mirror, so report the failure only after all fallbacks.
                $pageFailed = true;
            } else {
                $direct = articleResultFromBody($page['body'], $finalLink);
                if ($direct !== null) {
                    $entry['paragraphs'] = $direct['paragraphs'];
                    $entry['image'] = $direct['image'];
                } else {
                    // Some WAFs answer HTTP 200 with a challenge page.
                    $pageFailed = true;
                }
            }
        }

        // A WAF can reject one browser identity with a challenge while the
        // same article is served normally to another. Retry the publisher
        // page with alternate browser identities before using mirrors/feeds.
        if (!$entry['paragraphs'] && $pageFailed) {
            $retry = fetchArticleWithAlternateBrowsers($finalLink, $sourceKey);
            if ($retry !== null) {
                $entry['paragraphs'] = $retry['paragraphs'];
                $entry['image'] = $retry['image'];
                $pageFailed = false;
            }
        }

        // A bot challenge or paywall can hide the page body while the
        // publisher's public feed still carries the full story (Axios) or at
        // least its lead image and summary (NYT, FT, NewsNation).
        if (!$entry['paragraphs']) {
            considerArticleFallback(
                $entry,
                fetchArticleFeedFallback($finalLink, $sourceKey),
                $partial
            );
        }

        // Medium has no full-story feed and challenges direct page requests,
        // so a reader mirror is the only source of the complete article.
        if (!$entry['paragraphs']) {
            considerArticleFallback(
                $entry,
                fetchArticleReaderFallback($finalLink, $sourceKey),
                $partial
            );
        }

        // WordPress REST often exposes the full post even when the rendered
        // page is blocked (NewsNation and many other news sites).
        if (!$entry['paragraphs']) {
            considerArticleFallback(
                $entry,
                fetchArticleWordPressFallback($finalLink, $sourceKey),
                $partial
            );
        }

        // Generic reader proxies render some blocked pages from a different
        // network (Google Translate for AP, Jina Reader for other sites).
        if (!$entry['paragraphs']) {
            considerArticleFallback(
                $entry,
                fetchArticleProxyFallback($finalLink, $sourceKey),
                $partial
            );
        }

        // Paywalled/wire stories are often republished in full by partners.
        // Match the headline through Google News and use an accessible copy.
        if (!$entry['paragraphs'] && $storyTitle !== '') {
            considerArticleFallback(
                $entry,
                fetchArticleSyndicationFallback(
                    $finalLink,
                    $storyTitle,
                    $sourceKey
                ),
                $partial
            );
        }

        // Cloudflare-protected publishers may block both the live page and
        // generic reader mirrors. Fall back to a public Wayback snapshot so
        // older review/archive links still open with their complete body.
        if (!$entry['paragraphs']) {
            considerArticleFallback(
                $entry,
                fetchArticleArchiveFallback($finalLink, $sourceKey),
                $partial
            );
        }

        // A partial feed/mirror body is still far better than an empty story
        // modal. Use it only after every full-body recovery path failed.
        if (!$entry['paragraphs'] && $partial !== null) {
            $entry['paragraphs'] = $partial['paragraphs'];
            if ($entry['image'] === '' && !empty($partial['image'])) {
                $entry['image'] = $partial['image'];
            }
        }

        if (!$entry['paragraphs'] && $pageFailed) {
            diag(
                "news_fetch ({$sourceKey}): could not load article "
                . "{$finalLink} (HTTP {$pageStatus})"
            );
        }

        $cache[$originalLink] = $entry;
        if (isset($stories[$index])) {
            applyArticleContent($stories[$index], $entry);
        }
    }

    saveArticleCache($cache);
    return array_values($stories);
}

/**
 * Fetch one article on demand for the story-detail modal.
 *
 * The list feed may have failed transiently (the reader mirror can rate-limit
 * a burst of requests), so a detail request ignores cached empty bodies and
 * tries again. Good cached bodies are still reused.
 *
 * @return array{link:string, paragraphs:list<string>, images:list<string>}
 */
function fetchArticleByUrl(string $url, string $title = ''): array
{
    $story = [
        'title' => trim($title),
        'link' => $url,
        'published' => '',
        'sort' => 0,
        'summary' => '',
        'source' => 'Article',
        'section' => '',
        'paragraphs' => [],
        'images' => [],
        'feed_preview' => true,
    ];
    $stories = enrichStoryContent([$story], 'article', true);
    $story = $stories[0] ?? $story;
    $paragraphs = [];
    foreach (($story['paragraphs'] ?? []) as $paragraph) {
        $text = trim((string)$paragraph);
        if ($text !== '') {
            $paragraphs[] = $text;
        }
    }
    return [
        'link' => trim((string)($story['link'] ?? $url)),
        'paragraphs' => $paragraphs,
        'images' => array_values(($story['images'] ?? [])),
    ];
}

/**
 * Boilerplate lines that show up inside article bodies.
 */
function isBoilerplate(string $text): bool
{
    return (bool)preg_match(
        '/^(?:read more|continue reading.*|read (?:the )?(?:full|rest(?: of the)?) '
        . '(?:story|article).*|share|advertisement|ad feedback|sign up for|'
        . 'click here to|watch:|\d+:\d+|\(?\s*photo(?:\s+by)?\s*:.*|'
        . 'image\s*:.*|caption\s*:.*)$/iu',
        $text
    );
}

/**
 * Extract readable paragraph text from a story's HTML body.
 *
 * @return list<string>
 */
function extractParagraphs(string $html, string $summary = ''): array
{
    if (trim($html) === '') {
        return $summary !== '' ? [$summary] : [];
    }

    $previous = libxml_use_internal_errors(true);
    $document = new DOMDocument();
    $loaded = $document->loadHTML(
        '<?xml encoding="utf-8"?><div>' . $html . '</div>',
        LIBXML_NONET | LIBXML_NOERROR | LIBXML_NOWARNING
    );
    libxml_clear_errors();
    libxml_use_internal_errors($previous);

    $paragraphs = [];
    $seen = [];
    if ($loaded) {
        foreach ($document->getElementsByTagName('p') as $node) {
            $text = normalizeText($node->textContent);
            if ($text === '' || isBoilerplate($text)) {
                continue;
            }
            if (isset($seen[$text])) {
                continue;
            }
            $seen[$text] = true;
            $paragraphs[] = $text;
        }
    }

    if (!$paragraphs && $summary !== '') {
        $paragraphs[] = $summary;
    }
    return $paragraphs;
}

/**
 * Trim a list-view summary to a single readable lead paragraph.
 */
function makeSummary(string $text): string
{
    $text = trim($text);
    if (mb_strlen($text) > 400) {
        $text = rtrim(mb_substr($text, 0, 397)) . '…';
    }
    return $text;
}

/**
 * Parse one RSS feed into stories.
 *
 * @return list<array{title:string,link:string,published:string,sort:int,summary:string,source:string,section:string,paragraphs:list<string>}>
 */
function parseRssStories(
    string $xml,
    string $sourceKey,
    string $sourceLabel,
    string $sectionLabel
): array {
    $previous = libxml_use_internal_errors(true);
    $feed = simplexml_load_string(
        $xml,
        'SimpleXMLElement',
        LIBXML_NONET | LIBXML_NOERROR | LIBXML_NOWARNING
    );
    libxml_clear_errors();
    libxml_use_internal_errors($previous);

    if ($feed === false || !isset($feed->channel->item)) {
        diag("news_fetch ({$sourceKey}): a feed was not valid RSS");
        return [];
    }

    $stories = [];
    foreach ($feed->channel->item as $item) {
        $title = cleanTitle((string)$item->title);
        $link = trim((string)$item->link);
        $published = trim((string)$item->pubDate);
        if ($published === '') {
            $published = trim((string)$item->children('dc', true)->date);
        }
        $timestamp = $published !== '' ? strtotime($published) : false;

        $description = normalizeText(strip_tags((string)$item->description));
        $content = $item->children(CONTENT_NS);
        $html = isset($content->encoded) ? (string)$content->encoded : '';
        if (trim($html) === '') {
            // CNN's openrss.org feeds put the full article in <description>.
            $html = (string)$item->description;
        }

        $paragraphs = extractParagraphs($html);
        $feedPreview = feedBodyIsPreview($paragraphs, $html);
        $summary = makeSummary($paragraphs[0] ?? $description);
        $images = mergeImageUrls(
            imageUrlsFromHtml($html, $link),
            mediaImageUrls($item, $link)
        );

        $stories[] = [
            'title' => $title,
            'link' => $link,
            'published' => $timestamp !== false
                ? date(DATE_ATOM, $timestamp)
                : $published,
            'sort' => $timestamp !== false ? $timestamp : 0,
            'summary' => $summary,
            'source' => $sourceLabel,
            'section' => $sectionLabel,
            'paragraphs' => $paragraphs,
            'images' => $images,
            'feed_preview' => $feedPreview,
        ];
    }

    // Newest first within a section.
    usort(
        $stories,
        static fn(array $a, array $b): int => $b['sort'] <=> $a['sort']
    );
    return $stories;
}

/**
 * Parse the CBC Lite homepage into stories.
 *
 * The Lite site is a Next.js application. Its server-rendered
 * <script id="__NEXT_DATA__"> payload contains the current article list under
 * props.pageProps.articles. Each entry has a headline, URL, section, and an
 * updatedAt timestamp in milliseconds; the linked CBC article page is fetched
 * later for the full body and cover image.
 *
 * @return list<array{title:string,link:string,published:string,sort:int,summary:string,source:string,section:string,paragraphs:list<string>}>
 */
function parseCbcLiteStories(
    string $html,
    string $sourceKey,
    string $sourceLabel,
    string $sectionLabel
): array {
    if (!preg_match(
        '/<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)<\/script>/s',
        $html,
        $match
    )) {
        diag("news_fetch ({$sourceKey}): CBC Lite page did not contain __NEXT_DATA__");
        return [];
    }

    $data = json_decode($match[1], true);
    if (!is_array($data)) {
        diag("news_fetch ({$sourceKey}): CBC Lite __NEXT_DATA__ was not valid JSON");
        return [];
    }

    $articles = $data['props']['pageProps']['articles'] ?? null;
    if (!is_array($articles)) {
        diag("news_fetch ({$sourceKey}): CBC Lite page did not contain an article list");
        return [];
    }

    $stories = [];
    foreach ($articles as $article) {
        if (!is_array($article)) {
            continue;
        }
        $main = $article['components']['mainContent'] ?? null;
        if (!is_array($main)) {
            $main = [];
        }

        $title = cleanTitle((string)($article['title'] ?? $main['title'] ?? ''));
        $link = trim((string)($article['url'] ?? $main['url'] ?? ''));
        if ($link === '') {
            continue;
        }

        $sectionPath = trim((string)(
            $main['section']['path'] ?? $article['section']['path'] ?? ''
        ));
        $published = '';
        $timestamp = 0;
        $updatedAt = $article['updatedAt'] ?? $main['updatedAt'] ?? '';
        if (is_numeric($updatedAt) && (float)$updatedAt > 0) {
            $timestamp = (int)floor((float)$updatedAt / 1000);
            $published = date(DATE_ATOM, $timestamp);
        }

        $stories[] = [
            'title' => $title,
            'link' => $link,
            'published' => $published,
            'sort' => $timestamp,
            'summary' => '',
            'source' => $sourceLabel,
            'section' => $sectionPath !== '' ? $sectionPath : $sectionLabel,
            'paragraphs' => [],
            'images' => [],
        ];
    }

    usort(
        $stories,
        static fn(array $a, array $b): int => $b['sort'] <=> $a['sort']
    );
    return $stories;
}

/**
 * Parse the NPR text-only homepage into stories.
 *
 * https://text.npr.org/ lists each current story as an <a class="topic-title">
 * link to a lightweight article page (for example /nx-s1-6034483). The list
 * carries only headlines, so each linked page is fetched later for the body.
 *
 * @return list<array{title:string,link:string,published:string,sort:int,summary:string,source:string,section:string,paragraphs:list<string>}>
 */
function parseNprTextStories(
    string $html,
    string $sourceKey,
    string $sourceLabel,
    string $sectionLabel
): array {
    if (trim($html) === '') {
        diag("news_fetch ({$sourceKey}): NPR text homepage was empty");
        return [];
    }

    $previous = libxml_use_internal_errors(true);
    $document = new DOMDocument();
    $loaded = $document->loadHTML(
        '<?xml encoding="utf-8"?>' . $html,
        LIBXML_NONET | LIBXML_NOERROR | LIBXML_NOWARNING
    );
    libxml_clear_errors();
    libxml_use_internal_errors($previous);
    if (!$loaded) {
        diag("news_fetch ({$sourceKey}): NPR text homepage could not be parsed");
        return [];
    }

    // The homepage date is the only date on the list view; use it for the
    // section's stories and keep the listing order (which is the site's sort).
    $published = '';
    $timestamp = 0;
    $xpath = new DOMXPath($document);
    $dateNodes = $xpath->query(
        '//p[contains(concat(" ", normalize-space(@class), " "), " topic-date ")]'
    );
    if ($dateNodes !== false && $dateNodes->length > 0) {
        $dateText = normalizeText($dateNodes->item(0)->textContent);
        $parsed = $dateText !== '' ? strtotime($dateText) : false;
        if ($parsed !== false) {
            $timestamp = $parsed;
            $published = date(DATE_ATOM, $timestamp);
        }
    }

    $links = $xpath->query(
        '//a[contains(concat(" ", normalize-space(@class), " "), " topic-title ")]'
    );
    if ($links === false) {
        diag("news_fetch ({$sourceKey}): NPR text homepage links could not be read");
        return [];
    }

    $stories = [];
    $position = 0;
    foreach ($links as $link) {
        $title = cleanTitle($link->textContent);
        $href = trim((string)$link->getAttribute('href'));
        if ($title === '' || $href === '') {
            continue;
        }

        if (str_starts_with($href, '/')) {
            $href = 'https://text.npr.org' . $href;
        }
        if (!preg_match('~^https?://(?:www\.)?text\.npr\.org(/.*)$~i', $href, $match)) {
            continue;
        }
        $path = $match[1];
        // Article ids look like /nx-s1-6034483 or /g-s1-151625. Skip the
        // homepage and topic/category links that are not stories.
        if (!preg_match('~^/[a-z]+-s\d+-\d+$~i', $path)) {
            continue;
        }

        $stories[] = [
            'title' => $title,
            'link' => 'https://text.npr.org' . $path,
            'published' => $published,
            // Preserve the homepage's own order: the first headline is the
            // most prominent story on the current text-only front page.
            'sort' => $timestamp > 0 ? $timestamp - $position : 1000000 - $position,
            'summary' => '',
            'source' => $sourceLabel,
            'section' => $sectionLabel,
            'paragraphs' => [],
            'images' => [],
        ];
        $position++;
    }

    usort(
        $stories,
        static fn(array $a, array $b): int => $b['sort'] <=> $a['sort']
    );
    return $stories;
}

/**
 * Parse the SF Bay View WordPress posts endpoint into stories.
 *
 * @return list<array{title:string,link:string,published:string,sort:int,summary:string,source:string,section:string,paragraphs:list<string>}>
 */
function parseWpJsonStories(
    string $json,
    string $sourceKey,
    string $sourceLabel,
    string $sectionLabel
): array {
    $posts = json_decode($json, true);
    if (!is_array($posts)) {
        diag("news_fetch ({$sourceKey}): the endpoint did not return a JSON list");
        return [];
    }

    $stories = [];
    foreach ($posts as $post) {
        if (!is_array($post)) {
            continue;
        }
        $title = cleanTitle((string)($post['title']['rendered'] ?? ''));
        $link = trim((string)($post['link'] ?? ''));
        $published = trim((string)($post['date'] ?? ''));
        $timestamp = $published !== '' ? strtotime($published) : false;

        $contentHtml = (string)($post['content']['rendered'] ?? '');
        $excerptHtml = (string)($post['excerpt']['rendered'] ?? '');
        $excerpt = normalizeText(strip_tags($excerptHtml));
        $paragraphs = extractParagraphs($contentHtml, $excerpt);
        $summary = makeSummary($excerpt !== '' ? $excerpt : ($paragraphs[0] ?? ''));
        $images = mergeImageUrls(
            wpFeaturedImageUrls($post, $link),
            imageUrlsFromHtml($contentHtml . "\n" . $excerptHtml, $link)
        );

        $stories[] = [
            'title' => $title,
            'link' => $link,
            'published' => $timestamp !== false
                ? date(DATE_ATOM, $timestamp)
                : $published,
            'sort' => $timestamp !== false ? $timestamp : 0,
            'summary' => $summary,
            'source' => $sourceLabel,
            'section' => $sectionLabel,
            'paragraphs' => $paragraphs,
            'images' => $images,
            'feed_preview' => $feedPreview,
        ];
    }

    usort(
        $stories,
        static fn(array $a, array $b): int => $b['sort'] <=> $a['sort']
    );
    return $stories;
}

/**
 * Normalise a story link for de-duplication (the same story often appears
 * in more than one CNN section feed).
 */
function storyKey(array $story): string
{
    $key = strtolower($story['link']);
    $key = preg_replace('/[?#].*$/', '', $key) ?? $key;
    return rtrim($key, '/');
}

/**
 * Remove internal-only fields before emitting an event.
 */
function publicStory(array $story): array
{
    unset($story['sort']);
    return $story;
}

/**
 * Collect featured images exposed by WordPress REST API responses.
 *
 * Plugins such as Jetpack expose a ready-made URL, Yoast embeds an Open
 * Graph image, and the core API can inline the media object in _embedded.
 * Using all three keeps image coverage high across WordPress sources.
 *
 * @return list<string>
 */
function wpFeaturedImageUrls(array $post, string $baseUrl = ''): array
{
    $urls = [];

    $jetpack = $post['jetpack_featured_media_url'] ?? '';
    if (is_string($jetpack) && $jetpack !== '') {
        $urls[] = $jetpack;
    }

    $ogImages = $post['yoast_head_json']['og_image'] ?? [];
    if (is_array($ogImages)) {
        foreach ($ogImages as $image) {
            if (is_array($image) && isset($image['url'])) {
                $urls[] = (string)$image['url'];
            }
        }
    }

    $embedded = $post['_embedded']['wp:featuredmedia'] ?? [];
    if (is_array($embedded)) {
        foreach ($embedded as $media) {
            if (!is_array($media)) {
                continue;
            }
            if (isset($media['source_url'])) {
                $urls[] = (string)$media['source_url'];
            }
            $sizes = $media['media_details']['sizes'] ?? [];
            if (is_array($sizes)) {
                foreach ($sizes as $size) {
                    if (is_array($size) && isset($size['source_url'])) {
                        $urls[] = (string)$size['source_url'];
                    }
                }
            }
        }
    }

    $normalized = [];
    foreach ($urls as $url) {
        $url = normalizeImageUrl($url, $baseUrl);
        if ($url !== '' && looksLikeImageUrl($url)) {
            $normalized[] = $url;
        }
    }
    return mergeImageUrls($normalized);
}

/**
 * Build the per-page URL for the SF Bay View WordPress endpoint.
 */
function wpFeedUrl(string $baseUrl, int $limit): string
{
    $separator = str_contains($baseUrl, '?') ? '&' : '?';
    return $baseUrl . $separator . 'per_page=' . min(MAX_WP_PER_PAGE, max(1, $limit));
}

/**
 * Lazily fetch the selected sources, yielding (source key, source label,
 * feed label, stories, warning) tuples as each feed arrives.
 */
function storyStream(array $sourceKeys, ?int $limit, ?string $section, bool $lazy = false): Generator
{
    $seen = [];
    $total = 0;

    $sources = newsSources();
    foreach ($sourceKeys as $sourceKey) {
        if (!isset($sources[$sourceKey])) {
            diag("news_fetch: source is not configured: {$sourceKey}");
            continue;
        }
        $source = $sources[$sourceKey];
        if ($limit !== null) {
            $sourceLimit = $limit - $total;
        } else {
            $sourceLimit = $source['default_limit'];
        }
        if ($sourceLimit <= 0) {
            return;
        }

        $feeds = $source['feeds'];
        if ($section !== null) {
            if ($sourceKey !== 'cnn' || !isset($feeds[$section])) {
                diag("news_fetch: unknown section: {$section}");
                continue;
            }
            $feeds = [$section => $feeds[$section]];
        }

        $emitted = 0;
        $warned = false;
        $first = true;
        foreach ($feeds as $feed) {
            if ($emitted >= $sourceLimit) {
                break;
            }
            if (!$first && $sourceKey === 'cnn') {
                // Keep requests spaced out so openrss.org does not rate-limit us.
                sleep(2);
            }
            $first = false;

            $kind = $source['kind'];
            $url = $kind === 'wp_json'
                ? wpFeedUrl($feed['url'], min($sourceLimit - $emitted, MAX_WP_PER_PAGE))
                : $feed['url'];
            if ($kind === 'wp_json') {
                $accept = 'application/json, text/plain, */*';
            } elseif ($kind === 'cbc_lite' || $kind === 'npr_text') {
                $accept = 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8';
            } else {
                $accept = 'application/rss+xml, application/xml, text/xml, */*';
            }

            $body = fetchUrl($url, $accept, $sourceKey);
            if ($body === null) {
                $warned = true;
                yield [
                    'source_key' => $sourceKey,
                    'source_label' => $source['label'],
                    'feed_label' => $feed['label'],
                    'stories' => [],
                    'warning' => "Could not load the {$source['label']} "
                        . "{$feed['label']} feed.",
                ];
                continue;
            }

            if ($kind === 'wp_json') {
                $stories = parseWpJsonStories(
                    $body,
                    $sourceKey,
                    $source['label'],
                    $feed['label']
                );
            } elseif ($kind === 'cbc_lite') {
                $stories = parseCbcLiteStories(
                    $body,
                    $sourceKey,
                    $source['label'],
                    $feed['label']
                );
            } elseif ($kind === 'npr_text') {
                $stories = parseNprTextStories(
                    $body,
                    $sourceKey,
                    $source['label'],
                    $feed['label']
                );
            } else {
                $stories = parseRssStories(
                    $body,
                    $sourceKey,
                    $source['label'],
                    $feed['label']
                );
            }

            if ($sourceKey === 'googlenews') {
                $quotedPatterns = quotedSearchPhrasePatterns(googleNewsQuery());
                if ($quotedPatterns !== []) {
                    $stories = array_values(array_filter(
                        $stories,
                        static fn(array $story): bool => storyMatchesQuotedPhrases(
                            $story,
                            $quotedPatterns
                        )
                    ));
                }
            }

            $selected = [];
            $feedEmitted = 0;
            $feedLimit = $source['per_feed_limit'] ?? null;
            foreach ($stories as $story) {
                $key = storyKey($story);
                if ($key !== '' && isset($seen[$key])) {
                    continue;
                }
                if ($key !== '') {
                    $seen[$key] = true;
                }
                $selected[] = publicStory($story);
                $emitted++;
                $feedEmitted++;
                $total++;
                if ($emitted >= $sourceLimit) {
                    break;
                }
                if ($feedLimit !== null && $feedEmitted >= $feedLimit) {
                    break;
                }
            }

            if ($lazy) {
                // List views must not wait for article pages. Reuse only
                // what is already cached; the detail modal loads the rest.
                $selected = enrichStoryContent($selected, $sourceKey, false, true);
                $selected = enrichStoryImages($selected, $sourceKey, true);
            } else {
                $selected = enrichStoryContent($selected, $sourceKey);
                $selected = enrichStoryImages($selected, $sourceKey);
            }
            yield [
                'source_key' => $sourceKey,
                'source_label' => $source['label'],
                'feed_label' => $feed['label'],
                'stories' => $selected,
                'warning' => null,
            ];

            if ($limit !== null && $total >= $limit) {
                return;
            }
        }

        if ($emitted === 0 && !$warned) {
            yield [
                'source_key' => $sourceKey,
                'source_label' => $source['label'],
                'feed_label' => '',
                'stories' => [],
                'warning' => "No stories were found for {$source['label']}.",
            ];
        }
    }
}

/**
 * Stream stories as NDJSON events for app/chat_server.py.
 */
function streamJson(iterable $stream, array $sourceKeys, ?int $limit): void
{
    $available = newsSources();
    $sources = array_map(
        static fn(string $key): string => $available[$key]['label'] ?? $key,
        $sourceKeys
    );
    emitEvent([
        'type' => 'start',
        'source' => count($sources) === 1 ? $sources[0] : 'News',
        'sources' => $sources,
        'limit' => $limit,
    ]);

    $count = 0;
    foreach ($stream as $batch) {
        if ($batch['warning'] !== null) {
            emitEvent(['type' => 'warning', 'message' => $batch['warning']]);
            continue;
        }
        foreach ($batch['stories'] as $story) {
            $count++;
            $position = $count;
            emitEvent([
                'type' => 'item',
                'index' => $position,
                'title' => $story['title'],
                'link' => $story['link'],
                'published' => $story['published'],
                'source' => $story['source'],
                'section' => $story['section'],
                'summary' => $story['summary'],
                'images' => $story['images'] ?? [],
            ]);
            foreach ($story['paragraphs'] as $paragraph) {
                emitEvent([
                    'type' => 'paragraph',
                    'index' => $position,
                    'text' => $paragraph,
                ]);
            }
            emitEvent(['type' => 'item_end', 'index' => $position]);
        }
    }

    emitEvent(['type' => 'done', 'count' => $count]);
}

/**
 * Stream stories as readable terminal text.
 */
function streamHuman(iterable $stream, array $sourceKeys, ?int $limit): void
{
    echo "News\n";
    echo str_repeat('=', 74) . "\n\n";
    fflush(STDOUT);

    $count = 0;
    $currentSource = null;
    foreach ($stream as $batch) {
        if ($batch['warning'] !== null) {
            diag($batch['warning']);
            continue;
        }

        $sourceLabel = $batch['source_label'];
        if ($sourceLabel !== $currentSource) {
            echo "## {$sourceLabel}\n\n";
            $currentSource = $sourceLabel;
        }
        $feedLabel = $batch['feed_label'];
        if ($feedLabel !== '' && $feedLabel !== $sourceLabel) {
            echo "### {$feedLabel}\n\n";
        }

        foreach ($batch['stories'] as $story) {
            $count++;
            echo $count . '. ' . $story['title'] . "\n";
            if ($story['published'] !== '') {
                echo '   ' . $story['published'] . "\n";
            }
            if ($story['link'] !== '') {
                echo '   ' . $story['link'] . "\n";
            }
            if ($story['summary'] !== '') {
                echo '   ' . $story['summary'] . "\n";
            }
            foreach (($story['images'] ?? []) as $image) {
                echo '   image: ' . $image . "\n";
            }
            echo "\n";
            fflush(STDOUT);

            foreach ($story['paragraphs'] as $paragraph) {
                echo '   ' . $paragraph . "\n\n";
                fflush(STDOUT);
            }
            echo str_repeat('-', 74) . "\n\n";
            fflush(STDOUT);
        }
    }

    if ($count === 0) {
        echo "No stories found.\n";
    }
}

function main(array $argv): int
{
    $json = false;
    $limit = null;
    $section = null;
    $sourceKeys = null;
    $articleUrl = '';
    $articleTitle = '';
    $lazy = false;

    // Pre-scan for --search so the dynamic googlenews source exists before
    // --source googlenews is validated below.
    foreach ($argv as $index => $argument) {
        if (($argument === '--search' || $argument === '--query')
            && isset($argv[$index + 1])
        ) {
            $GLOBALS['NEWS_GOOGLE_NEWS_QUERY'] = trim($argv[$index + 1]);
        } elseif (preg_match('/^--(?:search|query)=(.+)$/', $argument, $match)) {
            $GLOBALS['NEWS_GOOGLE_NEWS_QUERY'] = trim($match[1]);
        }
    }
    if (googleNewsQuery() === '' && in_array('--search', $argv, true)) {
        diag('news_fetch: --search needs a keyword query');
        return 2;
    }

    $addSources = static function (string $value) use (&$sourceKeys): bool {
        $available = newsSources();
        foreach (preg_split('/[\s,]+/', strtolower($value)) as $requested) {
            if ($requested === '') {
                continue;
            }
            if ($requested === 'medium_author'
                && !isset($available['medium_author'])
            ) {
                diag('news_fetch: medium_author is not configured; write your '
                    . 'Medium handle to ~/.medium/username');
                return false;
            }
            if (!isset($available[$requested])) {
                diag("news_fetch: unknown source: {$requested}");
                return false;
            }
            if ($sourceKeys === null) {
                $sourceKeys = [];
            }
            if (!in_array($requested, $sourceKeys, true)) {
                $sourceKeys[] = $requested;
            }
        }
        return true;
    };

    for ($i = 1; $i < count($argv); $i++) {
        $argument = $argv[$i];
        if ($argument === '--json') {
            $json = true;
        } elseif ($argument === '--source' && isset($argv[$i + 1])) {
            if (!$addSources($argv[++$i])) {
                return 2;
            }
        } elseif (preg_match('/^--source=(.+)$/', $argument, $match)) {
            if (!$addSources($match[1])) {
                return 2;
            }
        } elseif ($argument === '--sources' && isset($argv[$i + 1])) {
            if (!$addSources($argv[++$i])) {
                return 2;
            }
        } elseif (preg_match('/^--sources=(.+)$/', $argument, $match)) {
            if (!$addSources($match[1])) {
                return 2;
            }
        } elseif ($argument === '--limit' && isset($argv[$i + 1])) {
            $limit = max(1, (int)$argv[++$i]);
        } elseif (preg_match('/^--limit=(\d+)$/', $argument, $match)) {
            $limit = max(1, (int)$match[1]);
        } elseif ($argument === '--section' && isset($argv[$i + 1])) {
            $section = strtolower($argv[++$i]);
        } elseif (preg_match('/^--section=(.+)$/', $argument, $match)) {
            $section = strtolower($match[1]);
        } elseif ($argument === '--search' || $argument === '--query') {
            // Already applied by the pre-scan above.
            $i++;
        } elseif (preg_match('/^--(?:search|query)=(.+)$/', $argument)) {
            // Already applied by the pre-scan above.
        } elseif ($argument === '--article' && isset($argv[$i + 1])) {
            $articleUrl = trim($argv[++$i]);
        } elseif (preg_match('/^--article=(.+)$/', $argument, $match)) {
            $articleUrl = trim($match[1]);
        } elseif ($argument === '--title' && isset($argv[$i + 1])) {
            $articleTitle = trim($argv[++$i]);
        } elseif (preg_match('/^--title=(.+)$/', $argument, $match)) {
            $articleTitle = trim($match[1]);
        } elseif ($argument === '--lazy') {
            // List mode: never download article bodies; the detail modal
            // fetches the selected story on demand via --article.
            $lazy = true;
        } elseif ($argument === '--help' || $argument === '-h') {
            echo "Usage: news_fetch.php [--json] [--source "
                . implode('|', array_keys(SOURCES)) . "|medium_author|googlenews"
                . " ...] [--limit N] [--lazy]\n"
                . "       news_fetch.php [--json] --search <keywords> [--limit N] [--lazy]\n"
                . "       news_fetch.php [--json] --article <url> [--title <headline>]\n";
            return 0;
        } else {
            diag("news_fetch: ignoring unknown argument: {$argument}");
        }
    }

    if ($articleUrl !== '') {
        if (!preg_match('{^https?://}i', $articleUrl)) {
            diag('news_fetch: --article needs an http(s) URL');
            return 2;
        }
        $article = fetchArticleByUrl($articleUrl, $articleTitle);
        if ($json) {
            echo json_encode(
                $article,
                JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES
            ) . "\n";
        } else {
            echo ($article['link'] !== '' ? $article['link'] : $articleUrl)
                . "\n\n";
            foreach ($article['paragraphs'] as $paragraph) {
                echo $paragraph . "\n\n";
            }
        }
        return $article['paragraphs'] ? 0 : 1;
    }

    if ($sourceKeys === null) {
        // A search query is a complete request by itself; otherwise default
        // to every static source as before.
        $sourceKeys = googleNewsQuery() !== ''
            ? ['googlenews']
            : array_keys(SOURCES);
    }
    if ($sourceKeys === []) {
        diag('news_fetch: no news sources selected');
        return 2;
    }

    if ($section !== null) {
        if ($sourceKeys !== ['cnn']) {
            diag('news_fetch: --section is only supported with --source cnn');
            return 2;
        }
        if (!isset(SOURCES['cnn']['feeds'][$section])) {
            diag("news_fetch: unknown section: {$section}");
            return 2;
        }
    }

    $stream = storyStream($sourceKeys, $limit, $section, $lazy);
    if ($json) {
        streamJson($stream, $sourceKeys, $limit);
    } else {
        streamHuman($stream, $sourceKeys, $limit);
    }
    return 0;
}

exit(main($argv));
