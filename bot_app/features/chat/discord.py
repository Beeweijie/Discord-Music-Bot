"""Discord message transport and server-authorized chat configuration."""
import discord
from discord.ext import commands
from bot_app.shared.identity import Scope, MessageEvent

class Chat(commands.Cog):
    def __init__(self, bot, runtime):
        self.bot, self.runtime = bot, runtime

    async def cog_load(self):
        self.bot.management.ai = self.runtime
        try:
            await self.runtime.start()
        except BaseException:
            await self.runtime.close()
            self.bot.management.ai = None
            raise

    async def cog_unload(self):
        await self.runtime.close()
        self.bot.management.ai = None

    def event(self, message):
        me = self.bot.user
        reference = message.reference
        replied = getattr(reference, 'resolved', None)
        direct = bool(me and (me in message.mentions or getattr(getattr(replied, 'author', None), 'id', None) == me.id))
        cfg = self.bot.management.get_settings('chat', message.guild.id)
        direct = direct or any(message.content.lstrip().casefold().startswith(name.casefold()) for name in cfg['aliases'])
        return MessageEvent(Scope(str(message.guild.id), str(message.channel.id)), str(message.id), str(message.author.id),
                            message.author.display_name, message.content, message.created_at.timestamp(),
                            str(reference.message_id) if reference else '', direct, message.author.bot)

    @commands.Cog.listener()
    async def on_message(self, message):
        if not message.guild or message.author.bot or message.webhook_id or not message.content or message.content.startswith('!'):
            return
        if (await self.bot.get_context(message)).valid:
            return
        event = self.event(message)
        async def can_send():
            if not self.runtime.chat.allowed(event.scope.key):
                return False
            permissions = message.channel.permissions_for(message.guild.me)
            return permissions.view_channel and permissions.send_messages
        async def send(text, target):
            if not await can_send():
                raise ValueError('Channel is no longer writable.')
            # No raw model-provided target IDs or mention permissions are passed to Discord.
            sent = await message.channel.send(text, allowed_mentions=discord.AllowedMentions.none())
            return MessageEvent(event.scope, str(sent.id), str(sent.author.id), sent.author.display_name,
                                sent.content, sent.created_at.timestamp(), is_bot=True)
        await self.runtime.chat.receive(event, send, can_send)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload):
        if payload.guild_id:
            await self.runtime.chat.deleted(Scope(str(payload.guild_id), str(payload.channel_id)).key, str(payload.message_id))

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload):
        if payload.guild_id:
            scope = Scope(str(payload.guild_id), str(payload.channel_id)).key
            for message_id in payload.message_ids:
                await self.runtime.chat.deleted(scope, str(message_id))

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload):
        if not payload.guild_id or 'content' not in payload.data:
            return
        scope = Scope(str(payload.guild_id), str(payload.channel_id))
        if not self.runtime.chat.allowed(scope.key):
            return
        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            return
        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.HTTPException:
            return
        if not message.author.bot:
            await self.runtime.chat.edited(self.event(message))

    @commands.hybrid_command(name='chat', description='Show or configure group chat: status, observe, on, off')
    @commands.guild_only()
    async def chat_command(self, ctx, mode: str = 'status'):
        if mode not in {'status', 'observe', 'on', 'off'}:
            await ctx.send('Use /chat status, observe, on or off.', ephemeral=True)
            return
        cfg = self.bot.management.get_settings('chat', ctx.guild.id)
        if mode != 'status':
            if not ctx.author.guild_permissions.manage_guild:
                await ctx.send('Manage Server permission is required.', ephemeral=True)
                return
            channel_id = str(ctx.channel.id)
            overrides = dict(cfg['channel_overrides'])
            if mode == 'off':
                cfg['channels'] = [item for item in cfg['channels'] if item != channel_id]
                overrides.pop(channel_id, None)
            else:
                cfg['enabled'] = True
                cfg['channels'] = list(dict.fromkeys(cfg['channels'] + [channel_id]))
                overrides[channel_id] = dict(overrides.get(channel_id, {}), mode='participate' if mode == 'on' else 'observe')
            cfg['channel_overrides'] = overrides
            self.bot.management.update_settings('chat', cfg, ctx.guild.id)
            await self.runtime.chat.clear_context(Scope(str(ctx.guild.id), channel_id).key)
        effective = self.runtime.chat.config(Scope(str(ctx.guild.id), str(ctx.channel.id)).key)
        health = self.runtime.health()
        await ctx.send(f"Chat: {'enabled' if self.runtime.chat.allowed(Scope(str(ctx.guild.id),str(ctx.channel.id)).key) else 'disabled'}; mode: {effective['mode']}; model: {health['state']}. Configure memory separately in the dashboard.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Chat(bot, bot.factories.ai()))
