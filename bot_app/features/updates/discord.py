"""Discord command registration for this feature."""
import os
import json
from discord.ext import commands
from bot_app.shared.paths import EMOJI_JSON


def register(bot):
    @bot.command(name="update")
    @commands.is_owner()
    @commands.cooldown(1, 60, commands.BucketType.user)
    @commands.max_concurrency(1, per=commands.BucketType.default, wait=False)
    async def update_bot(ctx, target: str = ""):
        """Owner only: latest release, branch:chatbot, or a commit SHA from the trusted repository."""
        from bot_app.features.updates.policies import UpdateError
        if os.getenv("BOT_SUPERVISED") != "1":
            await ctx.send("Start the bot through start.bat or python main.py to enable automatic update restarts.")
            return
        await ctx.send("Checking GitHub and preparing the update. The bot stays online while dependencies are installed.")
        try:
            ticket = await bot.updates.prepare(target)
            if ticket is None:
                await ctx.send(f"Already up to date ({bot.updates.backend.current_label()}).")
                return
            await ctx.send(f"Prepared {ticket.get('label',ticket['version'])}. Saving queues and restarting; this briefly interrupts playback. AI databases will be backed up; local settings and data are preserved.")
            bot.updates.activate(ticket, ctx.channel.id)
            bot.update_requested = True
            await bot.close()
        except UpdateError as error:
            await ctx.send(str(error))
        except Exception:
            if bot.update_requested:
                bot.updates.cancel()
                bot.update_requested = False
            raise
