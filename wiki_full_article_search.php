#!/usr/bin/env php
<?php

declare(strict_types=1);

// Wikimedia asks API clients for a descriptive User-Agent. Placeholder
// addresses (for example contact@example.com) are aggressively rate limited,
// so identify this tool the same way the rest of the project does.
const USER_AGENT = 'article-vector-strategy-chat/1.0 (local RAG chatbot; wiki_full_article_search.php)';
const REST_API_BASE = 'https://%s.wikipedia.org/api/rest_v1';
const SEARCH_BASE = 'https://%s.wikipedia.org/w/rest.php/v1/search/page';
const ACTION_API_BASE = 'https://%s.wikipedia.org/w/api.php';

/**
 * Perform an HTTP GET request and decode the JSON response.
 *
 * A short retry is performed when Wikipedia rate-limits us (HTTP 429).
 */
function fetchJson(string $url, int $attempt = 0): ?array
{
    $options = [
        'http' => [
            'method' => 'GET',
            'header' => "User-Agent: " . USER_AGENT . "\r\nAccept: application/json\r\n",
            'ignore_errors' => true,
            'timeout' => 20,
        ],
    ];

    $context = stream_context_create($options);
    $response = @file_get_contents($url, false, $context);

    $statusLine = $http_response_header[0] ?? '';
    preg_match('{HTTP\/\S*\s(\d{3})}', $statusLine, $match);
    $status = isset($match[1]) ? (int)$match[1] : ($response === false ? 500 : 200);

    // Back off briefly if we are being rate limited, then try once more.
    if ($status === 429 && $attempt < 2) {
        $retryAfter = 2;
        foreach ($http_response_header as $header) {
            if (stripos($header, 'retry-after:') === 0) {
                $retryAfter = max(1, (int)trim(substr($header, strlen('retry-after:'))));
            }
        }
        sleep(min($retryAfter, 5));
        return fetchJson($url, $attempt + 1);
    }

    if ($response === false || $status >= 400) {
        return null;
    }

    $decoded = json_decode((string)$response, true);
    return is_array($decoded) ? $decoded : null;
}

/**
 * Fetch the short lead summary for a specific page.
 */
function getSummary(string $title, string $lang = 'en'): ?array
{
    $formattedTitle = rawurlencode(str_replace(' ', '_', $title));
    $url = sprintf(REST_API_BASE, $lang) . "/page/summary/{$formattedTitle}";
    return fetchJson($url);
}

/**
 * Fetch the complete, plain-text article content via the Action API.
 */
function getFullArticle(string $title, string $lang = 'en'): ?array
{
    $params = [
        'action' => 'query',
        'prop' => 'extracts|info',
        'inprop' => 'url',
        'explaintext' => '1',      // Return plain text instead of HTML
        'titles' => $title,
        'format' => 'json',
        'redirects' => '1',       // Follow article redirects automatically
    ];

    $url = sprintf(ACTION_API_BASE, $lang) . '?' . http_build_query($params);
    $data = fetchJson($url);

    if (empty($data['query']['pages'])) {
        return null;
    }

    $page = reset($data['query']['pages']);
    if (isset($page['missing'])) {
        return null;
    }

    return [
        'title' => $page['title'] ?? $title,
        'full_text' => $page['extract'] ?? '',
        'url' => $page['fullurl'] ?? "https://{$lang}.wikipedia.org/wiki/" . rawurlencode($title),
    ];
}

/**
 * Fetch only the lead section of an article.
 *
 * Requesting the intro as its own, small response lets callers show the top of
 * the article immediately while the complete article is still downloading.
 */
function getArticleIntro(string $title, string $lang = 'en'): ?array
{
    $params = [
        'action' => 'query',
        'prop' => 'extracts|info',
        'inprop' => 'url',
        'explaintext' => '1',
        'exintro' => '1',
        'exlimit' => '1',
        'titles' => $title,
        'format' => 'json',
        'redirects' => '1',
    ];

    $url = sprintf(ACTION_API_BASE, $lang) . '?' . http_build_query($params);
    $data = fetchJson($url);

    if (empty($data['query']['pages'])) {
        return null;
    }

    $page = reset($data['query']['pages']);
    if (isset($page['missing'])) {
        return null;
    }

    $intro = (string)($page['extract'] ?? '');
    if (trim($intro) === '') {
        return null;
    }

    return [
        'title' => $page['title'] ?? $title,
        'intro' => $intro,
        'url' => $page['fullurl'] ?? "https://{$lang}.wikipedia.org/wiki/" . rawurlencode($title),
    ];
}

/**
 * Keyword search across Wikipedia.
 */
function searchPages(string $query, int $limit = 5, string $lang = 'en'): array
{
    $url = sprintf(SEARCH_BASE, $lang) . '?' . http_build_query([
        'q' => $query,
        'limit' => $limit,
    ]);

    $data = fetchJson($url);
    return $data['pages'] ?? [];
}

/**
 * Display text through the system pager (less/more) if available and running in a TTY.
 */
function displayWithPager(string $text): void
{
    // If output is redirected to a file or pipe, or pager isn't available, print directly
    if (!function_exists('posix_isatty') || !posix_isatty(STDOUT)) {
        echo $text;
        return;
    }

    $pager = getenv('PAGER') ?: 'less -R';
    $process = @popen($pager, 'w');

    if (is_resource($process)) {
        fwrite($process, $text);
        pclose($process);
    } else {
        echo $text;
    }
}

/**
 * Format the lead summary.
 */
function formatSummary(array $article): string
{
    $title = $article['title'] ?? 'Unknown';
    $description = $article['description'] ?? null;
    $extract = $article['extract'] ?? 'No summary available.';
    $url = $article['content_urls']['desktop']['page'] ?? '';

    $out = "\n\033[1;36m=== {$title} ===\033[0m\n";
    if ($description) {
        $out .= "\033[3m{$description}\033[0m\n\n";
    } else {
        $out .= "\n";
    }

    $out .= wordwrap($extract, 80, "\n") . "\n";
    if ($url) {
        $out .= "\n\033[90mRead more: {$url}\033[0m\n";
    }

    return $out;
}

/**
 * Format the entire article text with section headers highlighted.
 */
function formatFullArticle(array $article): string
{
    $title = $article['title'] ?? 'Unknown';
    $content = $article['full_text'] ?? '';
    $url = $article['url'] ?? '';

    // Emit the article title as a level-1 Markdown heading. The browser uses
    // it as a sticky header so the title stays pinned to the top of the
    // screen while the long article scrolls (and streams) underneath it.
    $out = "\n\033[1;36m# " . $title . "\033[0m\n\n";
    $out .= formatArticleBody($content);
    $out .= "\n\033[90mSource: {$url}\033[0m\n";
    return $out;
}

/**
 * Format the body of an article (everything below the title).
 */
function formatArticleBody(string $content): string
{
    $out = '';

    // Style section headings (e.g., "== Section Name ==" or "=== Subsection ===")
    $lines = explode("\n", $content);
    $blank = 0;
    foreach ($lines as $line) {
        $trimmed = trim($line);

        if ($trimmed === '') {
            // Collapse runs of blank lines produced by the plain-text extract.
            if (++$blank > 1) {
                continue;
            }
            $out .= "\n";
            continue;
        }
        $blank = 0;

        if (preg_match('/^(=+)\s*(.*?)\s*\1$/', $trimmed, $matches)) {
            $depth = strlen($matches[1]);
            $heading = trim($matches[2]);
            if ($depth === 2) {
                $out .= "\n\033[1;33m## " . $heading . "\033[0m\n\n";
            } else {
                $out .= "\n\033[1m### " . $heading . "\033[0m\n\n";
            }
        } else {
            $out .= wordwrap($trimmed, 80, "\n") . "\n";
        }
    }

    return $out;
}

/**
 * Remove the lead section from the full extract.
 *
 * The intro and the full text come from the same API, so the intro is
 * normally a whitespace-insensitive prefix of the full extract. If it is not
 * (the articles changed between the two requests), the full text is returned
 * unchanged rather than losing content.
 */
function stripLeadingIntro(string $fullText, string $intro): string
{
    $normalize = static function (string $text): string {
        return trim((string)preg_replace('/\s+/u', ' ', $text));
    };

    $target = $normalize($intro);
    if ($target === '') {
        return $fullText;
    }

    $lines = preg_split('/\R/u', $fullText) ?: [];
    $consumed = 0;
    $accumulated = '';
    $matched = false;

    foreach ($lines as $index => $line) {
        $next = $normalize($accumulated . ' ' . $line);
        if ($next !== '' && !str_starts_with($target, $next)) {
            break;
        }
        $accumulated = $next;
        $consumed = $index + 1;
        if ($next === $target) {
            $matched = true;
            break;
        }
        if (strlen($accumulated) >= strlen($target)) {
            $matched = str_starts_with($target, $accumulated);
            break;
        }
    }

    if (!$matched) {
        return $fullText;
    }

    return implode("\n", array_slice($lines, $consumed));
}

/**
 * Fetch and display the full article for a title, falling back to search.
 *
 * The lead section is fetched and printed first, so anyone reading the output
 * through a pipe (notably the chat web server) sees the top of the article
 * while the rest of it is still downloading. The remaining sections are
 * printed as soon as the full extract arrives.
 */
function showFullArticle(string $query, string $lang): void
{
    $usePager = function_exists('posix_isatty') && posix_isatty(STDOUT);
    $buffer = '';
    $emit = static function (string $text) use (&$buffer, $usePager): void {
        if ($usePager) {
            $buffer .= $text;
            return;
        }
        if ($text === '') {
            return;
        }
        echo $text;
        @flush();
    };

    // Resolve the title and get the lead paragraph quickly.
    $intro = getArticleIntro($query, $lang);
    if ($intro === null) {
        $emit("Exact match not found for '{$query}'. Searching...\n");
        $results = searchPages($query, 1, $lang);
        if (!empty($results[0]['title'])) {
            $intro = getArticleIntro((string)$results[0]['title'], $lang);
        }
    }

    if ($intro === null) {
        // A 429/transient failure can masquerade as a missing page; tell the user.
        echo "No article found matching '{$query}'.\n";
        return;
    }

    $title = (string)$intro['title'];
    $introText = (string)$intro['intro'];

    // Show the top of the article immediately.
    $emit("\n\033[1;36m# " . $title . "\033[0m\n\n");
    $emit(formatArticleBody($introText));

    $article = getFullArticle($title, $lang);
    if ($article === null) {
        $emit("\n\033[90mSource: {$intro['url']}\033[0m\n");
        if ($usePager) {
            displayWithPager($buffer);
        }
        return;
    }

    $remaining = stripLeadingIntro((string)$article['full_text'], $introText);
    if (trim($remaining) !== '') {
        $emit("\n");
        $emit(formatArticleBody($remaining));
    }

    $emit("\n\033[90mSource: {$article['url']}\033[0m\n");

    if ($usePager) {
        displayWithPager($buffer);
    }
}

/**
 * Fetch and display only the lead summary for a title, falling back to search.
 */
function showSummary(string $query, string $lang): void
{
    $article = getSummary($query, $lang);

    if ($article === null) {
        echo "Exact match not found for '{$query}'. Searching...\n";
        $results = searchPages($query, 1, $lang);
        if (!empty($results[0]['title'])) {
            $article = getSummary($results[0]['title'], $lang);
        }
    }

    if ($article !== null) {
        echo formatSummary($article);
        return;
    }

    echo "No article found matching '{$query}'.\n";
}

// -----------------------------------------------------------------------------
// CLI Argument Parsing & Main Flow
// -----------------------------------------------------------------------------

$shortOptions = 'sf::l::';
$longOptions = ['search', 'full', 'summary', 'limit:', 'lang:'];
$options = getopt($shortOptions, $longOptions, $restIndex);

$queryArgs = array_slice($argv, $restIndex);
if (empty($queryArgs)) {
    echo "Usage: {$argv[0]} [options] <query>\n\n";
    echo "By default the complete article is returned.\n\n";
    echo "Options:\n";
    echo "  -s, --search       Search and list candidate topics\n";
    echo "      --summary      Show only the lead summary instead of the full article\n";
    echo "  -f, --full         Return the complete article (default)\n";
    echo "  -l, --limit <n>    Number of search results to list (default: 5)\n";
    echo "      --lang <code>  Wikipedia language code (default: en)\n";
    exit(1);
}

$query = implode(' ', $queryArgs);
$isSearch = isset($options['s']) || isset($options['search']);
$wantSummary = isset($options['summary']);
$limit = (int)($options['l'] ?? $options['limit'] ?? 5);
$lang = (string)($options['lang'] ?? 'en');

if ($isSearch) {
    $results = searchPages($query, $limit, $lang);
    if (empty($results)) {
        echo "No search results found for '{$query}'.\n";
        exit(0);
    }

    echo "\nResults for '\033[1m{$query}\033[0m':\n\n";
    foreach ($results as $index => $item) {
        $num = $index + 1;
        $title = $item['title'] ?? '';
        $desc = $item['description'] ?? strip_tags($item['excerpt'] ?? 'No description');
        echo "  \033[1m{$num}.\033[0m {$title} \033[90m— {$desc}\033[0m\n";
    }

    echo "\nEnter number to view (or press Enter to exit): ";
    $choice = trim((string)fgets(STDIN));

    if (ctype_digit($choice)) {
        $idx = (int)$choice - 1;
        if (isset($results[$idx]['title'])) {
            $chosenTitle = $results[$idx]['title'];
            if ($wantSummary) {
                $summary = getSummary($chosenTitle, $lang);
                if ($summary) {
                    echo formatSummary($summary);
                }
            } else {
                $article = getFullArticle($chosenTitle, $lang);
                if ($article) {
                    displayWithPager(formatFullArticle($article));
                }
            }
        }
    }
} else {
    if ($wantSummary) {
        showSummary($query, $lang);
    } else {
        showFullArticle($query, $lang);
    }
}
