# SEP Finder Bot

This is a Telegram bot that will help you find the correct SEP and baseband files to use for your device with futurerestore.

## Usage

A hosted instance is available at <https://t.me/sepfinderbot>. Press the "Start" button or send `/start` and it will guide you through the process.

## Setup

If you wish to run your own instance, copy `config.toml.example` to `config.toml` and enter a bot token you obtained from BotFather. Then run `poetry install` to install the dependencies, and `poetry run ./sepfinder.py` to run it. (Python 3.7 or newer is required.)
