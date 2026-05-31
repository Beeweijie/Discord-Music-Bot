"""Discord Bot 启动入口。

这里负责读取环境变量、配置 Discord intents、加载功能扩展，并启动 Bot。
具体音乐和欢迎逻辑分别放在 bot.music / bot.welcome 中。
"""

import json
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

from bot.path import EMOJI_JSON


# ===== 基础配置 =====

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = 1298956383819010090


# ===== Discord 权限意图 =====

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.voice_states = True
intents.members = True


class MyBot(commands.Bot):
    """项目自定义 Bot，用 setup_hook 统一加载扩展和同步 slash commands。"""

    async def setup_hook(self):
        # 1. 启动阶段先加载扩展，扩展内会注册各自的 commands / listeners。
        await self.load_extension("bot.music")
        await self.load_extension("bot.welcome")

        # 2. 清理旧的全局 slash commands，避免 Discord 客户端点到过期的 /play。
        self.tree.clear_commands(guild=None)
        await self.tree.sync()

        # 3. 再同步当前 guild 的 slash commands，方便开发时快速生效。
        guild = discord.Object(id=GUILD_ID)
        synced = await self.tree.sync(guild=guild)
        print(f"✅ 已同步 {len(synced)} 个 guild slash commands")
        for cmd in synced:
            print(f" - /{cmd.name}")


bot = MyBot(command_prefix="!", intents=intents)


# ===== 配置文件 =====

with open(EMOJI_JSON, "r", encoding="utf-8") as f:
    emojis = json.load(f)


# ===== Bot 事件 =====

@bot.event
async def on_ready():
    """Bot 成功登录后触发。"""
    print(f"✅ Bot 已上线：{bot.user}")


@bot.event
async def on_message(message):
    """保留前缀命令处理入口。"""
    await bot.process_commands(message)


# ===== 简单测试命令 =====

@bot.command()
async def a(ctx):
    """手动同步当前 guild 的 slash commands。"""
    guild = discord.Object(id=GUILD_ID)
    synced = await bot.tree.sync(guild=guild)
    await ctx.send(f"同步完成喵~ {len(synced)} 个")


@bot.command()
async def emoji(ctx):
    """把 config/emoji.json 中配置的表情逐个发送出来。"""
    for name, symbol in emojis.items():
        await ctx.send(symbol)


@bot.command()
async def add(ctx, a: int, b: int):
    """测试普通前缀命令是否正常工作。"""
    await ctx.send(a + b)


def main():
    """启动 Discord Bot。"""
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
