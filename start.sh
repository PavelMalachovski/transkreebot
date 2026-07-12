#!/bin/sh
# PO token provider for YouTube (see Dockerfile); the bot works without it,
# so a crash here must not take the bot down
node /opt/bgutil/server/build/main.js &
exec python bot/main.py
