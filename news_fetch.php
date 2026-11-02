#!/usr/bin/env php
<?php

declare(strict_types=1);

/**
 * Fetch the latest stories from every news source supported by /news and
 * stream each story's title + paragraphs + source images, source by source.
 *
 * Sources:
 *   cnn           CNN RSS feeds served by openrss.org (top/world/us/politics/business)
 *   kqed          KQED News RSS feed
 *   sfgate        SFGATE Bay Area News RSS feed
 *   sfbayview     SF Bay View WordPress REST API
 *   mission_local Mission Local WordPress REST API
 *   richmond_sunset Richmond Review / Richmond & Sunset news RSS feed
 *   sunset        Sunset Blog RSS feed
 *
 * Default output is a human-readable stream for the terminal. The web chat
 * passes --json and receives one JSON event per line (NDJSON), which
 * app/chat_server.py relays to app/chat.html as it arrives.
 *
 * Usage:
 *   ./news_fetch.php [--json] [--source SOURCE] [--limit N]
 *
 * --source accepts cnn, kqed, sfgate, sfbayview, mission_local,
 * richmond_sunset, or sunset. Without
 * --source, every source is fetched and emitted as its own section. --limit
 * caps the total number of
 * stories across all selected sources (each source has its own default when
 * --limit is omitted).
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
    'sunset' => [
        'label' => 'Sunset Post',
        'kind' => 'rss',
        'default_limit' => 20,
        'feeds' => [
            'latest' => [
                'label' => 'Latest Stories',
                'url' => 'https://thesunsetpost.onrender.com/rss.xml',
            ],
        ],
    ],
];

const USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
    . 'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';
const CONTENT_NS = 'http://purl.org/rss/1.0/modules/content/';
const MAX_ATTEMPTS = 4;
const MAX_WP_PER_PAGE = 100;
const MAX_STORY_IMAGES = 6;
const IMAGE_CACHE_TTL = 604800;       // one week for successful lookups
const IMAGE_CACHE_FAILURE_TTL = 86400; // one day for missing/broken images
const IMAGE_FETCH_CONCURRENCY = 6;

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

        $description = normalizeText((string)$item->description);
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
        $images = imageUrlsFromHtml(
            $contentHtml . "\n" . $excerptHtml,
            $link
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

    foreach ($sourceKeys as $sourceKey) {
        $source = SOURCES[$sourceKey];
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
    $sources = array_map(
        static fn(string $key): string => SOURCES[$key]['label'],
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
    $sourceKeys = array_keys(SOURCES);

    for ($i = 1; $i < count($argv); $i++) {
        $argument = $argv[$i];
        if ($argument === '--json') {
            $json = true;
        } elseif ($argument === '--source' && isset($argv[$i + 1])) {
            $requested = strtolower($argv[++$i]);
            if (!isset(SOURCES[$requested])) {
                diag("news_fetch: unknown source: {$requested}");
                return 2;
            }
            $sourceKeys = [$requested];
        } elseif (preg_match('/^--source=(.+)$/', $argument, $match)) {
            $requested = strtolower($match[1]);
            if (!isset(SOURCES[$requested])) {
                diag("news_fetch: unknown source: {$requested}");
                return 2;
            }
            $sourceKeys = [$requested];
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
                . implode('|', array_keys(SOURCES)) . "] [--limit N]\n";
            return 0;
        } else {
            diag("news_fetch: ignoring unknown argument: {$argument}");
        }
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
