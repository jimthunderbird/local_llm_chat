#!/usr/bin/env php
<?php

declare(strict_types=1);

/**
 * Fetch the latest stories from every news source supported by /news and
 * stream each story's title + paragraphs + source images, source by source.
 *
 * Sources:
 *   cnn           CNN RSS feeds served by openrss.org (top/world/us/politics/business)
 *   yahoo_finance Yahoo Finance stock market news via Google News RSS
 *   kqed          KQED News RSS feed
 *   sfgate        SFGATE Bay Area News RSS feed
 *   sfbayview     SF Bay View WordPress REST API
 *   mission_local Mission Local WordPress REST API
 *   richmond_sunset Richmond Review / Richmond & Sunset news RSS feed
 *   techcrunch    TechCrunch WordPress REST API
 *   krasia        KrASIA RSS feed (Asian tech and startup news)
 *   medium        The Medium author feed configured in ~/.medium/username
 *                 (https://medium.com/feed/@username), used by /medium
 *
 * Default output is a human-readable stream for the terminal. The web chat
 * passes --json and receives one JSON event per line (NDJSON), which
 * app/chat_server.py relays to app/chat.html as it arrives.
 *
 * Usage:
 *   ./news_fetch.php [--json] [--source SOURCE ...] [--limit N]
 *
 * --source accepts cnn, yahoo_finance, kqed, sfgate, sfbayview, mission_local,
 * richmond_sunset, techcrunch, krasia, or medium and may be repeated (or given as a
 * comma separated list).
 * Without --source, every static source is fetched and emitted as its own
 * section; medium is only used when explicitly selected.
 * --limit caps the total number of stories across all selected sources (each
 * source has its own default when --limit is omitted).
 */

const SOURCES = [
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
];

const USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
    . 'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';

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
 * Build the dynamic Medium source used by /medium.
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
function newsSources(): array
{
    static $sources = null;
    if ($sources === null) {
        $sources = SOURCES;
        $medium = mediumSource();
        if ($medium !== null) {
            $sources['medium'] = $medium;
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
const ARTICLE_FETCH_CONCURRENCY = 6;
const MAX_ARTICLE_CACHE_ENTRIES = 500;

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
function fetchUrl(string $url, string $accept, string $sourceKey, int $attempt = 0): ?string
{
    $context = stream_context_create([
        'http' => [
            'method' => 'GET',
            'header' => 'User-Agent: ' . USER_AGENT . "\r\n"
                . 'Accept: ' . $accept . "\r\n"
                . "Accept-Language: en-US,en;q=0.9\r\n",
            'ignore_errors' => true,
            'timeout' => 30,
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
        diag("news_fetch ({$sourceKey}): {$url} returned HTTP {$status}; retrying in {$wait}s");
        sleep($wait);
        return fetchUrl($url, $accept, $sourceKey, $attempt + 1);
    }

    diag("news_fetch ({$sourceKey}): could not download {$url} (HTTP {$status})");
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
function enrichStoryImages(array $stories, string $sourceKey): array
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
    return (bool)preg_match(
        '/^(?:skip to|sign in|log in|subscribe|advertisement|ad feedback|'
        . 'we use cookies|this site uses cookies|cookie policy|'
        . 'by continuing|terms of service|privacy policy|follow us|'
        . 'download (?:the|our) app|get the app|enable javascript|'
        . 'javascript is (?:disabled|not)|share this|related:|'
        . 'recommended|most popular|click here|watch:|image:|'
        . 'caption:|photo:|all rights reserved|©)/iu',
        $text
    );
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
        '//article//p',
        '//*[contains(concat(" ", normalize-space(@class), " "), " article-body ")]//p',
        '//*[contains(concat(" ", normalize-space(@class), " "), " article-content ")]//p',
        '//*[contains(concat(" ", normalize-space(@class), " "), " entry-content ")]//p',
        '//*[contains(concat(" ", normalize-space(@class), " "), " post-content ")]//p',
        '//main//p',
    ];

    $fallback = [];
    foreach ($queries as $query) {
        $nodes = $xpath->query($query);
        if ($nodes === false) {
            continue;
        }
        $candidate = collectParagraphNodes($nodes);
        if (count($candidate) >= 2) {
            return $candidate;
        }
        if (!$fallback && $candidate) {
            $fallback = $candidate;
        }
    }

    if ($fallback) {
        return $fallback;
    }

    $nodes = $xpath->query('//p');
    return $nodes === false ? [] : collectParagraphNodes($nodes);
}

/**
 * A feed story needs a page fetch when it carries no usable body text, or
 * when its source explicitly asks for the complete article page.
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
    foreach (($story['paragraphs'] ?? []) as $paragraph) {
        if (trim((string)$paragraph) !== '') {
            return false;
        }
    }
    return true;
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
 */
function enrichStoryContent(array $stories, string $sourceKey): array
{
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
            $ttl = $paragraphs ? ARTICLE_CACHE_TTL : ARTICLE_CACHE_FAILURE_TTL;
            // Do not reuse a feed-excerpt entry when the source requires the
            // full article body; let it be fetched once and cached again.
            $cacheIsUsable = !$forceFullArticle
                || !empty($entry['full_article']);
            if ($cacheIsUsable && (int)($entry['ts'] ?? 0) + $ttl > $now) {
                applyArticleContent($story, $entry);
                continue;
            }
        }

        $originals[$index] = $link;
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
        $pageRequests[$index] = ['url' => $fetchLink];
    }

    $pages = $pageRequests
        ? fetchUrlsConcurrently($pageRequests, $sourceKey)
        : [];

    foreach ($originals as $index => $originalLink) {
        $finalLink = $resolved[$index] ?? $originalLink;
        $page = $pages[$index] ?? null;
        $entry = [
            'link' => $finalLink,
            'paragraphs' => [],
            'image' => '',
            'full_article' => $forceFullArticle,
            'ts' => $now,
        ];

        if (is_array($page) && $page['body'] !== '') {
            if ($page['status'] < 200 || $page['status'] >= 400) {
                diag(
                    "news_fetch ({$sourceKey}): could not load article "
                    . "{$finalLink} (HTTP {$page['status']})"
                );
            } else {
                $entry['paragraphs'] = extractArticleParagraphs(
                    $page['body'],
                    $finalLink
                );
                $entry['image'] = extractArticleImageFromHtml(
                    $page['body'],
                    $finalLink
                );
            }
        }

        $cache[$originalLink] = $entry;
        if (isset($stories[$index])) {
            applyArticleContent($stories[$index], $entry);
        }
    }

    saveArticleCache($cache);
    return $stories;
}

/**
 * Boilerplate lines that show up inside article bodies.
 */
function isBoilerplate(string $text): bool
{
    return (bool)preg_match(
        '/^(?:read more|share|advertisement|ad feedback|sign up for|'
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
function storyStream(array $sourceKeys, ?int $limit, ?string $section): Generator
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
            $accept = $kind === 'wp_json'
                ? 'application/json, text/plain, */*'
                : 'application/rss+xml, application/xml, text/xml, */*';

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

            $stories = $kind === 'wp_json'
                ? parseWpJsonStories($body, $sourceKey, $source['label'], $feed['label'])
                : parseRssStories($body, $sourceKey, $source['label'], $feed['label']);

            $selected = [];
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
                $total++;
                if ($emitted >= $sourceLimit) {
                    break;
                }
            }

            $selected = enrichStoryContent($selected, $sourceKey);
            $selected = enrichStoryImages($selected, $sourceKey);
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

    $addSources = static function (string $value) use (&$sourceKeys): bool {
        $available = newsSources();
        foreach (preg_split('/[\s,]+/', strtolower($value)) as $requested) {
            if ($requested === '') {
                continue;
            }
            if ($requested === 'medium' && !isset($available['medium'])) {
                diag('news_fetch: medium is not configured; write your Medium '
                    . 'handle to ~/.medium/username');
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
        } elseif ($argument === '--help' || $argument === '-h') {
            echo "Usage: news_fetch.php [--json] [--source "
                . implode('|', array_keys(SOURCES)) . "|medium"
                . " ...] [--limit N]\n";
            return 0;
        } else {
            diag("news_fetch: ignoring unknown argument: {$argument}");
        }
    }

    if ($sourceKeys === null) {
        $sourceKeys = array_keys(SOURCES);
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

    $stream = storyStream($sourceKeys, $limit, $section);
    if ($json) {
        streamJson($stream, $sourceKeys, $limit);
    } else {
        streamHuman($stream, $sourceKeys, $limit);
    }
    return 0;
}

exit(main($argv));
