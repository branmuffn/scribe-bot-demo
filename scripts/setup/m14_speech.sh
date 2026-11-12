#!/bin/bash
# Download the M0b speech models to the M14 and set up the benchmark environment. Safe to re-run.
# Run from the M4:  scripts/m14.sh 'bash -s' < scripts/setup/m14_speech.sh
#
# Models live outside the data folder (they are not private and need no backup).
# Audio conversion uses macOS's built-in afconvert, so no ffmpeg is needed.
set -euo pipefail

MODELS="$HOME/scribe-bot-models"
REL=https://github.com/k2-fsa/sherpa-onnx/releases/download
mkdir -p "$MODELS"

# release tag | file | sha256 (pinned 2026-10-18). Alternatives tested in M0 and dropped: CAM++, WeSpeaker ResNet34, TitaNet large.
while IFS='|' read -r tag file sha; do
  dest="$MODELS/$file"
  if [ ! -f "$dest.ok" ]; then
    echo "Downloading $file ..."
    curl -fsSL -o "$dest" "$REL/$tag/$file"
    got=$(shasum -a 256 "$dest" | awk '{print $1}')
    [ "$got" = "$sha" ] || { echo "Checksum mismatch for $file" >&2; rm -f "$dest"; exit 1; }
    case "$file" in
      *.tar.bz2) tar -xjf "$dest" -C "$MODELS" && rm "$dest" ;;
    esac
    touch "$dest.ok"
  fi
done <<'EOF'
asr-models|sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8.tar.bz2|157c157bc51155e03e37d2466522a3a737dd9c72bb25f36eb18912964161e1ad
asr-models|silero_vad.onnx|9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6
speaker-segmentation-models|sherpa-onnx-pyannote-segmentation-3-0.tar.bz2|24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488
speaker-recongition-models|nemo_en_titanet_small.onnx|ad4a1802485d8b34c722d2a9d04249662f2ece5d28a7a039063ca22f515a789e
EOF

# Benchmark Python environment (sherpa-onnx + numpy), pinned in benchmarks/pyproject.toml
cd "$HOME/scribe-bot/benchmarks"
"$HOME/.local/bin/uv" sync --quiet
"$HOME/.local/bin/uv" run python -c "import sherpa_onnx; print('sherpa-onnx', sherpa_onnx.__version__)"

mkdir -p "$HOME/scribe-bot-data/m0"
du -sh "$MODELS"
