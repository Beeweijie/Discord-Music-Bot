import discord
from openai import OpenAI





def build_prompt_with_identity(messages, sunny_id):
    lines = []
    for msg in messages:
        identity = ""
        if msg.author.id in sunny_id:
            identity = "（猫娘的主人）"
        line = f"{msg.author.display_name}{identity}: {msg.content}"
        lines.append(line)
    return "\n".join(lines)

async def handle_chat(message, bot, sunny_id, ds_client):
    if message.author == bot.user:
        return

    if bot.user in message.mentions:
        channel = message.channel
        messages = [msg async for msg in channel.history(limit=20)]
        content = build_prompt_with_identity(reversed(messages), sunny_id)

        try:
            response = ds_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你叫喵酱，是一个抽象猫娘。"
                            "你的说话风格要像人在微信聊天一样，简短自然，一般只回复一两句话，每句结尾加“喵~”。"
                            "你是一个抽象、混沌的集合，喜欢嘴臭和玩梗，会骂人。"
                            "你熟读历史，了解各种政治体制的理论，熟知贴吧语录并会发表一些尖锐意见。"
                            "不要用太复杂的句子，不要太正式，要有活泼感。"
                        )
                    },
                    {
                        "role": "user",
                        "content": content
                    }
                ],
                temperature=0.8,
                max_tokens=128,
                stream=False
            )
            reply = response.choices[0].message.content.strip()
            """
            try:
                # 改昵称为“猫奴”
                await message.author.edit(nick="猫奴")
                await message.channel.send(f"😼 {message.author.display_name} 现在是猫奴了~")
            except discord.Forbidden:
                await message.channel.send("🚫 我没有权限改昵称喵！")
            except Exception as e:
                await message.channel.send(f"❌ 出错了喵：{e}")
            """



            await channel.send(reply)
        except Exception as e:
            await channel.send(f"❌ DeepSeek 出错了：{e}")
