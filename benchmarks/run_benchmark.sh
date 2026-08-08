#!/bin/bash
set -e

# Usage: bash run_benchmark.sh /path/to/boltons

if [ -z "$1" ]; then
  echo "Usage: bash run_benchmark.sh /path/to/boltons"
  echo ""
  echo "Example:"
  echo "  git clone https://github.com/mahmoud/boltons.git"
  echo "  bash run_benchmark.sh ./boltons"
  exit 1
fi

BOLTONS_PATH="$1"

if [ ! -d "$BOLTONS_PATH" ]; then
  echo "Error: $BOLTONS_PATH does not exist"
  exit 1
fi

if [ ! -f "$BOLTONS_PATH/boltons/typeutils.py" ]; then
  echo "Error: $BOLTONS_PATH does not look like a boltons repository"
  exit 1
fi

if [ -z "$ANTHROPIC_API_KEY" ]; then
  echo "Error: ANTHROPIC_API_KEY is not set"
  echo "Set it with: export ANTHROPIC_API_KEY=sk-..."
  exit 1
fi

echo "Starting boltons benchmark..."
echo "Boltons path: $BOLTONS_PATH"
echo ""

MODULES=(
  "pathutils"
  "mathutils"
  "queueutils"
  "typeutils"
  "gcutils"
  "namedutils"
  "mboxutils"
)

RESULTS_DIR="$(dirname "$0")/results"
mkdir -p "$RESULTS_DIR"

TOTAL_COST=0

for MODULE in "${MODULES[@]}"; do
  MODULE_PATH="$BOLTONS_PATH/boltons/${MODULE}.py"
  
  if [ ! -f "$MODULE_PATH" ]; then
    echo "Warning: $MODULE_PATH not found, skipping"
    continue
  fi
  
  echo "Benchmarking $MODULE..."
  RESULT_FILE="$RESULTS_DIR/${MODULE}_result.json"
  
  python -m src.cli "$MODULE_PATH" \
    --max-iterations 3 \
    --timeout 60 \
    --sandbox docker \
    --report "$RESULT_FILE" \
    --no-color
  
  echo ""
done

echo "Benchmark complete. Results saved to $RESULTS_DIR"
echo ""
echo "To view results:"
echo "  cat $RESULTS_DIR/*.json"
