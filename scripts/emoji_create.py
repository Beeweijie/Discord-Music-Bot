"""交互式添加 Discord 自定义表情。

运行脚本后，输入 Discord 表情格式 `<:name:id>`，脚本会写入 config/emoji.json。
输入 stop / exit / quit 可以退出。
"""

import json
import os
import re


FILE_PATH = "../config/emoji.json"


# ===== 读取现有表情配置 =====

if os.path.exists(FILE_PATH):
    with open(FILE_PATH, "r", encoding="utf-8") as f:
        try:
            emojis = json.load(f)
        except json.JSONDecodeError:
            emojis = {}
else:
    emojis = {}


# ===== 命令行交互 =====

print("💬 输入表情（格式：<:name:id>），输入 'stop' 退出")

while True:
    user_input = input("👉 ").strip()

    if user_input.lower() in {"stop", "exit", "quit"}:
        print("👋 已退出添加")
        break

    match = re.match(r"<:(\w+):(\d+)>", user_input)
    if match:
        name, emoji_id = match.groups()
        emojis[name] = f"<:{name}:{emoji_id}>"

        with open(FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(emojis, f, indent=2, ensure_ascii=False)

        print(f"✅ 添加成功：{name}")
    else:
        print("❌ 无效格式！请使用 <:name:id>")
