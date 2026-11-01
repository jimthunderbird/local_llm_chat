# Architectural Blueprint: Wikipedia CLI Tool

## Step 1: Parse Command-Line Arguments & Options

Capture the user's intent and operational flags from terminal input:

* **Query String:** Positional argument representing the topic or keywords.
* **Search Flag (`-s`, `--search`):** Boolean toggle to trigger candidate discovery instead of direct lookup.
* **Limit Flag (`-l`, `--limit`):** Integer defining the maximum number of returned search candidates (default: 5).
* **Language Option (`--lang`):** Two-letter ISO language code determining the Wikipedia edition (default: `en`).

---

## Step 2: Query Targeted Wikipedia API Endpoints

All requests use HTTP `GET`. Prepend the selected language edition base URL:
`https://{lang}.wikipedia.org`

### 1. Direct Article Summary Endpoint
* **Path:** `/api/rest_v1/page/summary/{title}`
* **Purpose:** Fetches the lead introductory text, disambiguation description, and canonical article metadata for a specific topic.
* **URL Formatting:** Replace whitespace with underscores (`_`) and URI-encode special characters.
* **Example:** `https://en.wikipedia.org/api/rest_v1/page/summary/James_Webb_Space_Telescope`

### 2. Semantic / Related Topics Endpoint
* **Path:** `/api/rest_v1/page/related/{title}`
* **Purpose:** Returns up to 20 semantically related articles based on title context; useful for finding alternate topics or suggesting options.
* **Example:** `https://en.wikipedia.org/api/rest_v1/page/related/Quantum_entanglement`

### 3. Full-Text Search Endpoint
* **Path:** `/w/rest.php/v1/search/page?q={query}&limit={limit}`
* **Purpose:** Executes a keyword search across Wikipedia page titles and snippet bodies when the input is broad or unstructured.
* **Example:** `https://en.wikipedia.org/w/rest.php/v1/search/page?q=artificial%20intelligence&limit=5`

---

## Step 3: Configure Required Request Headers

Every request must include an identifiable user agent header per Wikimedia policy:

* **Header Name:** `User-Agent`
* **Format:** `<ApplicationName>/<Version> (<ContactEmailOrRepositoryURL>)`
* **Example Value:** `TerminalWikiCLI/1.0 (dev@example.com)`

*Note: Omitting this header or using generic client defaults (e.g., standard curl or empty headers) risks an `HTTP 403 Forbidden` response.*

---

## Step 4: Network Execution & Status Routing

Route program logic based on HTTP status codes returned by the server:

* **`200 OK`:** Proceed to parse the JSON response body.
* **`404 Not Found`:**
  * When in Direct Summary Mode, treat a 404 as an exact title mismatch.
  * Trigger an automatic fallback to the Search Endpoint (`/w/rest.php/v1/search/page`) or Related Endpoint (`/api/rest_v1/page/related/{title}`) to find matching candidates rather than terminating.
* **`429 Too Many Requests`:** Parse the `Retry-After` header if present, alert the user about rate limits, and exit gracefully.
* **Network/DNS Error:** Catch connectivity failures and output an informative terminal message asking the user to verify network access.

---

## Step 5: JSON Payload Extraction

Parse the response payload to isolate display fields:

* **From `/page/summary/{title}`:**
  * `title`: Canonical page title.
  * `description`: Single-line disambiguation/topic descriptor.
  * `extract`: Plain-text lead paragraph summary.
  * `content_urls.desktop.page`: Browser link to the complete article.

* **From `/w/rest.php/v1/search/page`:**
  * `pages[]`: List of candidate search records.
  * `pages[].title`: Result entry title.
  * `pages[].description`: Short descriptive text of the result.
  * `pages[].excerpt`: Matching text snippet (strip embedded HTML tags like `<span class="searchmatch">`).

---

## Step 6: Terminal Rendering & Interactive Selection

Format the extracted data for console display:

* **Summary Mode:**
  1. Print the article title formatted in bold or ANSI color.
  2. Print the short description in an italicized or subdued secondary style.
  3. Wrap the text `extract` to 80 columns to prevent awkward terminal edge-wrapping.
  4. Display the direct browser URL at the bottom.

* **Search Mode:**
  1. Print an indexed list (1 through N) displaying each candidate's title and description.
  2. Block on standard input (`stdin`) prompting the user: `Enter number to view summary (or press Enter to exit):`.
  3. Validate that the input is a valid integer between 1 and N.
  4. Query `/api/rest_v1/page/summary/{chosen_title}` using the selected candidate's title and render the resulting summary.

---

## Step 7: Executable Packaging & Global Installation

Make the utility globally executable across the system:

1. **Permissions & Compilation:**
   * For compiled binaries: Build with release optimizations enabled.
   * For script-based runtimes: Include an appropriate executable header line (shebang) and apply executable execution permissions via filesystem attributes (`chmod +x`).
2. **Path Setup:**
   * Move or create a symbolic link pointing to the executable inside a system `$PATH` directory (e.g., `/usr/local/bin` or `~/.local/bin`).

## PHP Example:
```php
#!/usr/bin/env php
<?php

declare(strict_types=1);

const USER_AGENT = 'TerminalWikiCLI-PHP/1.0 (contact@example.com)';
const API_BASE = 'https://%s.wikipedia.org/api/rest_v1';
const SEARCH_BASE = 'https://%s.wikipedia.org/w/rest.php/v1/search/page';

/**
 * Perform an HTTP GET request and decode the JSON response.
 */
function fetchJson(string $url): ?array
{
    $options = [
        'http' => [
            'method' => 'GET',
            'header' => "User-Agent: " . USER_AGENT . "\r\nAccept: application/json\r\n",
            'ignore_errors' => true,
            'timeout' => 10,
        ],
    ];

    $context = stream_context_create($options);
    $response = @file_get_contents($url, false, $context);

    if ($response === false) {
        return null;
    }

    // Inspect HTTP status code
    $statusLine = $http_response_header[0] ?? '';
    preg_match('{HTTP\/\S*\s(\d{3})}', $statusLine, $match);
    $status = isset($match[1]) ? (int)$match[1] : 500;

    if ($status === 404) {
        return null;
    }

    if ($status >= 400) {
        fwrite(STDERR, "HTTP error: {$status}\n");
        return null;
    }

    $decoded = json_decode($response, true);
    return is_array($decoded) ? $decoded : null;
}

/**
 * Fetch the lead summary for a specific page title.
 */
function getSummary(string $title, string $lang = 'en'): ?array
{
    $formattedTitle = rawurlencode(str_replace(' ', '_', $title));
    $url = sprintf(API_BASE, $lang) . "/page/summary/{$formattedTitle}";
    return fetchJson($url);
}

/**
 * Perform a keyword search using the MediaWiki search endpoint.
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
 * Format and display the article summary in the terminal.
 */
function displayArticle(array $article): void
{
    $title = $article['title'] ?? 'Unknown Title';
    $description = $article['description'] ?? null;
    $extract = $article['extract'] ?? 'No text summary available.';
    $url = $article['content_urls']['desktop']['page'] ?? null;

    // ANSI escape codes: 1=bold, 36=cyan, 3=italic, 90=dim/grey, 0=reset
    echo "\n\033[1;36m=== {$title} ===\033[0m\n";
    if ($description) {
        echo "\033[3m{$description}\033[0m\n\n";
    } else {
        echo "\n";
    }

    // Word wrap summary at 80 columns
    echo wordwrap($extract, 80, "\n") . "\n";

    if ($url) {
        echo "\n\033[90mRead more: {$url}\033[0m\n\n";
    }
}

// -----------------------------------------------------------------------------
// CLI Argument Parsing & Execution
// -----------------------------------------------------------------------------

$shortOptions = 's::l::';
$longOptions = ['search', 'limit:', 'lang:'];
$options = getopt($shortOptions, $longOptions, $restIndex);

// Extract remaining non-flag arguments as the query
$queryArgs = array_slice($argv, $restIndex);
if (empty($queryArgs)) {
    echo "Usage: {$argv[0]} [options] <query>\n\n";
    echo "Options:\n";
    echo "  -s, --search       Display matching search results instead of jumping to summary\n";
    echo "  -l, --limit <n>    Number of search results to return (default: 5)\n";
    echo "      --lang <code>  Wikipedia language subdomain (default: en)\n";
    exit(1);
}

$query = implode(' ', $queryArgs);
$isSearch = isset($options['s']) || isset($options['search']);
$limit = (int)($options['l'] ?? $options['limit'] ?? 5);
$lang = (string)($options['lang'] ?? 'en');

if ($isSearch) {
    $results = searchPages($query, $limit, $lang);

    if (empty($results)) {
        echo "No search results found for '{$query}'.\n";
        exit(0);
    }

    echo "\nSearch results for '\033[1m{$query}\033[0m':\n\n";
    foreach ($results as $index => $item) {
        $num = $index + 1;
        $title = $item['title'] ?? '';
        $desc = $item['description'] ?? strip_tags($item['excerpt'] ?? 'No description');
        echo "  \033[1m{$num}.\033[0m {$title} \033[90m— {$desc}\033[0m\n";
    }

    echo "\nEnter number to view summary (or press Enter to exit): ";
    $choice = trim((string)fgets(STDIN));

    if (ctype_digit($choice)) {
        $idx = (int)$choice - 1;
        if (isset($results[$idx]['title'])) {
            $summary = getSummary($results[$idx]['title'], $lang);
            if ($summary) {
                displayArticle($summary);
            }
        }
    }
} else {
    $article = getSummary($query, $lang);

    // Fallback: If exact match 404s, try search
    if ($article === null) {
        echo "Exact match not found for '{$query}'. Searching related topics...\n";
        $results = searchPages($query, 1, $lang);
        if (!empty($results[0]['title'])) {
            $article = getSummary($results[0]['title'], $lang);
        }
    }

    if ($article !== null) {
        displayArticle($article);
    } else {
        echo "No articles found matching '{$query}'.\n";
    }
}
```
