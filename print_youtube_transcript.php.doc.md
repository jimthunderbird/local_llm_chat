# `print_youtube_transcript.php`

A single-file PHP CLI (no Composer dependencies) that fetches a YouTube
video's **English transcript** and prints it to **stdout** as a single line of
plain text. It performs no summarization with Ollama and writes no files.

```bash
./print_youtube_transcript.php FTwxtyOEGyM
```

---

## Table of contents

1. [Overview](#overview)
2. [Quick start](#quick-start)
3. [Requirements](#requirements)
4. [Command-line interface](#command-line-interface)
5. [Output](#output)
6. [How it works](#how-it-works)
7. [Function reference](#function-reference)
8. [Caption selection algorithm](#caption-selection-algorithm)
9. [Supported caption formats](#supported-caption-formats)
10. [HTTP requests made](#http-requests-made)
11. [Error handling](#error-handling)
12. [Relationship to other scripts](#relationship-to-other-scripts)
13. [Using it as a library](#using-it-as-a-library)
14. [Limitations and caveats](#limitations-and-caveats)
15. [Troubleshooting](#troubleshooting)
16. [Verification notes](#verification-notes)

---

## Overview

`print_youtube_transcript.php` is a thin, self-contained HTTP client for
YouTube's public caption data. It is a PHP port of the caption-fetching portion
of `fetch_youtube_transcript.py` (and of
`fetch_youtube_transcript.php`), with two intentional differences:

* It **does not** call Ollama or produce a summary.
* It **does not** create `./.chat/youtube_transcripts/<id>.transcript` or
  `<id>.title` files.

It supports:

* Videos with manually uploaded English captions.
* Videos with auto-generated ("ASR") English captions.
* Caption payloads in `json3`, `srv3` (XML `<p><s>`), and `srv1` (XML `<text>`)
  formats.
* Videos whose ID begins with `-` (e.g. `-PcBFgkrR2o`).

It does **not** support:

* Non-English transcript selection.
* Timestamps, speaker labels, or formatting — only the concatenated text.
* Full YouTube URLs; only the bare video ID (normally 11 characters) is
  accepted.
* Playlists or multiple videos in one invocation.

No Composer packages, API keys, or external tools such as `yt-dlp` are needed.
All caption acquisition is done with cURL and direct requests to
`youtube.com`.

---

## Quick start

```bash
# Make sure it is executable (already set in the repo)
chmod +x print_youtube_transcript.php

# Print a transcript
./print_youtube_transcript.php FTwxtyOEGyM

# Save to a file
php print_youtube_transcript.php FTwxtyOEGyM > transcript.txt

# Show help
./print_youtube_transcript.php --help
```

Exit status is `0` on success, so it composes cleanly in pipelines:

```bash
./print_youtube_transcript.php FTwxtyOEGyM | wc -w
```

---

## Requirements

| Requirement | Details |
|---|---|
| PHP | **8.0+** — the script uses `str_starts_with()`, `str_contains()`, and `declare(strict_types=1)` |
| SAPI | `cli` (the entry point is guarded by `PHP_SAPI === 'cli'`) |
| `ext-curl` | All HTTP traffic |
| `ext-dom` + `libxml` | Parsing `srv1` / `srv3` XML caption payloads |
| Network | Outbound HTTPS access to `www.youtube.com` |
| Shell | Nothing beyond PHP; shebang is `#!/usr/bin/env php` |

Verify the environment:

```bash
php -v
php -m | grep -E '^(curl|dom|libxml)$'
```

There are **no environment variables, config files, or CLI flags** for
timeouts, language, or proxy settings. All endpoints, client versions, and
timeouts are hard-coded.

> `ext-json` is bundled with PHP and is used for all JSON handling. The script
> also uses `RuntimeException` and `Throwable` from the PHP core, plus
> `DOMDocument` / `DOMXPath`, which require `ext-dom` (see above).

### Optional: proxy support

`httpRequest()` does not read `HTTP_PROXY` / `HTTPS_PROXY`. cURL will honor
`CURLOPT_PROXY` only if it were set, which this script does not do. If you need
a proxy, edit `httpRequest()` or wrap the calls.

---

## Command-line interface

### Synopsis

```
print_youtube_transcript.php [-h] youtube_video_hash
```

### Positional arguments

| Argument | Description |
|---|---|
| `youtube_video_hash` | Bare YouTube video ID, e.g. `FTwxtyOEGyM`. Must match `^[A-Za-z0-9_-]+$` after trimming. Exactly one is required. |

The value is **not** a URL and is **not** normalized. `https://www.youtube.com/watch?v=…`
will be rejected because `/`, `:`, `?`, and `=` are not in the allowed
character set.

### Options

| Option | Effect |
|---|---|
| `-h`, `--help` | Print usage to **stdout** and exit `0`. Short-circuits argument validation, so `script foo -h` still prints help. |
| `--` | End of options. Everything after it is treated as positional (a literal `-h` after `--` is therefore a video ID and will fail validation). |

There are no other options. Unknown dash-prefixed tokens before `--` are an
error.

### Special handling for IDs that start with `-`

Some YouTube IDs begin with a dash. If the **first** argument starts with `-`
and is not `-h`, `--help`, or `--`, the parser implicitly inserts `--` before
it:

```bash
# These are equivalent:
./print_youtube_transcript.php -PcBFgkrR2o
./print_youtube_transcript.php -- -PcBFgkrR2o
```

This applies only to the first argument. A later argument such as
`foo -PcBFgkrR2o` is still rejected as an unknown option.

### Exit codes

| Code | Meaning | Streams used |
|---|---|---|
| `0` | Transcript printed successfully, or help requested | stdout |
| `1` | Invalid video hash, or transcript retrieval failed (no transcript, empty transcript, missing extension, network error) | stderr for the message; stdout untouched |
| `2` | Usage error: wrong number of positional arguments, or unknown option | stderr (message + usage) |

All failures print a diagnostic to **stderr** and leave **stdout empty**, so
redirecting stdout to a file never yields a partial "error transcript".

### Examples

```bash
# Normal use
./print_youtube_transcript.php FTwxtyOEGyM

# Capture and inspect
php print_youtube_transcript.php FTwxtyOEGyM > japan.txt
head -c 300 japan.txt

# Video ID beginning with a dash
./print_youtube_transcript.php -- -PcBFgkrR2o

# Help
./print_youtube_transcript.php -h

# Missing argument (exit 2)
./print_youtube_transcript.php

# Unknown option (exit 2)
./print_youtube_transcript.php --verbose FTwxtyOEGyM

# Invalid ID (exit 1)
./print_youtube_transcript.php 'https://www.youtube.com/watch?v=FTwxtyOEGyM'
```

---

## Output

* **One line** of plain text, terminated by a single `\n`.
* No timestamps, captions numbering, or speaker annotations.
* No HTML/XML tags, entities, or JSON artifacts; entities are decoded.
* Whitespace inside each caption snippet is collapsed to single spaces and
  trimmed.
* Snippets are joined with a single space. Empty snippets are retained during
  concatenation (to preserve the same spacing as `youtube-transcript-api`), so
  occasional double spaces can appear in the final string.
* The final string is `.trim()`-med and written with `echo`, so the only
  trailing newline is the one added by `echo $transcript . "\n"`.

Typical output (truncated):

```text
What's up, guys? We're here in Japan, and in this video, we're going to show you the best places to visit for your first time in the country. ...
```

(Shown wrapped here for readability; the actual output is a single line.)

---

## How it works

```text
argv
 │
 ├─ argument parsing / validation                        main()
 │
 └─ fetchTranscript(videoId)                    [throws RuntimeException]
     │
     ├─ 1. fetchCaptionTracks(videoId)
     │     ├─ GET /watch?v=… (HTML, CONSENT cookies)
     │     │    └─ scrape INNERTUBE_API_KEY
     │     ├─ fetchPlayerViaInnertube(videoId, apiKey)
     │     │    ├─ POST /youtubei/v1/player  ANDROID 20.10.38
     │     │    ├─ POST /youtubei/v1/player  ANDROID 19.44.38
     │     │    ├─ POST /youtubei/v1/player  IOS 19.45.4
     │     │    └─ POST /youtubei/v1/player  WEB 2.20240726.00.00
     │     └─ fallback: extractPlayerResponse(watch HTML)
     │          └─ ytInitialPlayerResponse → captionTracks
     │
     ├─ 2. selectEnglishTrack(tracks)
     │     └─ manual en → generated en → en-US → en-GB → any "en*"
     │
     ├─ 3. fetchSnippets(track)
     │     ├─ strip "&fmt=srv3" from baseUrl
     │     ├─ try baseUrl, then +fmt=json3, +fmt=srv3, +fmt=srv1
     │     └─ parseJson3Transcript() or parseXmlTranscript()
     │
     └─ 4. normalize whitespace, join with spaces, trim
```

### Why the InnerTube call comes first

The comments in the source explain the design: modern WEB watch pages embed
caption URLs that are often served as **empty** responses (they expect a
PoToken). The Android InnerTube client used by `youtube-transcript-api` returns
caption URLs that still work without one. The script therefore asks InnerTube
first and only falls back to whatever the watch page embedded.

### Caption-track shape

Tracks come from:

```text
captions.playerCaptionsTracklistRenderer.captionTracks[]
```

Each track is an associative array. The script relies on three keys:

| Key | Use |
|---|---|
| `languageCode` | Case-insensitive match against `en`, `en-US`, `en-GB`, or prefix `en` |
| `kind` | `"asr"` marks auto-generated captions; anything else is treated as manual |
| `baseUrl` | Caption download URL; `&fmt=srv3` is stripped before use |

All other fields (name, vssId, isTranslatable, …) are ignored.

---

## Function reference

All functions live in the **global namespace** and are declared before use.
They are all pure-ish helpers except those that perform HTTP I/O.

| Function | Signature | Returns |
|---|---|---|
| `httpRequest` | `(string $method, string $url, ?string $body, array $headers = [], int $timeout = 30): array` | `{status:int, body:string, error:string}` |
| `extractJsonValue` | `(string $source, int $start): ?string` | Balanced JSON substring or `null` |
| `extractPlayerResponse` | `(string $html): ?array` | Decoded `ytInitialPlayerResponse` or `null` |
| `captionTracksOf` | `(array $playerResponse): array` | Re-indexed list of caption-track arrays |
| `fetchPlayerViaInnertube` | `(string $videoId, ?string $apiKey = null): ?array` | Player response with captions, or `null` |
| `fetchCaptionTracks` | `(string $videoId): array` | List of caption tracks (possibly empty) |
| `selectEnglishTrack` | `(array $tracks): ?array` | Best English track or `null` |
| `parseJson3Transcript` | `(string $json): array` | List of snippet strings |
| `parseXmlTranscript` | `(string $xml): array` | List of snippet strings |
| `fetchSnippets` | `(array $track): array` | List of snippet strings |
| `fetchTranscript` | `(string $videoId): string` | Normalized transcript; throws `RuntimeException` |
| `printUsage` | `($stream = STDOUT): void` | Writes usage text; no return |
| `main` | `(array $argv): int` | Process exit code |

---

### `httpRequest()`

```php
function httpRequest(
    string $method,
    string $url,
    ?string $body,
    array $headers = [],
    int $timeout = 30
): array
```

A small cURL wrapper used for every network call.

* Automatically adds `User-Agent` (the browser UA in `USER_AGENT`) unless the
  caller supplied one, and `Accept-Language: en-US,en;q=0.9` unless supplied.
  Header matching is case-insensitive on the header *name* prefix.
* Follows up to **5** redirects.
* Connect timeout **20 s**, total timeout **`$timeout`** (default **30 s**).
* Accepts gzip/deflate (`CURLOPT_ENCODING => ''`), so decompression is
  automatic.
* Uses `CURLOPT_CUSTOMREQUEST` for the method, and sets `CURLOPT_POSTFIELDS`
  only when `$body !== null`.
* Returns `status => 0` and a non-empty `error` when cURL initialization or
  execution fails.
* The returned `body` is cast to string; `error` is `''` on any HTTP response,
  including 4xx/5xx (check `status` instead).

Return shape:

```php
['status' => 200, 'body' => '...', 'error' => '']
```

There is no retry, backoff, body-size limit, or HTTP status exception.

---

### `extractJsonValue()`

```php
function extractJsonValue(string $source, int $start): ?string
```

Scans forward from `$start` for the first `{` or `[`, then walks the string
tracking:

* nesting depth for `{}` / `[]`,
* string state (`"`),
* backslash escapes.

Returns the substring from the opening bracket through its matching closing
bracket, or `null` if:

* no opening bracket exists at/after `$start`,
* the input ends before balance is restored,
* an unmatched closing bracket drives depth below zero.

This is needed because `ytInitialPlayerResponse` is embedded as a JavaScript
assignment, not a separate JSON document, so a plain `json_decode()` of the
page would fail.

---

### `extractPlayerResponse()`

```php
function extractPlayerResponse(string $html): ?array
```

* Finds `ytInitialPlayerResponse\s*=\s*` with `preg_match(..., PREG_OFFSET_CAPTURE)`.
* Extracts the JSON value via `extractJsonValue()`.
* Returns the decoded array, or `null` on regex miss, unbalanced JSON, or
  non-array JSON.

This is the **fallback** source of caption tracks when InnerTube fails.

---

### `captionTracksOf()`

```php
function captionTracksOf(array $playerResponse): array
```

Safely reads
`captions.playerCaptionsTracklistRenderer.captionTracks`.
Non-array entries are filtered out and keys are re-indexed with
`array_values()`. Missing or malformed paths yield `[]`.

Any player response with `playabilityStatus: ERROR` (e.g. private, deleted, or
age-restricted video) typically has no captions, so this returns `[]`.

---

### `fetchPlayerViaInnertube()`

```php
function fetchPlayerViaInnertube(string $videoId, ?string $apiKey = null): ?array
```

POSTs to `https://www.youtube.com/youtubei/v1/player`:

* With an API key: `?key=<urlencoded key>`
* Without one: `?prettyPrint=false`

The request body is:

```json
{
  "context": { "client": { "clientName": "...", "clientVersion": "...", ... } },
  "videoId": "<id>"
}
```

`json_encode()` uses `JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE`.

Client attempts, in order:

| # | Client | Version | Extra context | Extra headers |
|---|---|---|---|---|
| 1 | `ANDROID` | `20.10.38` | — | none (browser UA added by `httpRequest`) |
| 2 | `ANDROID` | `19.44.38` | `androidSdkVersion=30`, `hl=en`, `gl=US` | `X-YouTube-Client-Name: 3`, `X-YouTube-Client-Version`, Android app UA |
| 3 | `IOS` | `19.45.4` | `deviceModel=iPhone16,2`, `hl=en`, `gl=US` | `X-YouTube-Client-Name: 5`, `X-YouTube-Client-Version`, iOS app UA |
| 4 | `WEB` | `2.20240726.00.00` | `hl=en`, `gl=US` | `X-YouTube-Client-Name: 1`, `X-YouTube-Client-Version` |

Every attempt also sends:

```
Content-Type: application/json
Origin:  https://www.youtube.com
Referer: https://www.youtube.com/watch?v=<id>
```

The first attempt whose status is `2xx` **and** whose decoded body contains at
least one caption track is returned. All other outcomes (non-2xx, invalid
JSON, no tracks) continue to the next client. Returns `null` if every attempt
fails.

> Client versions are pinned deliberately. YouTube periodically changes which
> clients are allowed to return caption URLs without a PoToken, so these may
> need updating if fetching starts failing across all videos.

---

### `fetchCaptionTracks()`

```php
function fetchCaptionTracks(string $videoId): array
```

High-level track discovery:

1. **GET** the watch page:

   ```text
   https://www.youtube.com/watch?v=<id>&hl=en&gl=US&has_verified=1&bpctr=9999999999
   ```

   with headers:

   ```text
   Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8
   Cookie: CONSENT=YES+cb; SOCS=CAI
   ```

   `has_verified=1` and `bpctr=9999999999` are used to bypass some
   consent/age gates; the cookies prevent the EU consent interstitial.

2. If the watch page returns `200`, scrape `"INNERTUBE_API_KEY": "..."` from
   the HTML.

3. Call `fetchPlayerViaInnertube($videoId, $apiKey)`. If it yields tracks,
   **return them immediately** — the watch page's own caption URLs are not
   used.

4. Otherwise, parse `ytInitialPlayerResponse` from the same HTML via
   `extractPlayerResponse()` and return its tracks if any.

5. Return `[]` if nothing worked.

Note that the watch page is fetched even if it ultimately is not needed, to
obtain the InnerTube API key and as a fallback source.

---

### `selectEnglishTrack()`

```php
function selectEnglishTrack(array $tracks): ?array
```

Returns the preferred English caption track, or `null` if none qualify.
Matching is case-insensitive on `languageCode`. See
[Caption selection algorithm](#caption-selection-algorithm) for the exact
order.

---

### `parseJson3Transcript()`

```php
function parseJson3Transcript(string $json): array
```

Parses YouTube's `json3` caption format:

```json
{ "events": [ { "segs": [ { "utf8": "Hello " }, { "utf8": "world" } ], ... } ] }
```

* Events without a non-empty `segs` array are skipped.
* The `utf8` values of each event's `segs` are concatenated into one snippet.
* Snippets **are** emitted even if the concatenation is empty (spacing
  preservation).
* Returns `[]` if the top-level JSON is not an array.

---

### `parseXmlTranscript()`

```php
function parseXmlTranscript(string $xml): array
```

Parses both XML caption dialects:

* **`srv1`:** `<transcript><text start="…" dur="…">…</text>…</transcript>`
* **`srv3`:** `<transcript><body><p t="…" d="…"><s>word</s>…</p>…</body></transcript>`

Behavior:

1. Loads with `DOMDocument::loadXML()` using
   `LIBXML_NONET | LIBXML_NOERROR | LIBXML_NOWARNING`. External entity/network
   loading is disabled by `LIBXML_NONET`.
2. libxml errors are captured and suppressed, then the previous
   `libxml_use_internal_errors()` state is restored.
3. If parsing fails, returns `[]`.
4. Queries `//text` first. If any such nodes exist, returns their non-empty
   `textContent` values and stops.
5. Otherwise queries `//p` (srv3) and returns non-empty `textContent` values.
6. Each value is passed through
   `html_entity_decode(..., ENT_QUOTES | ENT_HTML5, 'UTF-8')` to handle
   double-escaped caption text such as `What&amp;#39;s up`.

The `//text`-first, `//p`-second strategy means a document containing both
node types is treated as `srv1`.

---

### `fetchSnippets()`

```php
function fetchSnippets(array $track): array
```

Downloads and parses the caption payload for one track.

* Removes the literal substring `&fmt=srv3` from `baseUrl` (matching
  `youtube-transcript-api`). If `baseUrl` is missing/empty, returns `[]`.
* Tries, in order:
  1. the stripped `baseUrl` as-is,
  2. `baseUrl` + `&fmt=json3` (or `?fmt=json3` if no `?` is present),
  3. `baseUrl` + `&fmt=srv3`,
  4. `baseUrl` + `&fmt=srv1`.
* Each request sends `Accept: */*` and `Referer: https://www.youtube.com/`.
* Non-2xx responses and blank bodies are skipped.
* Payloads whose trimmed body starts with `{` go to
  `parseJson3Transcript()`; everything else goes to
  `parseXmlTranscript()`.
* A response is accepted only if at least one snippet contains non-whitespace
  text. Empty entries inside an accepted response are preserved.
* Returns `[]` if every URL fails or yields no usable text.

---

### `fetchTranscript()`

```php
function fetchTranscript(string $videoId): string
```

The main orchestration function. Throws `RuntimeException` when:

| Condition | Message |
|---|---|
| No caption tracks found | `No English transcript available for <id>` |
| No track passes English selection | `No English transcript available for <id>` |
| Snippets empty after download/parsing | `Transcript for <id> is empty` |
| Text empty after normalization | `Transcript for <id> is empty` |

Normalization for each snippet:

```php
preg_replace('/[\s\p{Z}]+/u', ' ', $snippet)  // then trim()
```

`\p{Z}` ensures Unicode separator characters (NBSP, etc.) collapse as well.
Snippets are joined with a single space, then the whole string is trimmed.

This function performs all network I/O for a successful run and is the most
convenient entry point for programmatic use.

---

### `printUsage()`

```php
function printUsage($stream = STDOUT): void
```

Writes the help text to the given stream. Used with `STDOUT` for `-h` and with
`STDERR` for usage errors. Note that the parameter is untyped intentionally —
it accepts any writable stream resource.

---

### `main()`

```php
function main(array $argv): int
```

Parses arguments and executes the workflow:

1. Drops `$argv[0]`.
2. Applies the leading-dash workaround described above.
3. Iterates arguments:
   * `--` → stop option processing.
   * `-h` / `--help` → `printUsage()`, return `0`.
   * other `-…` option → error + usage to stderr, return `2`.
   * everything else → positional list.
4. Requires exactly one positional; otherwise usage to stderr, return `2`.
5. Trims the ID and validates `^[A-Za-z0-9_-]+$`; otherwise
   `Invalid YouTube video hash: '<value>'` to stderr, return `1`.
6. Calls `fetchTranscript()`, catching `Throwable` and printing
   `Error: <message>` to stderr with return `1`.
7. Prints the transcript plus `\n` and returns `0`.

The entry point at the bottom of the file runs `main()` only when **all** of
these are true:

```php
PHP_SAPI === 'cli' && isset($argv) && realpath($argv[0]) === realpath(__FILE__)
```

This prevents execution on web requests and when the file is `require`d or
`include`d from another CLI script.

---

## Caption selection algorithm

`selectEnglishTrack()` mirrors `youtube-transcript-api`'s
`TranscriptList.find_transcript()` preference order. Tracks are classified as
**manual** (no `kind`, or `kind !== "asr"`) or **generated**
(`kind === "asr"`).

Exact-match phase, in this priority order:

| Priority | Language | Track type |
|---:|---|---|
| 1 | `en` | manual |
| 2 | `en` | generated |
| 3 | `en-US` | manual |
| 4 | `en-US` | generated |
| 5 | `en-GB` | manual |
| 6 | `en-GB` | generated |

If none match exactly, the fallback phase selects the first track whose
`languageCode` starts with `en` — manual tracks first, then generated tracks
(so any manual English variant such as `en-AU` beats an ASR `en` track).

If neither phase matches, `null` is returned and the caller raises
`No English transcript available for <id>`.

Consequences worth knowing:

* An auto-generated `en` track beats a manual `en-US` track (priority 2 > 3),
  matching the Python library's per-language loop.
* Manual tracks always win ties within the same language.
* Only the first matching track in the list is used; no quality comparison is
  made.

---

## Supported caption formats

| Format | Detected by | Parser | Notes |
|---|---|---|---|
| `json3` | body starts with `{` | `parseJson3Transcript()` | Preferred; structured, no XML entity issues |
| `srv3` | XML `<p><s>…</s></p>` | `parseXmlTranscript()` | Falls back to `//p` when no `//text` nodes |
| `srv1` | XML `<text>…</text>` | `parseXmlTranscript()` | Queried first via `//text` |
| unknown/HTML error page | anything else | `parseXmlTranscript()` | Usually fails to parse and yields `[]`, so the next URL is tried |

The first format that yields any non-whitespace text wins; formats later in the
list are not attempted after that.

---

## HTTP requests made

A successful run typically makes **3** requests:

1. `GET` the watch page (for the InnerTube key and as fallback).
2. `POST` `/youtubei/v1/player` (usually the first Android client succeeds).
3. `GET` one caption URL (`json3` typically succeeds first).

A failing run can make many more:

* up to 4 InnerTube attempts,
* the embedded-player fallback (no extra request),
* up to 4 caption-format URLs.

All requests use HTTPS on `www.youtube.com`; no other hosts are contacted.

### Worst-case latency

Each request has a 30 s total timeout and 20 s connect timeout. With no
retries but many sequential attempts, a worst-case run can take roughly
**4–5 minutes** before failing. There is no CLI option to lower this; edit the
`httpRequest()` default or its call sites if you need a shorter budget.

### Request headers at a glance

| Call | Headers |
|---|---|
| Watch page | Browser UA, `Accept-Language`, HTML `Accept`, `Cookie: CONSENT=YES+cb; SOCS=CAI` |
| InnerTube | `Content-Type: application/json`, browser UA, `Accept-Language`, `Origin`, `Referer`, client-specific headers |
| Caption URL | Browser UA, `Accept-Language`, `Accept: */*`, `Referer: https://www.youtube.com/` |

---

## Error handling

* Network failures are surfaced as `status => 0` with a cURL error string; the
  caption code treats them the same as a non-2xx HTTP response and moves on.
* Parsing failures are non-fatal: they yield empty arrays so the next
  candidate URL/client can be tried.
* Only the terminal conditions in `fetchTranscript()` throw; `main()` catches
  every `Throwable` (including missing-extension `Error`s such as
  `Class "DOMDocument" not found`).
* Nothing is written to stdout until the complete transcript is available, so
  `> file` never leaves partial transcript output on failure.

---

## Relationship to other scripts

| File | Relationship |
|---|---|
| `fetch_youtube_transcript.php` | Shares the same caption-fetching function names and logic, but additionally summarizes with Ollama (`MODEL = 'ornith-1.5:35b'`) and writes `./.chat/youtube_transcripts/<id>.transcript` and `<id>.title`. |
| `fetch_youtube_transcript.py` | The original Python implementation this logic mirrors. |
| `.chat/youtube_transcripts/` | Populated by `fetch_youtube_transcript.php`; **not** used or written by this script. |
| `search_youtube.py`, `get_videos_in_playlist.py` | Can supply video IDs to feed into this script. |

Because `print_youtube_transcript.php` and `fetch_youtube_transcript.php`
declare the **same global function names**, they cannot be `require`d into the
same PHP process. Use one or the other per process, or copy the functions into
a namespaced/class-based module if you need both.

---

## Using it as a library

The CLI guard means `require`/`include` does not execute `main()`, so the
helpers are reusable from another PHP CLI script:

```php
<?php
require __DIR__ . '/print_youtube_transcript.php';

try {
    $text = fetchTranscript('FTwxtyOEGyM');
    echo strlen($text), " characters\n";
} catch (Throwable $e) {
    fwrite(STDERR, $e->getMessage() . "\n");
}
```

Useful lower-level entry points:

```php
$tracks = fetchCaptionTracks('FTwxtyOEGyM');   // list of track arrays
$track  = selectEnglishTrack($tracks);         // chosen track
$parts  = fetchSnippets($track);               // list of raw snippets
$text   = fetchTranscript('FTwxtyOEGyM');      // normalized single string
```

Remember the global-namespace collision caveat above.

---

## Limitations and caveats

* **YouTube changes break this.** Caption availability depends on undocumented
  InnerTube clients and private API shapes. Pinned client versions and
  endpoints may need updating.
* **English only.** There is no parameter to request another language, even
  though YouTube may expose non-English tracks.
* **No URL parsing.** Bare IDs only. Extract the ID yourself, e.g.:

  ```bash
  id=$(printf '%s' 'https://www.youtube.com/watch?v=FTwxtyOEGyM' | sed -E 's/.*v=([A-Za-z0-9_-]+).*/\1/')
  ./print_youtube_transcript.php "$id"
  ```

* **No pagination/streaming.** The full transcript is buffered in memory; there
  is no size cap on HTTP responses.
* **No timestamps or structure.** Output is flattened text; segment boundaries
  and timing are discarded.
* **Occasional double spaces.** Empty caption snippets are intentionally kept
  during the join, so the final text can contain repeated spaces.
* **Consent and age gates are best-effort.** `has_verified=1`, `bpctr`, and the
  `CONSENT`/`SOCS` cookies help, but geo-restricted or sign-in-required videos
  may still fail.
* **No retry/backoff on 429/5xx.** It simply tries the next client/URL.
* **No proxy/auth support** beyond what cURL picks up implicitly.
* **Global functions.** Not namespaced or class-based; unsuitable for direct
  combination with `fetch_youtube_transcript.php`.
* **No output file option.** Redirect stdout if you need a file.
* **No tests in the repository** for this script; behavior is verified by
  manual runs.

---

## Troubleshooting

| Symptom | Likely cause | What to try |
|---|---|---|
| `Error: No English transcript available for <id>` | Video has no captions, captions are non-English, or YouTube blocked all InnerTube clients | Open the video in a browser to confirm captions exist; retry later; update pinned client versions |
| `Error: Transcript for <id> is empty` | Caption URLs returned empty payloads (PoToken requirement) | Retry; the script already tries json3/srv3/srv1 and four clients |
| `Error: Class "DOMDocument" not found` | `ext-dom` missing | Install/enable `php-dom` |
| `Error: Call to undefined function curl_init()` | `ext-curl` missing | Install/enable `php-curl` |
| `Unknown option: --verbose` (exit 2) | Unsupported flag | Use `--help`; there are no other options |
| `Invalid YouTube video hash: '…'` (exit 1) | Passed a URL or malformed ID | Pass only the bare video ID |
| Hangs for minutes | Sequential 30 s timeouts on failing attempts | Check network access to `youtube.com`; shorten `httpRequest()` timeout locally |
| Works for some videos but not others | Caption availability/format varies per video | Confirm manually; report as a YouTube-side change if widespread |

---

## Verification notes

Checked against the current script and PHP 8.4.13 in this repository:

```bash
$ php -v | head -1
PHP 8.4.13 (cli) (built: Sep 23 2025 14:14:32) (NTS)

$ php -m | grep -E '^(curl|dom|libxml)$'
curl
dom
libxml

$ php print_youtube_transcript.php --help >/dev/null; echo $?
0

$ php print_youtube_transcript.php >/dev/null 2>&1; echo $?
2

$ php print_youtube_transcript.php FTwxtyOEGyM | head -c 100
What's up, guys? We're here in Japan, and in this video, we're going to show you the best places to
$ echo ${PIPESTATUS[0]}
0
```

Manual regression checks that are useful after edits:

1. Help exits `0` on stdout; missing args exit `2` on stderr.
2. `-- -PcBFgkrR2o` reaches fetch logic instead of erroring as an option.
3. A known-captioned video returns non-empty output and exit `0`.
4. An invalid ID exits `1` without touching stdout.
5. `require`-ing the file from another CLI script does not run `main()`.
