#!/usr/bin/env bash
# Handle image message: download and return text content for Claude
# Usage: handle_image.sh <message_id> <content_json> <jarvis_dir>
# Output: text content to stdout (for Claude to process)
# Exit 0 on success, non-zero on failure (caller should fallback gracefully)

message_id="$1"
content_json="$2"
jarvis_dir="$3"

[ -z "$message_id" ] || [ -z "$content_json" ] || [ -z "$jarvis_dir" ] && exit 1

image_key=$(echo "$content_json" | jq -r '.image_key // empty' 2>/dev/null)
[ -z "$image_key" ] && { echo "[用户发送了一张图片，但无法解析 image_key]"; exit 0; }

img_dir="$jarvis_dir/tmp/images"
mkdir -p "$img_dir"
img_path="$img_dir/${image_key}.png"

lark-cli im +messages-resources-download \
  --message-id "$message_id" \
  --file-key "$image_key" \
  --type image \
  --output "$img_path" \
  --as bot 2>/dev/null

if [ -f "$img_path" ]; then
  echo "[用户发送了一张图片，已保存到 $img_path，请用 Read 工具查看图片内容并回复]"
else
  echo "[用户发送了一张图片，但下载失败 (image_key: $image_key)]"
  exit 1
fi
