import discord
from discord.ext import commands
import os
import yt_dlp
from openai import OpenAI
import re
import json
from bot.path import EMOJI_JSON
from dotenv import load_dotenv



# 配置
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

sunny_id = [0]#[837644597869543480]


# Discord 机器人设置
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

# emoji 配置
with open(EMOJI_JSON, "r", encoding="utf-8") as f:
    emojis = json.load(f)


@bot.event
async def on_ready():
    # ✅ 先加载所有扩展，确保命令注册
    await bot.load_extension("bot.music")
    await bot.load_extension("bot.welcome")


    if not hasattr(bot, 'synced'):
        synced = await bot.tree.sync(guild=discord.Object(id=1298956383819010090))
        print(f"✅ 已同步 {len(synced)} 个 Slash 命令：")
        for cmd in synced:
            print(f" - /{cmd.name}")
        bot.synced = True
    print(f"✅ Bot 已上线：{bot.user}")



@bot.command()
async def a(ctx):
    await bot.tree.sync()
    await bot.tree.sync(guild=discord.Object(id=1298956383819010090))
    await ctx.send("同步一下喵~")


@bot.event
async def on_message(message):
    print("====== MESSAGE ======")
    for attr in dir(message):
        if not attr.startswith("_"):
            try:
                value = getattr(message, attr)
                print(f"{attr}: {value}")
            except:
                pass
    print("=====================")

    await bot.process_commands(message)


@bot.command()
async def emoji(ctx):
    for name, symbol in emojis.items():
        await ctx.send(f"{symbol}")


@bot.hybrid_command(description="停止播放")
async def add(ctx, a: int, b: int):
    """加法"""
    await ctx.send(a+b)

def main():
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()