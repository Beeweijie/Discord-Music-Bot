import json
import re
import os

FILE_PATH = "../config/emoji.json"

# 读取原有表情
if os.path.exists(FILE_PATH):
    with open(FILE_PATH, "r", encoding="utf-8") as f:
        try:
            emojis = json.load(f)
        except json.JSONDecodeError:
            emojis = {}
else:
    emojis = {}

print("💬 输入表情（格式：<:name:id>），输入 'stop' 退出")

while True:
    user_input = input("👉 ").strip()

    if user_input.lower() in {"stop", "exit", "quit"}:
        print("👋 已退出添加")
        break

    match = re.match(r'<:(\w+):(\d+)>', user_input)
    if match:
        name, emoji_id = match.groups()
        emojis[name] = f"<:{name}:{emoji_id}>"

        with open(FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(emojis, f, indent=2, ensure_ascii=False)

        print(f"✅ 添加成功：{name}")
    else:
        print("❌ 无效格式！请使用 <:name:id>")