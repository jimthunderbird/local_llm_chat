#question: Can you show how to call yt-dlp or another backend tool from Rust CLI to get the transcript reliably?

#answer:
Using yt-dlp as a backend is the most reliable approach because it actively maintains YouTube bot-detection workarounds, player tokens, and dynamic signature generation.

The recommended strategy is to call yt-dlp to fetch subtitles to an isolated temporary directory using --skip-download, --write-subs, and --write-auto-subs, then clean and parse the resulting VTT/SRT file in Rust
