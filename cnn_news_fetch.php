#!/usr/bin/env php
<?php

declare(strict_types=1);

/**
 * Fetch the CNN RSS feeds served by openrss.org and stream every story
 * title + paragraphs, section by section.
 *
 * Default output is a human-readable stream for the terminal. The web chat
 * passes --json and receives one JSON event per line (NDJSON), which
 * app/chat_server.py relays to app/chat.html as it arrives.
 *
 * Usage:
 *   ./cnn_news_fetch.php [--json] [--limit N] [--section NAME]
 *
 * --section accepts: top, world, us, politics, business (default: all).
 * --limit caps the total number of stories across sections (default 50,
 * which covers the ten items from each of the five feeds).
 */

const FEEDS = [
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
];

const USER_AGENT = 'article-vector-strategy-chat/1.0 (local RAG chatbot; cnn_news_fetch.php)';
const CONTENT_NS = 'http://purl.org/rss/1.0/modules/content/';
const DEFAULT_LIMIT = 50;
const MAX_ATTEMPTS = 4;

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
 * Download an openrss.org feed.
 *
 * openrss.org rate-limits bursts, so requests are spaced out by the caller
 * and HTTP 429/5xx responses are retried with exponential backoff.
 */
function fetchFeed(string $url, int $attempt = 0): ?string
{
    $context = stream_context_create([
        'http' => [
            'method' => 'GET',
            'header' => "User-Agent: " . USER_AGENT . "\r\n"
                . "Accept: application/rss+xml, application/xml, text/xml\r\n",
            'ignore_errors' => true,
            'timeout' => 30,
        ],
    ]);

    $body = @file_get_contents($url, false, $context);
    $headers = $http_response_header ?? [];
    $statusLine = $headers[0] ?? '';
    preg_match('{HTTP/\S*\s(\d{3})}', $statusLine, $match);
    $status = isset($match[1]) ? (int)$match[1] : ($body === false ? 500 : 200);

    $contentType = '';
    foreach ($headers as $header) {
        if (stripos($header, 'content-type:') === 0) {
            $contentType = strtolower(trim(substr($header, 13)));
        }
    }

    $looksLikeFeed = $body !== false
        && ($contentType === '' || str_contains($contentType, 'xml')
            || str_contains($contentType, 'rss'));
    if ($body !== false && $status >= 200 && $status < 400 && $looksLikeFeed) {
        return $body;
    }

    $retryable = $status === 429 || $status >= 500 || $body === false;
    if ($retryable && $attempt + 1 < MAX_ATTEMPTS) {
        $wait = 2 ** ($attempt + 1); // 2s, 4s, 8s
        diag("cnn_news_fetch: {$url} returned HTTP {$status}; retrying in {$wait}s");
        sleep($wait);
        return fetchFeed($url, $attempt + 1);
    }

    diag("cnn_news_fetch: could not download {$url} (HTTP {$status})");
    return null;
}

/**
 * Collapse HTML entities and whitespace into clean paragraph text.
 */
function normalizeText(string $html): string
{
    $text = html_entity_decode($html, ENT_QUOTES | ENT_HTML5, 'UTF-8');
    $text = str_replace("\xC2\xA0", ' ', $text); // non-breaking spaces
    $text = preg_replace('/\s+/u', ' ', $text) ?? $text;
    return trim($text);
}

/**
 * Boilerplate lines that show up inside CNN article bodies.
 */
function isBoilerplate(string $text): bool
{
    return (bool)preg_match(
        '/^(?:read more|share|advertisement|ad feedback|sign up for|'
        . 'click here to|watch:|\d+:\d+)$/iu',
        $text
    );
}

/**
 * Extract readable <p> paragraph text from the story's HTML.
 *
 * @return list<string>
 */
function extractParagraphs(string $html, string $summary): array
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
 * Parse one feed into stories.
 *
 * @return list<array{title:string,link:string,published:string,summary:string,section:string,paragraphs:list<string>}>
 */
function parseStories(string $xml, string $sectionLabel): array
{
    $previous = libxml_use_internal_errors(true);
    $feed = simplexml_load_string(
        $xml,
        'SimpleXMLElement',
        LIBXML_NONET | LIBXML_NOERROR | LIBXML_NOWARNING
    );
    libxml_clear_errors();
    libxml_use_internal_errors($previous);

    if ($feed === false || !isset($feed->channel->item)) {
        diag('cnn_news_fetch: a feed was not valid RSS');
        return [];
    }

    $stories = [];
    foreach ($feed->channel->item as $item) {
        $title = normalizeText((string)$item->title);
        $link = trim((string)$item->link);
        $published = trim((string)$item->pubDate);
        if ($published === '') {
            $published = trim((string)$item->children('dc', true)->date);
        }
        $timestamp = $published !== '' ? strtotime($published) : false;

        $content = $item->children(CONTENT_NS);
        $html = isset($content->encoded) ? (string)$content->encoded : '';
        if (trim($html) === '') {
            // CNN's openrss.org feeds put the full article in <description>.
            $html = (string)$item->description;
        }

        $paragraphs = extractParagraphs($html, '');
        // The feed description can be the whole article; keep the list view's
        // summary to a single readable lead paragraph.
        $summary = $paragraphs[0] ?? '';
        if (mb_strlen($summary) > 400) {
            $summary = rtrim(mb_substr($summary, 0, 397)) . '…';
        }

        $stories[] = [
            'title' => $title !== '' ? $title : '(untitled)',
            'link' => $link,
            'published' => $timestamp !== false
                ? date(DATE_ATOM, $timestamp)
                : $published,
            'sort' => $timestamp !== false ? $timestamp : 0,
            'summary' => $summary,
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
 * Normalise a story link for de-duplication (the same story often appears
 * in "Top Stories" and its own section feed).
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
 * Stream stories as NDJSON events for app/chat_server.py.
 */
function streamJson(iterable $sections): void
{
    emitEvent([
        'type' => 'start',
        'source' => 'CNN News',
        'feeds' => array_values(array_map(
            static fn(array $feed): string => $feed['url'],
            FEEDS
        )),
    ]);

    $count = 0;
    foreach ($sections as [$sectionLabel, $stories]) {
        foreach ($stories as $story) {
            $count++;
            $position = $count;
            emitEvent([
                'type' => 'item',
                'index' => $position,
                'title' => $story['title'],
                'link' => $story['link'],
                'published' => $story['published'],
                'section' => $sectionLabel,
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
function streamHuman(iterable $sections): void
{
    echo "CNN News — https://openrss.org/feed/www.cnn.com\n";
    echo str_repeat('=', 74) . "\n\n";
    fflush(STDOUT);

    $count = 0;
    foreach ($sections as [$sectionLabel, $stories]) {
        if (!$stories) {
            continue;
        }
        echo "## {$sectionLabel}\n\n";
        foreach ($stories as $story) {
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

/**
 * Lazily fetch the selected feeds, yielding (section label, stories) pairs
 * and skipping stories already seen in an earlier section.
 */
function sectionStream(array $sections, int $limit, bool $json): Generator
{
    $seen = [];
    $emitted = 0;
    $first = true;

    foreach ($sections as $key) {
        $feed = FEEDS[$key];
        if (!$first) {
            // Keep requests spaced out so openrss.org does not rate-limit us.
            sleep(2);
        }
        $first = false;

        $xml = fetchFeed($feed['url']);
        if ($xml === null) {
            if ($json) {
                emitEvent([
                    'type' => 'warning',
                    'message' => "Could not load the {$feed['label']} feed.",
                ]);
            } else {
                diag("cnn_news_fetch: skipping {$feed['label']}");
            }
            continue;
        }

        $stories = [];
        foreach (parseStories($xml, $feed['label']) as $story) {
            $key2 = storyKey($story);
            if ($key2 !== '' && isset($seen[$key2])) {
                continue;
            }
            if ($key2 !== '') {
                $seen[$key2] = true;
            }
            $stories[] = publicStory($story);
            $emitted++;
            if ($emitted >= $limit) {
                break;
            }
        }

        yield [$feed['label'], $stories];
        if ($emitted >= $limit) {
            return;
        }
    }
}

function main(array $argv): int
{
    $json = false;
    $limit = DEFAULT_LIMIT;
    $sections = array_keys(FEEDS);

    for ($i = 1; $i < count($argv); $i++) {
        $argument = $argv[$i];
        if ($argument === '--json') {
            $json = true;
        } elseif ($argument === '--limit' && isset($argv[$i + 1])) {
            $limit = max(1, (int)$argv[++$i]);
        } elseif (preg_match('/^--limit=(\d+)$/', $argument, $match)) {
            $limit = max(1, (int)$match[1]);
        } elseif ($argument === '--section' && isset($argv[$i + 1])) {
            $requested = strtolower($argv[++$i]);
            if (!isset(FEEDS[$requested])) {
                diag("cnn_news_fetch: unknown section: {$requested}");
                return 2;
            }
            $sections = [$requested];
        } elseif (preg_match('/^--section=(.+)$/', $argument, $match)) {
            $requested = strtolower($match[1]);
            if (!isset(FEEDS[$requested])) {
                diag("cnn_news_fetch: unknown section: {$requested}");
                return 2;
            }
            $sections = [$requested];
        } elseif ($argument === '--help' || $argument === '-h') {
            echo "Usage: cnn_news_fetch.php [--json] [--limit N] "
                . "[--section top|world|us|politics|business]\n";
            return 0;
        } else {
            diag("cnn_news_fetch: ignoring unknown argument: {$argument}");
        }
    }

    $stream = sectionStream($sections, $limit, $json);
    if ($json) {
        streamJson($stream);
    } else {
        streamHuman($stream);
    }
    return 0;
}

exit(main($argv));
