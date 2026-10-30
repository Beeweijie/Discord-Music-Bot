"""Voice commands share the existing member-command and queue boundaries."""
import asyncio
import logging
import os
import time
import re
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from types import SimpleNamespace

import discord
from discord.ext import commands

from bot_app.features.voice_control.grammar import (WakeGate, VoiceCommand, clean_speech,
    is_wake_text, is_quiet_text, embedded_command)
from bot_app.shared.runtime import write_status

logger = logging.getLogger("bot_app.features.voice_control.discord")


class VoiceControlService:
    def __init__(self, bot, music=None):
        self.bot = bot
        if music is None:
            from bot_app.features.music.api import MusicAPI
            music = MusicAPI(bot.get_cog("Music").service)
        self.music = music
        self.enabled = os.getenv("VOICE_COMMANDS_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
        self.disabled_guilds = set()
        self.active = {}
        self.queue = asyncio.Queue(maxsize=6)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voice-asr")
        self.tasks = []
        self.recognizer = None
        self.utterances = self.recognized = 0
        self.cooldowns = {}
        self.stats = Counter()
        self.test_users = {}
        self.replies = None
        self.reply_jobs = set()
        self.pending = Counter()
        self.music_search = None
        self.song_choices = {}
        self.command_jobs = set()
        self.user_jobs = {}
        self.retried = set()
        self.ignore_before = {}

    def reset_user(self, vc, gate, user_id, cutoff=None):
        key = (vc.guild.id, user_id)
        gate.armed.pop(user_id, None)
        gate.partial.pop(user_id, None)
        self.song_choices.pop(key, None)
        self.retried.discard(key)
        self.ignore_before[key] = max(self.ignore_before.get(key, 0),
            time.monotonic() if cutoff is None else cutoff)
        self.user_jobs.pop(key, None)
        for job in list(self.command_jobs):
            if getattr(job, 'voice_key', None) == key and job is not asyncio.current_task():
                job.cancel()
        if self.replies:
            self.replies.stop(vc)

    def schedule_text(self, vc, sink, gate, user_id, result, overlap=False, stamp=None):
        key = (vc.guild.id, user_id)
        interrupt = result.heard_wake or is_wake_text(result.text) or is_quiet_text(result.text)
        if (not interrupt and user_id not in gate.armed and user_id not in gate.partial
                and key not in self.song_choices):
            return
        if interrupt:
            self.reset_user(vc, gate, user_id, cutoff=stamp)
        previous = self.user_jobs.get(key)
        async def run():
            if previous:
                await asyncio.gather(previous, return_exceptions=True)
            try:
                await self.handle_text(vc, sink, gate, user_id, result.text, result.heard_wake, overlap, stamp)
            except Exception:
                self.stats['command_errors'] += 1
                logger.exception('Voice command task failed', extra={'guild_id': vc.guild.id})
        job = asyncio.create_task(run(), name='voice.command')
        job.voice_key = key
        self.command_jobs.add(job)
        self.user_jobs[key] = job
        def done(task):
            self.command_jobs.discard(task)
            if self.user_jobs.get(key) is task:
                self.user_jobs.pop(key, None)
        job.add_done_callback(done)

    async def cog_load(self):
        if not self.enabled:
            self.publish("disabled")
            return
        try:
            from bot_app.host.discord.voice_receive import SpeechVoiceClient, SpeechSink
            # Validate the native codec before advertising a working receiver.
            # Linux installs need libopus0 in addition to the Python packages.
            discord.opus.Decoder()
            self.sink_class = SpeechSink
            self.recognizer = await asyncio.get_running_loop().run_in_executor(
                self.executor, self.bot.factories.speech)
            self.replies = self.bot.factories.voice_replies()
            self.music_search = self.bot.factories.voice_search()
            self.music.configure_connection({"cls": SpeechVoiceClient, "self_deaf": False})
            self.tasks = [asyncio.create_task(self.watch(), name="voice.receive"),
                          asyncio.create_task(self.consume(), name="voice.recognize")]
            self.publish("ready")
            logger.info("Local voice commands ready; say 喵酱喵酱 followed by a music command")
        except Exception:
            self.publish("error")
            logger.exception("Voice commands could not start; check optional dependencies, Linux libopus0 and VOICE_MODEL_PATH")

    def publish(self, state):
        sinks = [item[1] for item in self.active.values()]
        write_status(voice_commands={"enabled": self.enabled, "state": state,
            "engine": "SenseVoiceSmall / FunASR (local)", "active_channels": len(sinks),
            "model": getattr(self.recognizer, 'model_name', '') if self.recognizer else '',
            "language": getattr(self.recognizer, 'language', '') if self.recognizer else '',
            "packets": sum(s.packets for s in sinks), "decoded": sum(s.decoded for s in sinks),
            "decode_errors": sum(s.errors for s in sinks),
            "utterances": self.utterances, "commands": self.recognized,
            "wake_detector": "phonetic" if self.recognizer and self.recognizer.spotter else "text",
            "spoken_replies": self.replies is not None,
            "diagnostics": dict(self.stats)})

    def stop_guild(self, guild_id):
        item = self.active.pop(guild_id, None)
        if item:
            vc, sink, gate = item
            for key in list(self.user_jobs):
                if key[0] == guild_id:
                    self.reset_user(vc, gate, key[1])
            self.song_choices = {k: v for k, v in self.song_choices.items() if k[0] != guild_id}
            sink.cleanup()  # Invalidate pending recognition before stopping threads.
            vc.stop_listening()

    async def on_voice_state_update(self, member, before, after):
        if (self.bot.user and member.id == self.bot.user.id
                and before.channel != after.channel):
            self.stop_guild(member.guild.id)

    async def cog_unload(self):
        for task in self.tasks:
            task.cancel()
        for task in self.reply_jobs:
            task.cancel()
        jobs = list(self.command_jobs)
        for task in jobs:
            task.cancel()
        for guild_id in list(self.active):
            self.stop_guild(guild_id)
        self.music.configure_connection({})
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await asyncio.gather(*self.reply_jobs, return_exceptions=True)
        await asyncio.gather(*jobs, return_exceptions=True)
        while not self.queue.empty():
            self.queue.get_nowait()
        await asyncio.to_thread(self.executor.shutdown, wait=True, cancel_futures=True)
        self.test_users.clear()
        self.song_choices.clear()

    def live_member(self, vc, sink, user_id):
        item = self.active.get(vc.guild.id)
        if (not item or item[1] is not sink or sink.closed or not vc.is_connected()
                or vc.guild.voice_client is not vc or vc.channel.id != sink.channel_id):
            return None
        member = vc.guild.get_member(user_id)
        if (not member or member.bot or not member.voice or not member.voice.channel
                or member.voice.channel.id != sink.channel_id or member.voice.suppress):
            return None
        return member

    async def watch(self):
        last_status = 0
        while True:
            try:
                for guild_id, (vc, sink, _) in list(self.active.items()):
                    if (not vc.is_connected() or vc.channel.id != sink.channel_id
                            or not vc.is_listening() or guild_id in self.disabled_guilds):
                        self.stop_guild(guild_id)
                for vc in self.bot.voice_clients:
                    if (vc.guild.id in self.active or vc.guild.id in self.disabled_guilds
                            or not vc.is_connected() or not hasattr(vc, "listen")):
                        continue
                    sink = self.sink_class(vc)
                    vc.listen(sink)
                    self.active[vc.guild.id] = (vc, sink, WakeGate())
                    logger.info("Voice commands listening", extra={"guild_id": vc.guild.id, "channel_id": vc.channel.id})
                now = time.monotonic()
                for vc, sink, gate in self.active.values():
                    for user_id, pcm, stamp, overlap in sink.take_ready(now):
                        if not self.queue.full():
                            self.queue.put_nowait((vc, sink, gate, user_id, pcm, stamp, overlap))
                            self.pending[(id(sink), user_id)] += 1
                        else:
                            self.stats["queue_full"] += 1
                    for user_id, expiry in list(gate.armed.items()):
                        if now <= expiry:
                            continue
                        if (self.pending[(id(sink), user_id)] or sink.activity(user_id)["buffer_seconds"]
                                or (vc.guild.id, user_id) in self.user_jobs):
                            gate.armed[user_id] = now + 1
                            continue
                        gate.armed.pop(user_id, None)
                        self.retried.discard((vc.guild.id, user_id))
                    for key, choice in list(self.song_choices.items()):
                        if now > choice['expires']:
                            self.song_choices.pop(key, None)
                if now - last_status > 15:
                    self.publish("listening" if self.active else "ready")
                    last_status = now
                for key, test in list(self.test_users.items()):
                    if now >= test["expires"]:
                        self.test_users.pop(key, None)
                        active = self.active.get(key[0])
                        activity = active[1].activity(key[1]) if active else {"frames": 0, "peak_rms": 0}
                        frames = max(0, activity['frames'] - test["baseline_frames"])
                        await test["ctx"].send(f"🧪 本人语音测试结束。测试期间收到音频帧 {frames}，连接期间峰值 RMS {activity['peak_rms']}。未保存录音。",
                            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            except Exception:
                logger.exception("Voice receiver check failed; retrying")
            await asyncio.sleep(0.2)

    async def consume(self):
        while True:
            vc, sink, gate, user_id, pcm, stamp, overlap = await self.queue.get()
            try:
                if (time.monotonic() - stamp > 8 or stamp < self.ignore_before.get((vc.guild.id, user_id), 0)
                        or not self.live_member(vc, sink, user_id)):
                    self.stats["stale_or_left"] += 1
                    continue
                testing = (vc.guild.id, user_id) in self.test_users
                recognize = self.recognizer.recognize_diagnostic if testing else self.recognizer.recognize
                result = await asyncio.get_running_loop().run_in_executor(
                    self.executor, recognize, pcm, (id(sink), user_id))
                self.utterances += 1
                if result.heard_wake:
                    self.stats["acoustic_wake"] += 1
                if time.monotonic() - stamp > 8 or stamp < self.ignore_before.get((vc.guild.id, user_id), 0):
                    self.stats["stale_after_asr"] += 1
                    continue
                await self.report_test(vc, sink, user_id, result)
                self.schedule_text(vc, sink, gate, user_id, result, overlap, stamp)
            except Exception:
                self.stats["errors"] += 1
                logger.exception("Voice command failed", extra={"guild_id": vc.guild.id})
            finally:
                # Speech audio and unrelated transcripts are never saved or logged.
                pcm = result = None
                self.pending[(id(sink), user_id)] = max(0, self.pending[(id(sink), user_id)] - 1)
                if not self.pending[(id(sink), user_id)]:
                    self.pending.pop((id(sink), user_id), None)
                self.queue.task_done()

    async def say(self, vc, sink, kind):
        if not self.replies or sink.closed:
            return False
        try:
            spoken = await self.music.speak(self.replies, vc, kind, sink.set_reply_active)
            self.stats['spoken_replies' if spoken else 'reply_busy'] += 1
            return spoken
        except Exception:
            self.stats['reply_errors'] += 1
            logger.warning('Could not speak voice reply', exc_info=True, extra={'guild_id':vc.guild.id})
            return False

    async def report_test(self, vc, sink, user_id, result):
        key = (vc.guild.id, user_id)
        test = self.test_users.get(key)
        if not test or time.monotonic() >= test["expires"] or not self.live_member(vc, sink, user_id):
            return
        text = discord.utils.escape_markdown(result.text[:300]) or "（空）"
        activity = sink.activity(user_id)
        extra = ''
        if result.alternate_text:
            extra += '\n中文模型对照：' + discord.utils.escape_markdown(result.alternate_text[:300])
        if result.diagnostics:
            extra += '\n音频诊断：' + str(result.diagnostics) + '；流诊断：' + str(activity.get('last_segment', {})) + '；缺包：' + str(activity.get('missing_packets', 0))
        await test["ctx"].send(f"🧪 本人测试转写：{text}\n读音唤醒：{'是' if result.heard_wake else '否'}；连接期间峰值 RMS：{activity['peak_rms']}。" + extra,
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        test["remaining"] -= 1
        if test["remaining"] <= 0:
            self.test_users.pop(key, None)

    async def handle_text(self, vc, sink, gate, user_id, text, heard_wake=False, overlap=False, stamp=None):
        member = self.live_member(vc, sink, user_id)
        if not member:
            return
        user_key = (vc.guild.id, user_id)
        if is_quiet_text(text):
            self.reset_user(vc, gate, user_id, cutoff=stamp)
            self.stats['quiet'] += 1
            return
        fresh_wake = heard_wake or is_wake_text(text)
        if fresh_wake:
            self.song_choices.pop(user_key, None)
            self.retried.discard(user_key)
        elif overlap:
            # Preserve interruptions while ignoring the bot's own short reply
            # leaking through a member's microphone.
            tail = embedded_command(text) if user_id in gate.armed else None
            if tail:
                text = tail
            elif not (user_key in self.song_choices and re.fullmatch(r'(?:第)?[一二三123](?:个|個)?', clean_speech(text))):
                self.stats['reply_echo_ignored'] += 1
                return
        choice = self.song_choices.get((vc.guild.id, user_id))
        if choice and (choice['sink'] is not sink or time.monotonic() > choice['expires']):
            self.song_choices.pop((vc.guild.id, user_id), None)
            choice = None
        selection = None
        if choice:
            cleaned = clean_speech(text)
            match = re.fullmatch(r"(?:选|選)?第?([一二三123])(?:个|個|首)?(?:歌|歌曲)?(?:吧)?", cleaned)
            if match:
                index = {'一': 0, '二': 1, '三': 2, '1': 0, '2': 1, '3': 2}[match[1]]
                if index < len(choice['items']):
                    selection = choice['items'][index]
                    self.song_choices.pop((vc.guild.id, user_id), None)
                    command = VoiceCommand(choice['action'], selection['url'])
                else:
                    return
            elif cleaned in {'取消', '算了'}:
                self.song_choices.pop((vc.guild.id, user_id), None)
                return
        if selection is None:
            command = gate.accept(user_id, text, time.monotonic(), heard_wake)
            self.stats[gate.last_reason] += 1
        if not command and gate.last_reason == "unsupported_armed":
            command = VoiceCommand("unclear")
        if not command:
            return
        if command.action == 'unclear':
            if user_key in self.retried:
                gate.armed.pop(user_id, None)
                self.retried.discard(user_key)
                return
            self.retried.add(user_key)
        key = (vc.guild.id, user_id, command.action, command.query)
        now = time.monotonic()
        if self.cooldowns.get(key, 0) > now:
            self.stats["duplicate"] += 1
            return
        channel_id = self.music.text_channel(vc.guild.id)
        channel = self.bot.get_channel(channel_id) if channel_id else None
        # Fall back to the voice channel's built-in text chat for /join sessions.
        channel = channel or vc.channel
        permissions = channel.permissions_for(member)
        if not permissions.view_channel:
            return
        # A wake acknowledgement must never suppress the following command.
        self.cooldowns[key] = now + (2 if command.action in {"wake", "unclear"} else 0.8)
        if len(self.cooldowns) > 500:
            self.cooldowns = {k: t for k, t in self.cooldowns.items() if t > now}
        if command.action in {"wake", "unclear"}:
            message = "🐱 我在喵，接着说指令吧。" if command.action == "wake" else "🐱 没听清喵，请再说一次指令。"
            await channel.send(message, allowed_mentions=discord.AllowedMentions.none())
            await self.say(vc, sink, command.action)
            if command.action == 'wake' and self.live_member(vc, sink, user_id):
                gate.arm(user_id, time.monotonic())
            return
        self.recognized += 1
        async def send(*args, **kwargs):
            kwargs["allowed_mentions"] = discord.AllowedMentions.none()
            return await channel.send(*args, **kwargs)
        ctx = SimpleNamespace(author=member, guild=vc.guild, channel=channel, bot=self.bot, send=send)
        await send("🎤 " + (("播放：" if command.action == "play" else "加入队尾：") +
                   (selection['title'] if selection else command.query) if command.action in {"play", "enqueue"} else
                   {"skip": "下一首歌", "pause": "暂停播放", "resume": "继续播放", "playqueue": "播放当前歌单"}[command.action]))
        logger.info("Voice command: %s", command.action, extra={"guild_id": vc.guild.id, "channel_id": vc.channel.id})
        await self.say(vc, sink, "ok")
        if not self.live_member(vc, sink, user_id):
            return
        query = command.query
        if command.action in {"play", "enqueue"} and self.music_search and not selection:
            guess, candidates, selected = await self.music.resolve_voice(query, vc.guild.id, user_id, self.music_search)
            if not self.live_member(vc, sink, user_id):
                return
            if guess.query != query and not guess.alternatives:
                await send(f"💡 歌名联想：{discord.utils.escape_markdown(query)} → **{discord.utils.escape_markdown(guess.query)}**")
            if selected:
                query = selected['url']
                await send("🎵 找到：**" + discord.utils.escape_markdown(selected['title']) + "**")
            elif candidates:
                self.song_choices[(vc.guild.id, user_id)] = {'sink': sink, 'items': candidates,
                    'action': command.action, 'expires': time.monotonic() + 30}
                listing = '\n'.join(f"{i + 1}. {discord.utils.escape_markdown(item['title'])[:160]}" for i, item in enumerate(candidates))
                await send("🐱 找到几个接近的结果，请说「第一个」「第二个」或「第三个」。说「取消」退出，重新喊「喵酱喵酱」可换指令：\n" + listing)
                await self.say(vc, sink, "choices")
                return
            else:
                await send("🐱 没找到可靠的歌曲结果。请补充歌手，或重新说歌名。")
                return
        action = 'playskip' if command.action == 'play' else command.action
        await self.music.execute(action, ctx, *([query] if command.action in {"play", "enqueue"} else []))
