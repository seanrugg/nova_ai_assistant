#!/bin/bash
# setup_coral_models.sh — Download Coral TPU models for Nova perception
# Run once after setting up the Coral TPU environment.

MODEL_DIR="$HOME/coral_models"
mkdir -p "$MODEL_DIR"

echo "Downloading SSD MobileNet V2 (Edge TPU compiled)..."
wget -q --show-progress -O "$MODEL_DIR/ssd_mobilenet_v2_coco_quant_postprocess_edgetpu.tflite" \
    "https://github.com/google-coral/test_data/raw/master/ssd_mobilenet_v2_coco_quant_postprocess_edgetpu.tflite"

echo "Downloading SSD MobileNet V2 (CPU fallback)..."
wget -q --show-progress -O "$MODEL_DIR/ssd_mobilenet_v2_coco_quant_postprocess.tflite" \
    "https://github.com/google-coral/test_data/raw/master/ssd_mobilenet_v2_coco_quant_postprocess.tflite"

echo "Downloading COCO labels..."
wget -q --show-progress -O "$MODEL_DIR/coco_labels.txt" \
    "https://github.com/google-coral/test_data/raw/master/coco_labels.txt"

echo ""
echo "Models ready in $MODEL_DIR:"
ls -lh "$MODEL_DIR"
