"""Compatibility extension for existing deployments."""
from bot_app.features.music.adapters.discord import Music, setup
from bot_app.features.music.models import Song, ChannelSession
from bot_app.features.music.policies import *
