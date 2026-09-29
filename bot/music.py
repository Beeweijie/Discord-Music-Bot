"""Compatibility extension for existing deployments."""
from bot_app.presentation.discord.music import Music, setup
from bot_app.application.music.models import Song, ChannelSession
from bot_app.domain.music.policies import *
