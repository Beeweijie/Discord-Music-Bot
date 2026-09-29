"""Welcome event policy, deduplication and retry state, owned by the event loop."""
from collections import OrderedDict


class WelcomeService:
    def __init__(self, delivery, *, enabled, include_bots, channel_id, max_remembered):
        self.settings_repository = None
        self.delivery = delivery
        self.enabled = enabled
        self.include_bots = include_bots
        self.channel_id = channel_id
        self.max_remembered = max_remembered
        self._sending = set()
        self._welcomed = OrderedDict()

    def settings_for(self, guild_id):
        if self.settings_repository is not None:
            return self.settings_repository.read("welcome", guild_id)
        return {"enabled": self.enabled, "include_bots": self.include_bots, "channel_id": self.channel_id}

    def _pick_channel(self, guild):
        return self.delivery.pick_channel(guild, int(self.settings_for(guild.id)["channel_id"]))

    async def _send_welcome(self, member, reason=""):
        settings = self.settings_for(member.guild.id)
        if not settings["enabled"] or (member.bot and not settings["include_bots"]):
            return
        key = (member.guild.id, member.id)
        joined_at = member.joined_at
        event_key = (*key, joined_at)
        if event_key in self._sending or (key in self._welcomed and self._welcomed[key] == joined_at):
            return
        channel = self._pick_channel(member.guild)
        if channel is None:
            return
        self._sending.add(event_key)
        try:
            if await self.delivery.send(channel, member, reason):
                self._welcomed[key] = joined_at
                self._welcomed.move_to_end(key)
                while len(self._welcomed) > self.max_remembered():
                    self._welcomed.popitem(last=False)
        finally:
            self._sending.discard(event_key)

    async def on_member_join(self, member):
        """成员刚加入服务器时触发。"""
        pending = getattr(member, "pending", None)
        # 如果服务器没有开启入服验证，或者状态不可用，就直接欢迎。
        if pending is False or pending is None:
            await self._send_welcome(member, reason="on_member_join")


    async def on_member_update(self, before, after):
        """处理入服验证完成：pending 从 True 变为 False。"""
        b = getattr(before, "pending", None)
        a = getattr(after, "pending", None)

        if b is True and a is False:
            await self._send_welcome(after, reason="pending->False")


    async def on_member_remove(self, member):
        """Allow a member who leaves and rejoins to be welcomed again."""
        key = (member.guild.id, member.id)
        if key in self._welcomed and self._welcomed[key] == member.joined_at:
            self._welcomed.pop(key, None)


    async def on_guild_remove(self, guild):
        for key in list(self._welcomed):
            if key[0] == guild.id:
                self._welcomed.pop(key, None)
