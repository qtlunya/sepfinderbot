#!/usr/bin/env python3

import argparse
import html
import logging
import plistlib
import urllib.parse
from enum import Enum

import requests
import toml
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import CommandHandler, Filters, MessageHandler, Updater


DEVICE_TYPES = {
    'iPhone': 'iPhone',
    'iPad': 'iPad',
    'iPod touch': 'iPod',
    'Apple TV': 'AppleTV',
}


class State(Enum):
    NONE = 0
    DEVICE_TYPE = 1
    DEVICE_MODEL = 2
    BOARD_CONFIG = 3
    FIRMWARE = 4


def start(update, ctx):
    update.message.reply_text('Please select a device type.', reply_markup=ReplyKeyboardMarkup([
        [
            'iPhone',
            'iPad',
        ],
        [
            'iPod touch',
            'Apple TV',
        ],
    ]))

    ctx.user_data['state'] = State.DEVICE_TYPE


def on_text(update, ctx):
    ctx.user_data.setdefault('state', State.NONE)

    text = update.message.text

    if ctx.user_data['state'] == State.DEVICE_TYPE:
        try:
            device_type = DEVICE_TYPES[text]
        except KeyError:
            return update.message.reply_text('Invalid input.')

        ctx.bot_data['devices'] = session.get('https://api.ipsw.me/v4/devices').json()

        devices = [x for x in ctx.bot_data['devices'] if x['identifier'].startswith(device_type)]

        if not devices:
            return update.message.reply_text(
                'No devices found. Please start over using /start.', reply_markup=ReplyKeyboardRemove(),
            )

        keyboard = []
        for i, device in enumerate(devices):
            if i % 2 == 0:
                keyboard.append([])
            keyboard[-1].append(device['name'])

        update.message.reply_text('Please select a device.', reply_markup=ReplyKeyboardMarkup(keyboard))

        ctx.user_data['state'] = State.DEVICE_MODEL
    elif ctx.user_data['state'] == State.DEVICE_MODEL:
        try:
            device = next(x for x in ctx.bot_data['devices'] if x['name'] == text)
        except StopIteration:
            return update.message.reply_text('Invalid input.')

        device = session.get(f'https://api.ipsw.me/v4/device/{device["identifier"]}').json()
        # Filter out DEV boards
        boards = [x['boardconfig'] for x in device['boards'] if x['boardconfig'].lower().endswith('ap')]

        ctx.user_data['device'] = device

        if len(boards) > 1:
            keyboard = []
            for i, board in enumerate(boards):
                if i % 2 == 0:
                    keyboard.append([])
                keyboard[-1].append(board)

            update.message.reply_text(
                'Please select your board config.\n\n'
                'You can get this using the System Info tweak or AIDA64 from the App Store.',
                reply_markup=ReplyKeyboardMarkup(keyboard),
            )

            ctx.user_data['state'] = State.BOARD_CONFIG
        else:
            ctx.user_data['boardconfig'] = boards[0]

            show_firmware_menu(update, ctx)
    elif ctx.user_data['state'] == State.BOARD_CONFIG:
        if not text.lower().endswith('ap'):
            return update.message.reply_text('Invalid input.')

        ctx.user_data['boardconfig'] = text

        show_firmware_menu(update, ctx)
    elif ctx.user_data['state'] == State.FIRMWARE:
        if 'device' not in ctx.user_data or 'boardconfig' not in ctx.user_data:
            return update.message.reply_text(
                'Invalid state. Please start over using /start.', reply_markup=ReplyKeyboardRemove()
            )

        try:
            firmware = next(x for x in ctx.user_data['device']['firmwares'] if x['version'] == text)
        except StopIteration:
            return update.message.reply_text('Invalid input.')

        p = urllib.parse.urlparse(firmware['url'])

        if p.netloc == 'appldnld.apple.com':
            # Partialzip required
            return update.message.reply_text(
                'Sorry, firmwares older than 11.3.1 are not currently supported.', reply_markup=ReplyKeyboardRemove()
            )
        else:
            buildmanifest_url = urllib.parse.urlunparse(
                p._replace(path='/'.join([*p.path.split('/')[:-1], 'BuildManifest.plist']))
            )

            r = session.get(buildmanifest_url)

            buildmanifest = plistlib.loads(r.content)

            buildidentity = next(
                x for x in buildmanifest['BuildIdentities']
                if x['Info']['DeviceClass'].lower() == ctx.user_data['boardconfig'].lower()
            )

            sep_path = buildidentity['Manifest']['RestoreSEP']['Info']['Path']
            bb_path = buildidentity['Manifest']['BasebandFirmware']['Info']['Path']

            update.message.reply_text(
                f'<b>SEP</b>: {html.escape(sep_path)}\n'
                f'<b>Baseband</b>: {html.escape(bb_path)}',
                parse_mode='html',
                reply_markup=ReplyKeyboardRemove()
            )

        ctx.user_data.clear()
    else:
        update.message.reply_text('Invalid state. Please start over using /start.', reply_markup=ReplyKeyboardRemove())


def show_firmware_menu(update, ctx):
    if 'device' not in ctx.user_data:
        return update.message.reply_text(
            'Invalid state. Please start over using /start.', reply_markup=ReplyKeyboardRemove(),
        )

    firmwares = [x for x in ctx.user_data['device']['firmwares'] if x['signed']]

    keyboard = []
    for i, firmware in enumerate(firmwares):
        if i % 2 == 0:
            keyboard.append([])
        keyboard[-1].append(firmware['version'])

    update.message.reply_text('Please select a version.', reply_markup=ReplyKeyboardMarkup(keyboard))

    ctx.user_data['state'] = State.FIRMWARE


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--debug', action='store_true', help='enable debug logging')
    args = parser.parse_args()

    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.DEBUG if args.debug else logging.INFO,
    )

    config = toml.load('config.toml')

    updater = Updater(config['token'])
    dispatcher = updater.dispatcher

    session = requests.Session()

    dispatcher.add_handler(CommandHandler('start', start))
    dispatcher.add_handler(MessageHandler(Filters.text, on_text))

    updater.start_polling()
