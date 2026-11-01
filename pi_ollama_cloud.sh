# Credentials are kept out of version control.
# Store the key in ~/.config/ollama/cloud_api_key (chmod 600) or export
# OLLAMA_CLOUD_API_KEY before running this script.
if [ -z "${OLLAMA_CLOUD_API_KEY:-}" ] && [ -f "$HOME/.config/ollama/cloud_api_key" ]; then
    OLLAMA_CLOUD_API_KEY="$(cat "$HOME/.config/ollama/cloud_api_key")"
fi

if [ -z "${OLLAMA_CLOUD_API_KEY:-}" ]; then
    echo "OLLAMA_CLOUD_API_KEY is not set; add it to ~/.config/ollama/cloud_api_key" >&2
    return 1 2>/dev/null || exit 1
fi

export OLLAMA_DEBUG=1
export OLLAMA_CLOUD_API_KEY
pi --model deepseek-v4.1-flash "run init.prompt"
