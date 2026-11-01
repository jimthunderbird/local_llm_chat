#!/usr/bin/env php
<?php

declare(strict_types=1);

/**
 * Fetch an English YouTube transcript and print only the first 100 characters.
 *
 * Usage:
 *     php get_youtube_transcript_100.php <youtube_video_hash>
 */

const USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    . 'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36';

const MAX_CHARS = 1000;

function httpRequest(
    string $method,
    string $url,
    ?string $body,
    array $headers = [],
    int $timeout = 30
): array {
    $hasUserAgent = false;
    $hasAcceptLanguage = false;
    foreach ($headers as $header) {
        if (stripos($header, 'user-agent:') === 0) {
            $hasUserAgent = true;
        } elseif (stripos($header, 'accept-language:') === 0) {
            $hasAcceptLanguage = true;
        }
    }
    if (!$hasUserAgent) {
        $headers[] = 'User-Agent: ' . USER_AGENT;
    }
    if (!$hasAcceptLanguage) {
        $headers[] = 'Accept-Language: en-US,en;q=0.9';
    }

    $curl = curl_init($url);
    if ($curl === false) {
        return ['status' => 0, 'body' => '', 'error' => 'could not initialize cURL'];
    }

    curl_setopt_array($curl, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_FOLLOWLOCATION => true,
        CURLOPT_MAXREDIRS => 5,
        CURLOPT_CONNECTTIMEOUT => 20,
        CURLOPT_TIMEOUT => $timeout,
        CURLOPT_ENCODING => '',
        CURLOPT_HTTPHEADER => $headers,
        CURLOPT_CUSTOMREQUEST => $method,
    ]);
    if ($body !== null) {
        curl_setopt($curl, CURLOPT_POSTFIELDS, $body);
    }

    $responseBody = curl_exec($curl);
    $error = curl_error($curl);
    $status = (int) curl_getinfo($curl, CURLINFO_RESPONSE_CODE);
    unset($curl);

    if ($responseBody === false) {
        return [
            'status' => 0,
            'body' => '',
            'error' => $error !== '' ? $error : 'request failed',
        ];
    }

    return ['status' => $status, 'body' => (string) $responseBody, 'error' => ''];
}

function extractJsonValue(string $source, int $start): ?string
{
    $length = strlen($source);
    $index = $start;
    while ($index < $length && $source[$index] !== '{' && $source[$index] !== '[') {
        $index++;
    }
    if ($index >= $length) {
        return null;
    }

    $depth = 0;
    $inString = false;
    $escaped = false;

    for ($cursor = $index; $cursor < $length; $cursor++) {
        $char = $source[$cursor];

        if ($inString) {
            if ($escaped) {
                $escaped = false;
            } elseif ($char === '\\') {
                $escaped = true;
            } elseif ($char === '"') {
                $inString = false;
            }
            continue;
        }

        if ($char === '"') {
            $inString = true;
        } elseif ($char === '{' || $char === '[') {
            $depth++;
        } elseif ($char === '}' || $char === ']') {
            $depth--;
            if ($depth === 0) {
                return substr($source, $index, $cursor - $index + 1);
            }
            if ($depth < 0) {
                return null;
            }
        }
    }

    return null;
}

function extractPlayerResponse(string $html): ?array
{
    if (!preg_match('/ytInitialPlayerResponse\s*=\s*/', $html, $match, PREG_OFFSET_CAPTURE)) {
        return null;
    }

    $start = $match[0][1] + strlen($match[0][0]);
    $json = extractJsonValue($html, $start);
    if ($json === null) {
        return null;
    }

    $decoded = json_decode($json, true);
    return is_array($decoded) ? $decoded : null;
}

function captionTracksOf(array $playerResponse): array
{
    $tracks = $playerResponse['captions']['playerCaptionsTracklistRenderer']['captionTracks'] ?? [];
    if (!is_array($tracks)) {
        return [];
    }

    return array_values(array_filter($tracks, 'is_array'));
}

function fetchPlayerViaInnertube(string $videoId, ?string $apiKey = null): ?array
{
    $endpoint = 'https://www.youtube.com/youtubei/v1/player';
    if ($apiKey !== null && $apiKey !== '') {
        $endpoint .= '?key=' . rawurlencode($apiKey);
    } else {
        $endpoint .= '?prettyPrint=false';
    }

    $clients = [
        [
            'context' => [
                'client' => [
                    'clientName' => 'ANDROID',
                    'clientVersion' => '20.10.38',
                ],
            ],
            'headers' => [],
        ],
        [
            'context' => [
                'client' => [
                    'clientName' => 'ANDROID',
                    'clientVersion' => '19.44.38',
                    'androidSdkVersion' => 30,
                    'hl' => 'en',
                    'gl' => 'US',
                ],
            ],
            'headers' => [
                'X-YouTube-Client-Name: 3',
                'X-YouTube-Client-Version: 19.44.38',
                'User-Agent: com.google.android.youtube/19.44.38 (Linux; U; Android 11) gzip',
            ],
        ],
        [
            'context' => [
                'client' => [
                    'clientName' => 'IOS',
                    'clientVersion' => '19.45.4',
                    'deviceModel' => 'iPhone16,2',
                    'hl' => 'en',
                    'gl' => 'US',
                ],
            ],
            'headers' => [
                'X-YouTube-Client-Name: 5',
                'X-YouTube-Client-Version: 19.45.4',
                'User-Agent: com.google.ios.youtube/19.45.4 (iPhone16,2; U; CPU iOS 18_1_0 like Mac OS X;)',
            ],
        ],
        [
            'context' => [
                'client' => [
                    'clientName' => 'WEB',
                    'clientVersion' => '2.20240726.00.00',
                    'hl' => 'en',
                    'gl' => 'US',
                ],
            ],
            'headers' => [
                'X-YouTube-Client-Name: 1',
                'X-YouTube-Client-Version: 2.20240726.00.00',
            ],
        ],
    ];

    foreach ($clients as $client) {
        $payload = [
            'context' => $client['context'],
            'videoId' => $videoId,
        ];
        $headers = array_merge(
            ['Content-Type: application/json'],
            $client['headers'],
            [
                'Origin: https://www.youtube.com',
                'Referer: https://www.youtube.com/watch?v=' . rawurlencode($videoId),
            ]
        );

        $response = httpRequest(
            'POST',
            $endpoint,
            json_encode($payload, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE),
            $headers
        );

        if ($response['status'] < 200 || $response['status'] >= 300) {
            continue;
        }

        $decoded = json_decode($response['body'], true);
        if (is_array($decoded) && captionTracksOf($decoded) !== []) {
            return $decoded;
        }
    }

    return null;
}

function fetchCaptionTracks(string $videoId): array
{
    $watchResponse = httpRequest(
        'GET',
        'https://www.youtube.com/watch?v=' . rawurlencode($videoId)
            . '&hl=en&gl=US&has_verified=1&bpctr=9999999999',
        null,
        [
            'Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Cookie: CONSENT=YES+cb; SOCS=CAI',
        ]
    );
    $html = $watchResponse['status'] === 200 ? $watchResponse['body'] : '';

    $apiKey = null;
    if ($html !== '' && preg_match('/"INNERTUBE_API_KEY":\s*"([a-zA-Z0-9_-]+)"/', $html, $match)) {
        $apiKey = $match[1];
    }

    $player = fetchPlayerViaInnertube($videoId, $apiKey);
    if ($player !== null) {
        $tracks = captionTracksOf($player);
        if ($tracks !== []) {
            return $tracks;
        }
    }

    if ($html !== '') {
        $player = extractPlayerResponse($html);
        if ($player !== null) {
            $tracks = captionTracksOf($player);
            if ($tracks !== []) {
                return $tracks;
            }
        }
    }

    return [];
}

function selectEnglishTrack(array $tracks): ?array
{
    $preferences = ['en', 'en-US', 'en-GB'];

    foreach ($preferences as $preference) {
        $wanted = strtolower($preference);
        foreach ([false, true] as $wantsGenerated) {
            foreach ($tracks as $track) {
                $isGenerated = (($track['kind'] ?? '') === 'asr');
                if ($isGenerated !== $wantsGenerated) {
                    continue;
                }
                $code = strtolower((string) ($track['languageCode'] ?? ''));
                if ($code === $wanted) {
                    return $track;
                }
            }
        }
    }

    foreach ([false, true] as $wantsGenerated) {
        foreach ($tracks as $track) {
            $isGenerated = (($track['kind'] ?? '') === 'asr');
            if ($isGenerated !== $wantsGenerated) {
                continue;
            }
            $code = strtolower((string) ($track['languageCode'] ?? ''));
            if (str_starts_with($code, 'en')) {
                return $track;
            }
        }
    }

    return null;
}

function parseJson3Transcript(string $json): array
{
    $decoded = json_decode($json, true);
    if (!is_array($decoded)) {
        return [];
    }

    $snippets = [];
    foreach ($decoded['events'] ?? [] as $event) {
        if (!is_array($event) || empty($event['segs']) || !is_array($event['segs'])) {
            continue;
        }
        $chunk = '';
        foreach ($event['segs'] as $segment) {
            if (is_array($segment) && isset($segment['utf8'])) {
                $chunk .= (string) $segment['utf8'];
            }
        }
        $snippets[] = $chunk;
    }

    return $snippets;
}

function parseXmlTranscript(string $xml): array
{
    $document = new DOMDocument();

    $previous = libxml_use_internal_errors(true);
    $loaded = $document->loadXML($xml, LIBXML_NONET | LIBXML_NOERROR | LIBXML_NOWARNING);
    libxml_clear_errors();
    libxml_use_internal_errors($previous);

    if (!$loaded) {
        return [];
    }

    $xpath = new DOMXPath($document);

    $decode = static fn(string $text): string => html_entity_decode(
        $text,
        ENT_QUOTES | ENT_HTML5,
        'UTF-8'
    );

    $snippets = [];
    $nodes = $xpath->query('//text');
    if ($nodes !== false && $nodes->length > 0) {
        foreach ($nodes as $node) {
            if ($node->textContent !== '') {
                $snippets[] = $decode($node->textContent);
            }
        }
        return $snippets;
    }

    $nodes = $xpath->query('//p');
    if ($nodes !== false) {
        foreach ($nodes as $node) {
            if ($node->textContent !== '') {
                $snippets[] = $decode($node->textContent);
            }
        }
    }

    return $snippets;
}

function fetchSnippets(array $track): array
{
    $baseUrl = str_replace('&fmt=srv3', '', (string) ($track['baseUrl'] ?? ''));
    if ($baseUrl === '') {
        return [];
    }

    $urls = [$baseUrl];
    $separator = str_contains($baseUrl, '?') ? '&' : '?';
    foreach (['json3', 'srv3', 'srv1'] as $format) {
        $urls[] = $baseUrl . $separator . 'fmt=' . $format;
    }

    foreach ($urls as $url) {
        $response = httpRequest('GET', $url, null, [
            'Accept: */*',
            'Referer: https://www.youtube.com/',
        ]);
        if ($response['status'] < 200 || $response['status'] >= 300) {
            continue;
        }

        $body = trim($response['body']);
        if ($body === '') {
            continue;
        }

        $snippets = str_starts_with($body, '{')
            ? parseJson3Transcript($body)
            : parseXmlTranscript($body);

        foreach ($snippets as $snippet) {
            if (trim($snippet) !== '') {
                return $snippets;
            }
        }
    }

    return [];
}

function fetchTranscript(string $videoId): string
{
    $tracks = fetchCaptionTracks($videoId);
    if ($tracks === []) {
        throw new RuntimeException("No English transcript available for {$videoId}");
    }

    $track = selectEnglishTrack($tracks);
    if ($track === null) {
        throw new RuntimeException("No English transcript available for {$videoId}");
    }

    $snippets = fetchSnippets($track);
    if ($snippets === []) {
        throw new RuntimeException("Transcript for {$videoId} is empty");
    }

    $parts = [];
    foreach ($snippets as $snippet) {
        $collapsed = preg_replace('/[\s\p{Z}]+/u', ' ', $snippet);
        $parts[] = trim($collapsed ?? $snippet);
    }

    $text = trim(implode(' ', $parts));
    if ($text === '') {
        throw new RuntimeException("Transcript for {$videoId} is empty");
    }

    return $text;
}

function printUsage($stream = STDOUT): void
{
    fwrite(
        $stream,
        "usage: get_youtube_transcript_100.php [-h] youtube_video_hash\n\n"
        . "Fetch a YouTube transcript and print the first " . MAX_CHARS . " characters to stdout.\n\n"
        . "positional arguments:\n"
        . "  youtube_video_hash  YouTube video ID, e.g. FTwxtyOEGyM\n\n"
        . "options:\n"
        . "  -h, --help          show this help message and exit\n"
    );
}

function main(array $argv): int
{
    $args = array_slice($argv, 1);

    if (
        $args !== []
        && str_starts_with($args[0], '-')
        && !in_array($args[0], ['-h', '--help', '--'], true)
    ) {
        array_unshift($args, '--');
    }

    $positional = [];
    $endOfOptions = false;
    foreach ($args as $arg) {
        if (!$endOfOptions) {
            if ($arg === '--') {
                $endOfOptions = true;
                continue;
            }
            if ($arg === '-h' || $arg === '--help') {
                printUsage();
                return 0;
            }
            if (str_starts_with($arg, '-')) {
                fwrite(STDERR, "Unknown option: {$arg}\n");
                printUsage(STDERR);
                return 2;
            }
        }
        $positional[] = $arg;
    }

    if (count($positional) !== 1) {
        printUsage(STDERR);
        return 2;
    }

    $videoId = trim($positional[0]);
    if (!preg_match('/^[A-Za-z0-9_-]+$/', $videoId)) {
        fwrite(STDERR, "Invalid YouTube video hash: '{$videoId}'\n");
        return 1;
    }

    try {
        $transcript = fetchTranscript($videoId);
    } catch (Throwable $exception) {
        fwrite(STDERR, 'Error: ' . $exception->getMessage() . "\n");
        return 1;
    }

    // Truncate to the first 100 characters.
    $output = mb_substr($transcript, 0, MAX_CHARS, 'UTF-8');
    echo $output . "\n";

    return 0;
}

if (PHP_SAPI === 'cli' && isset($argv) && realpath($argv[0]) === realpath(__FILE__)) {
    exit(main($argv));
}
