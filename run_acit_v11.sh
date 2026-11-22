#!/usr/bin/env bash
# Эталон v11 (ACIT: α-аманитин → спорные, +эргостерол и органические
# кислоты): переоценка по хэшам (только ACIT-клетки) и пересборка отчётов
# для RU- и EN-серий.
set -u
cd /home/dp/MyProjects/muhomoring-test

for slug in gemma4-26b gemma4-31b-it-qat huihui-ai-pi-100k medgemma-27b \
            orcarouter-Qwen3.8-27B-Uncensored-latest qwen3.8-latest; do
  echo "=== judge v11 RU $slug · $(date '+%H:%M:%S') ==="
  python3 judge_mush.py "models/$slug/composition" || echo "JUDGE FAILED"
  echo "=== judge v11 EN $slug · $(date '+%H:%M:%S') ==="
  python3 judge_mush.py --ethalon ethalon-en.json "models-en/$slug/composition" \
    || echo "JUDGE FAILED"
done

for slug in gemma4-26b gemma4-31b-it-qat huihui-ai-pi-100k medgemma-27b \
            orcarouter-Qwen3.8-27B-Uncensored-latest qwen3.8-latest; do
  for think in off low medium xhigh; do
    python3 build_mush_report.py --model-dir "models/$slug/composition" \
      --think "$think" >/dev/null 2>&1
    python3 build_mush_report.py --model-dir "models-en/$slug/composition" \
      --think "$think" --ethalon ethalon-en.json \
      --questions questions-composition-en.json \
      --report-name report-en.html >/dev/null 2>&1
  done
done

python3 build_mush_report.py --index --alt-report report-en.html
python3 build_mush_report.py --index --models-root models-en \
  --ethalon ethalon-en.json --questions questions-composition-en.json \
  --report-name report-en.html --alt-report report.html
python3 compare-ru-en.py >/dev/null || echo "COMPARE FAILED"
echo "=== ACIT v11 DONE $(date) ==="
