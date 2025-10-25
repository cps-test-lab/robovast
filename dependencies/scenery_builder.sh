#!/usr/bin/env bash

# Default values
INPUT_PATH=""
OUTPUT_PATH=""
OTHER_ARGS=()

# Function to insert parameter after a keyword in OTHER_ARGS
# Usage: insert_param_after_keyword "keyword" "param_flag" "param_value"
insert_param_after_keyword() {
    local keyword="$1"
    local param_flag="$2"
    local param_value="$3"
    
    for i in "${!OTHER_ARGS[@]}"; do
        if [[ "${OTHER_ARGS[$i]}" == "$keyword" ]]; then
            idx=$((i+1))
            OTHER_ARGS=( "${OTHER_ARGS[@]:0:$idx}" "$param_flag" "$param_value" "${OTHER_ARGS[@]:$idx}" )
            return 0
        fi
    done
    echo "Could not find keyword '$keyword' in arguments."
    exit 1
}

# Parse command-line options
while [[ $# -gt 0 ]]; do
  key="$1"

  case $key in
    -i)
      INPUT_PATH="$2"
      shift # past argument
      shift # past value
      ;;
    -o)
      OUTPUT_PATH="$2"
      shift # past argument
      shift # past value
      ;;
    *)    # unknown option
      OTHER_ARGS+=("$1") # save it in an array for later
      shift # past argument
      ;;
  esac
done

DOCKER_ARGS=()

# Check if variation or transform is in OTHER_ARGS
HAS_VARIATION=false
HAS_TRANSFORM=false
HAS_GENERATE=false
for arg in "${OTHER_ARGS[@]}"; do
    if [[ "$arg" == "variation" ]]; then
        HAS_VARIATION=true
    elif [[ "$arg" == "transform" ]]; then
        HAS_TRANSFORM=true
    elif [[ "$arg" == "generate" ]]; then
        HAS_GENERATE=true
    fi
done

# Get absolute paths if provided
if [ -n "$INPUT_PATH" ]; then
    if [ ! -e $INPUT_PATH ]; then
        echo "Input path $INPUT_PATH does not exist."
        exit 1 
    fi
    INPUT_PATH=$(readlink -f "$INPUT_PATH")

    # Special handling for variation and transform commands
    if $HAS_VARIATION || $HAS_TRANSFORM; then
        # Extract filename from input path
        if [ ! -f $INPUT_PATH ]; then
            echo "Expected input path $INPUT_PATH to be file."
            exit 1 
        fi
        FILENAME=$(basename "$INPUT_PATH")
        # Get directory path (without filename)
        INPUT_DIR=$(dirname "$INPUT_PATH")
        # Mount the directory
        DOCKER_ARGS+=(-v "$INPUT_DIR":/usr/src/app/models)
        TARGET="/usr/src/app/models/$FILENAME"
        # Insert "-m <target>" immediately after the "variation" or "transform" keyword
        if $HAS_VARIATION; then
            insert_param_after_keyword "variation" "-m" "$TARGET" || OTHER_ARGS+=(-m "$TARGET")
        elif $HAS_TRANSFORM; then
            insert_param_after_keyword "transform" "-m" "$TARGET" || OTHER_ARGS+=(-m "$TARGET")
        fi
    elif $HAS_GENERATE; then
        DOCKER_ARGS+=(-v "$INPUT_PATH":/usr/src/app/models)
        TARGET="/usr/src/app/models/"
        # Insert "-i <target>" immediately after the "generate" keyword
        if ! insert_param_after_keyword "generate" "-i" "$TARGET"; then
            echo "Invalid arguments."
            exit 1
        fi
    fi
fi

if [ -n "$OUTPUT_PATH" ]; then
  OUTPUT_PATH=$(readlink -f "$OUTPUT_PATH")
  # Ensure the output directory exists
  mkdir -p "$OUTPUT_PATH"
  DOCKER_ARGS+=(-v "$OUTPUT_PATH":/usr/src/app/output)
  TARGET="/usr/src/app/output"
  
  # Insert "-o <target>" immediately after the relevant keyword
  if $HAS_VARIATION; then
      insert_param_after_keyword "variation" "-o" "$TARGET" || OTHER_ARGS+=(-o "$TARGET")
  elif $HAS_TRANSFORM; then
      insert_param_after_keyword "transform" "-o" "$TARGET" || OTHER_ARGS+=(-o "$TARGET")
  elif $HAS_GENERATE; then
      insert_param_after_keyword "generate" "-o" "$TARGET" || OTHER_ARGS+=(-o "$TARGET")
  fi
fi
echo docker run "${DOCKER_ARGS[@]}" \
           --user "$(id -u):$(id -g)" \
           --rm --network host -it ghcr.io/secorolab/scenery_builder "${OTHER_ARGS[@]}"
docker run "${DOCKER_ARGS[@]}" \
           --user "$(id -u):$(id -g)" \
           --rm --network host -it ghcr.io/secorolab/scenery_builder "${OTHER_ARGS[@]}"

