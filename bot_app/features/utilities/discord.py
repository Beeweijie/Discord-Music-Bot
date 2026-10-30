"""Discord command registration for this feature."""
import os
import json
from discord.ext import commands
from bot_app.shared.paths import EMOJI_JSON


def register(bot):
    @bot.command(name="sync", aliases=["a"])
    @commands.is_owner()
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def sync_commands(ctx):
        """Owner only: synchronize global slash commands."""
        synced = await bot.tree.sync()
        await ctx.send(f"Synced {len(synced)} global slash commands.")


    @bot.command()
    async def emoji(ctx):
        """Show configured emoji."""
        try:
            values = json.loads(EMOJI_JSON.read_text(encoding="utf-8"))
            if not isinstance(values, dict):
                raise ValueError("Emoji config must be an object")
        except (OSError, ValueError):
            await ctx.send("表情配置无法读取，请检查 config/emoji.json。")
            return
        text = " ".join(str(value) for value in values.values())
        await ctx.send(text[:1900] or "尚未配置表情。")


    @bot.command()
    async def add(ctx, a: int, b: int):
        """Add two integers."""
        await ctx.send(a + b)


    @bot.hybrid_command()
    async def ping(ctx):
        """Check the bot's Discord latency."""
        latency = bot.latency_ms()
        await ctx.send(f"Pong! {latency} ms" if latency is not None else "Connecting to Discord.")
