"""Member-authorized memory commands; source and extraction details stay in memory."""
import time
from discord.ext import commands
import discord
from bot_app.shared.identity import Scope, MessageEvent

class Memory(commands.Cog):
    def __init__(self, bot, runtime):
        self.bot, self.runtime = bot, runtime

    @commands.hybrid_command(name='memory', description='Your memory: list, forget, correct, remember')
    @commands.guild_only()
    async def memory_command(self, ctx, operation: str = 'list', memory_id: str = '', *, text: str = ''):
        scope = Scope(str(ctx.guild.id), str(ctx.channel.id))
        actor = str(ctx.author.id)
        if operation == 'forget':
            count = await self.runtime.memory.forget(scope.key, actor, memory_id or None)
            await self.runtime.chat.clear_context(scope.key)
            await ctx.send(f'Forgot {count} profile records and removed associated retained context.', ephemeral=True)
            return
        if not self.runtime.chat.allowed(scope.key) or not self.bot.management.get_settings('memory', ctx.guild.id)['enabled']:
            await ctx.send('Memory is disabled in this channel. You can still use /memory forget.', ephemeral=True)
            return
        if operation == 'list':
            rows = await self.runtime.memory.list(scope.key, actor, candidates=True)
            content = '\n'.join(f"{row['id']} · {row['state']} · {row['content'][:180]}" for row in rows[:8]) or 'No profile memories recorded for you here.'
            await ctx.send(content[:1900], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        elif operation == 'correct':
            await self.runtime.memory.correct(scope.key, memory_id, text, actor)
            await self.runtime.chat.clear_context(scope.key)
            await ctx.send('Memory corrected.', ephemeral=True)
        elif operation == 'remember':
            content = (memory_id + ' ' + text).strip()
            if not 1 <= len(content) <= 1800:
                await ctx.send('Supply a note of 1-1800 characters.', ephemeral=True)
                return
            source_id = str(ctx.message.id if ctx.interaction is None else ctx.interaction.id)
            event = MessageEvent(scope, source_id, actor, ctx.author.display_name, content, time.time())
            if not await self.runtime.memory.remember(event):
                await ctx.send('This note could not be stored.', ephemeral=True)
                return
            await ctx.send('Note remembered for this channel.', ephemeral=True)
        else:
            await ctx.send('Use /memory list, forget [ID], correct ID text, or remember text.', ephemeral=True)

async def setup(bot):
    await bot.add_cog(Memory(bot, bot.management.ai))
