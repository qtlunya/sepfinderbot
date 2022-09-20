#!/usr/bin/env python3

import argparse
import html
import logging
import plistlib
import tempfile
import urllib.parse
import zipfile
from enum import Enum
from io import BytesIO
from pathlib import Path

import requests
import toml
from packaging import version
from remotezip import RemoteZip
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import CallbackQueryHandler, CommandHandler, Filters, MessageHandler, Updater


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


def sepbb(update, ctx):
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

    ctx.user_data.clear()
    ctx.user_data['state'] = State.DEVICE_TYPE


def on_text(update, ctx):
    ctx.user_data.setdefault('state', State.NONE)

    text = update.message.text

    if ctx.user_data['state'] == State.DEVICE_TYPE:
        try:
            device_type = DEVICE_TYPES[text]
        except KeyError:
            return update.message.reply_text('Invalid input.')

        r = session.get('https://api.ipsw.me/v4/devices')

        if not r.ok:
            return update.message.reply_text('Unable to communicate with ipsw.me API, please try again later.')

        ctx.bot_data['devices'] = r.json()

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

        r = session.get(f'https://api.ipsw.me/v4/device/{device["identifier"]}')
        rb = session.get(f'https://api.m1sta.xyz/betas/{device["identifier"]}')
        if r.ok:
            device = r.json()
            ctx.user_data['ipswme_failed'] = False
        else:
            device = {
                'name': device['name'],
                'identifier': device['identifier'],
                'boards': device['boards'],
                'firmwares': [],
            }
            update.message.reply_text('Unable to communicate with ipsw.me API.')
            ctx.user_data['ipswme_failed'] = True

        if rb.ok:
            device_beta = rb.json()
            device['firmwares'] += [
                d for d in device_beta if not any(x['buildid'] == d['buildid'] for x in device['firmwares'])
            ]
        else:
            update.message.reply_text('Unable to communicate with the beta API.')
            if ctx.user_data['ipswme_failed']:
                return update.message.reply_text('Please try again later.', reply_markup=ReplyKeyboardRemove())

        # Filter out DEV boards
        boards = [x['boardconfig'] for x in device['boards'] if x['boardconfig'].lower().endswith('ap')]

        if not boards:
            return update.message.reply_text('No boardconfigs found for this device.')

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
            firmware = ctx.user_data['firmware'] = next(
                x for x in ctx.user_data['device']['firmwares'] if x['version'] == text
            )
        except StopIteration:
            return update.message.reply_text('Invalid input.')

        p = urllib.parse.urlparse(firmware['url'])

        if p.netloc == 'appldnld.apple.com':
            ctx.user_data['buildmanifest'] = pzb(update, ctx, firmware, 'BuildManifest.plist', 'BuildManifest')
        else:
            buildmanifest_url = urllib.parse.urlunparse(
                p._replace(path='/'.join([*p.path.split('/')[:-1], 'BuildManifest.plist']))
            )

            r = session.get(buildmanifest_url)

            if r.ok:
                ctx.user_data['buildmanifest'] = r.content
            else:
                ctx.user_data['buildmanifest'] = pzb(
                    update, ctx, ctx.user_data['firmware'], 'BuildManifest.plist', 'BuildManifest'
                )

        try:
            buildmanifest = plistlib.loads(ctx.user_data['buildmanifest'])
        except Exception:
            update.message.reply_text('Unable to parse BuildManifest, please try again later.')
            raise

        try:
            buildidentity = next(
                x for x in buildmanifest['BuildIdentities']
                if x['Info']['DeviceClass'].lower() == ctx.user_data['boardconfig'].lower()
            )

            if 'RestoreSEP' in buildidentity['Manifest']:
                sep_path = ctx.user_data['sep_path'] = buildidentity['Manifest']['RestoreSEP']['Info']['Path']
            else:
                sep_path = ctx.user_data['sep_path'] = None

            if 'BasebandFirmware' in buildidentity['Manifest']:
                bb_path = ctx.user_data['bb_path'] = buildidentity['Manifest']['BasebandFirmware']['Info']['Path']
            else:
                bb_path = ctx.user_data['bb_path'] = None
        except Exception:
            update.message.reply_text('Unable to get data from BuildManifest, please try again later.')
            raise

        try:
            update.message.reply_text(
                'Removing keyboard... (ignore this message)',
                reply_markup=ReplyKeyboardRemove(),
            ).delete()
        except Exception:
            pass

        update.message.reply_text(
            ('<b>{device} ({boardconfig}) - {firmware} ({buildid})</b>\n\n'
             '<b>SEP</b>: {sep_path}\n'
             '<b>Baseband</b>: {bb_path}').format(
                device=html.escape(ctx.user_data['device']['name']),
                boardconfig=html.escape(ctx.user_data['boardconfig']),
                firmware=html.escape(firmware['version']),
                buildid=html.escape(firmware['buildid']),
                sep_path=html.escape(str(sep_path)),
                bb_path=html.escape(str(bb_path)),
            ),
            parse_mode='html',
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("Download", callback_data="download"),
                ],
            ]),
        )
    else:
        update.message.reply_text('Invalid state. Please start over using /start.', reply_markup=ReplyKeyboardRemove())


def on_callback_query(update, ctx):
    if update.callback_query.data == 'download':
        zf = BytesIO()
        zf.name = f'sepbb_{ctx.user_data["boardconfig"]}_{ctx.user_data["firmware"]["buildid"]}.zip'

        with zipfile.ZipFile(zf, 'w') as zfd:
            buildmanifest = ctx.user_data['buildmanifest']
            zfd.writestr('BuildManifest.plist', buildmanifest)

            if ctx.user_data['sep_path']:
                sep = pzb(update, ctx, ctx.user_data['firmware'], ctx.user_data['sep_path'], 'SEP')
                if sep:
                    zfd.writestr(ctx.user_data['sep_path'], sep)

            if ctx.user_data['bb_path']:
                baseband = pzb(update, ctx, ctx.user_data['firmware'], ctx.user_data['bb_path'], 'baseband')
                if baseband:
                    zfd.writestr(ctx.user_data['bb_path'], baseband)

        zf.seek(0)
        update.message.reply_document(zf.read(), zf.name)

        ctx.bot.answer_callback_query(update.callback_query.id)


def show_firmware_menu(update, ctx):
    if 'device' not in ctx.user_data:
        return update.message.reply_text(
            'Invalid state. Please start over using /start.', reply_markup=ReplyKeyboardRemove(),
        )

    firmwares = [x for x in ctx.user_data['device']['firmwares'] if x.get('signed')]

    if not firmwares:
        return update.message.reply_text('No signed firmwares found for this device.')

    for firmware in firmwares:
        firmware['version'] = firmware['version'].replace('[', '').replace(']', '')

    firmwares = sorted(
        firmwares,
        key=lambda x: version.parse(
            x['version'].replace(' ', '').replace('Update', '+')
            + ('1' if x['version'].lower().endswith(('beta', 'rc', 'update')) else '')
        ),
    )

    keyboard = []
    for i, firmware in enumerate(firmwares):
        if i % 2 == 0:
            keyboard.append([])
        keyboard[-1].append(firmware['version'])

    update.message.reply_text(
        'Please select a version.\n(Only currently signed versions are shown.)',
        reply_markup=ReplyKeyboardMarkup(keyboard),
    )

    ctx.user_data['state'] = State.FIRMWARE


def pzb(update, ctx, firmware, file, name):
    update.message = update.message or update.callback_query.message

    update.message.reply_text(f'Extracting {name}, please wait...')

    with tempfile.TemporaryDirectory() as d:
        try:
            with RemoteZip(firmware['url']) as rzip:
                rzip.extract(file, d)
        except Exception as e:
            update.message.reply_text(
                f'Unable to extract {name} for the selected firmware, please try again later.'
            )
            log.exception(e)
            return

        return (Path(d) / file).read_bytes()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--debug', action='store_true', help='enable debug logging')
    args = parser.parse_args()

    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.DEBUG if args.debug else logging.INFO,
    )

    config = toml.load('config.toml')

    updater = Updater(config['token'], base_url=config.get('base_url'))
    dispatcher = updater.dispatcher

    session = requests.Session()

    dispatcher.add_handler(CommandHandler('start', sepbb))
    dispatcher.add_handler(CommandHandler('sep', sepbb))
    dispatcher.add_handler(CommandHandler('sepbb', sepbb))
    dispatcher.add_handler(MessageHandler(Filters.text, on_text))
    dispatcher.add_handler(CallbackQueryHandler(on_callback_query))

    updater.start_polling()
