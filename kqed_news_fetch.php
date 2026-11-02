#!/usr/bin/env php
<?php

declare(strict_types=1);

/**
 * Fetch the KQED News RSS feed and stream every story title + paragraphs.
 *
 * Default output is a human-readable stream for the terminal. The web chat
 * passes --json and receives one JSON event per line (NDJSON), which
 * app/chat_server.py relays to app/chat.html as it arrives.
 *
 * Usage:
 *   ./kqed_news_fetch.php [--json] [--limit N]
 */

const FEED_URL = 'https://ww2.kqed.org/news/feed/';
const USER_AGENT = 'article-vector-strategy-chat/1.0 (local RAG chatbot; kqed_news_fetch.php)';
const CONTENT_NS = 'http://purl.org/rss/1.0/modules/content/';
const DEFAULT_LIMIT = 20;

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
 * Download the feed. A single short retry handles transient network errors.
 */
function fetchFeed(int $attempt = 0): ?string
{
    $context = stream_context_create([
        'http' => [
            'method' => 'GET',
            'header' => "User-Agent: " . USER_AGENT . "\r\n"
                . "Accept: application/rss+xml, application/xml, text/xml\r\n",
            'ignore_errors' => true,
            'timeout' => 25,
        ],
    ]);

    $body = @file_get_contents(FEED_URL, false, $context);
    $headers = $http_response_header ?? [];
    $statusLine = $headers[0] ?? '';
    preg_match('{HTTP/\S*\s(\d{3})}', $statusLine, $match);
    $status = isset($match[1]) ? (int)$match[1] : ($body === false ? 500 : 200);

    if ($body !== false && $status < 400) {
        return $body;
    }
    if ($attempt < 1) {
        sleep(1);
        return fetchFeed($attempt + 1);
    }

    diag("kqed_news_fetch: could not download " . FEED_URL
        . " (HTTP {$status})");
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
 * Extract readable <p> paragraph text from the story's content:encoded HTML.
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
    if ($loaded) {
        foreach ($document->getElementsByTagName('p') as $node) {
            $text = normalizeText($node->textContent);
            if ($text === '' || preg_match('/^(?:read more|share|advertisement)$/i', $text)) {
                continue;
            }
            $paragraphs[] = $text;
        }
    }

    if (!$paragraphs && $summary !== '') {
        $paragraphs[] = $summary;
    }
    return $paragraphs;
}

/**
 * Parse the feed into stories. Reading it fully first keeps the streamed
 * events ordered and lets the UI show a total count.
 *
 * @return list<array{title:string,link:string,published:string,summary:string,paragraphs:list<string>}>
 */
function parseStories(string $xml, int $limit): array
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
        diag('kqed_news_fetch: the feed was not valid RSS');
        return [];
    }

    $stories = [];
    foreach ($feed->channel->item as $item) {
        if (count($stories) >= $limit) {
            break;
        }

        $title = normalizeText((string)$item->title);
        $link = trim((string)$item->link);
        $summary = normalizeText((string)$item->description);
        $published = trim((string)$item->pubDate);
        $timestamp = $published !== '' ? strtotime($published) : false;

        $content = $item->children(CONTENT_NS);
        $html = isset($content->encoded) ? (string)$content->encoded : '';

        $stories[] = [
            'title' => $title !== '' ? $title : '(untitled)',
            'link' => $link,
            'published' => $timestamp !== false
                ? date(DATE_ATOM, $timestamp)
                : $published,
            'summary' => $summary,
            'paragraphs' => extractParagraphs($html, $summary),
        ];
    }
    return $stories;
}

/**
 * Stream stories as NDJSON events for app/chat_server.py.
 */
function streamJson(array $stories): void
{
    emitEvent([
        'type' => 'start',
        'source' => 'KQED News',
        'feed' => FEED_URL,
        'count' => count($stories),
    ]);

    foreach ($stories as $index => $story) {
        $position = $index + 1;
        emitEvent([
            'type' => 'item',
            'index' => $position,
            'title' => $story['title'],
            'link' => $story['link'],
            'published' => $story['published'],
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

    emitEvent(['type' => 'done', 'count' => count($stories)]);
}

/**
 * Stream stories as readable terminal text.
 */
function streamHuman(array $stories): void
{
    echo "KQED News — " . FEED_URL . "\n";
    echo str_repeat('=', 74) . "\n\n";
    fflush(STDOUT);

    if (!$stories) {
        echo "No stories found.\n";
        return;
    }

    foreach ($stories as $index => $story) {
        echo ($index + 1) . '. ' . $story['title'] . "\n";
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

function main(array $argv): int
{
    $json = false;
    $limit = DEFAULT_LIMIT;

    for ($i = 1; $i < count($argv); $i++) {
        $argument = $argv[$i];
        if ($argument === '--json') {
            $json = true;
        } elseif ($argument === '--limit' && isset($argv[$i + 1])) {
            $limit = max(1, (int)$argv[++$i]);
        } elseif (preg_match('/^--limit=(\d+)$/', $argument, $match)) {
            $limit = max(1, (int)$match[1]);
        } elseif ($argument === '--help' || $argument === '-h') {
            echo "Usage: kqed_news_fetch.php [--json] [--limit N]\n";
            return 0;
        } else {
            diag("kqed_news_fetch: ignoring unknown argument: {$argument}");
        }
    }

    $xml = fetchFeed();
    if ($xml === null) {
        if ($json) {
            emitEvent(['type' => 'error', 'message' => 'Could not download the KQED News feed.']);
        }
        return 1;
    }

    $stories = parseStories($xml, $limit);
    if ($json) {
        streamJson($stories);
    } else {
        streamHuman($stories);
    }
    return $stories ? 0 : 1;
}

exit(main($argv));
