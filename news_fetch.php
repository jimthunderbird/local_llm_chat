#!/usr/bin/env php
<?php

declare(strict_types=1);

/**
 * Fetch the latest stories from every news source supported by /news and
 * stream each story's title + paragraphs, source by source.
 *
 * Sources:
 *   cnn        CNN RSS feeds served by openrss.org (top/world/us/politics/business)
 *   kqed       KQED News RSS feed
 *   sfbayview  SF Bay View WordPress REST API
 *
 * Default output is a human-readable stream for the terminal. The web chat
 * passes --json and receives one JSON event per line (NDJSON), which
 * app/chat_server.py relays to app/chat.html as it arrives.
 *
 * Usage:
 *   ./news_fetch.php [--json] [--source SOURCE] [--limit N]
 *
 * --source accepts cnn, kqed, or sfbayview. Without --source, every source is
 * fetched and emitted as its own section. --limit caps the total number of
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
];

const USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
    . 'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';
const CONTENT_NS = 'http://purl.org/rss/1.0/modules/content/';
const MAX_ATTEMPTS = 4;
const MAX_WP_PER_PAGE = 100;

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
            echo "Usage: news_fetch.php [--json] [--source cnn|kqed|sfbayview] "
                . "[--limit N]\n";
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
