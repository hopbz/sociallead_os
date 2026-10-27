from __future__ import annotations

import html
import logging
from typing import Any

import requests
from sqlalchemy.orm import Session

from app.config import Settings
from app.core.env_file import update_env_file
from app.db.models import AppSetting, ScrapedPost

logger = logging.getLogger(__name__)


TELEGRAM_ENABLED_KEY = 'telegram_enabled'
TELEGRAM_CHAT_ID_KEY = 'telegram_chat_id'


class TelegramConfigurationError(RuntimeError):
    pass


def get_telegram_settings(settings: Settings, db: Session) -> dict[str, object]:
    enabled_row = db.get(AppSetting, TELEGRAM_ENABLED_KEY)
    chat_id_row = db.get(AppSetting, TELEGRAM_CHAT_ID_KEY)
    enabled = settings.telegram_enabled
    if enabled_row:
        enabled = enabled_row.value.strip().lower() in {'1', 'true', 'yes', 'on'}
    chat_id = chat_id_row.value.strip() if chat_id_row else settings.telegram_chat_id.strip()
    return {
        'enabled': enabled,
        'chat_id': chat_id,
        'bot_token_configured': bool(settings.telegram_bot_token.strip()),
    }


def save_telegram_settings(
    db: Session,
    *,
    enabled: bool,
    chat_id: str,
    bot_token: str | None = None,
    persist_env: bool = False,
) -> None:
    values = {
        TELEGRAM_ENABLED_KEY: 'true' if enabled else 'false',
        TELEGRAM_CHAT_ID_KEY: chat_id.strip(),
    }
    for key, value in values.items():
        row = db.get(AppSetting, key)
        if row:
            row.value = value
        else:
            db.add(AppSetting(key=key, value=value))
    db.commit()

    if persist_env:
        env_values: dict[str, object] = {
            'TELEGRAM_ENABLED': enabled,
            'TELEGRAM_CHAT_ID': chat_id.strip(),
        }
        if bot_token is not None:
            env_values['TELEGRAM_BOT_TOKEN'] = bot_token.strip()
        update_env_file(env_values)


def send_telegram_message(settings: Settings, chat_id: str, text: str) -> None:
    if not settings.telegram_bot_token:
        raise TelegramConfigurationError('Telegram bot token chưa được cấu hình trong biến môi trường.')
    if not chat_id:
        raise TelegramConfigurationError('Telegram chat ID chưa được cấu hình.')

    bot_id = _telegram_bot_id(settings.telegram_bot_token)
    if bot_id and chat_id.strip() == bot_id:
        raise TelegramConfigurationError(
            'Telegram chat ID đang trùng với bot ID. Hãy mở bot trên Telegram, bấm Start hoặc gửi /start, '
            'sau đó bấm "Gửi thử" lại để hệ thống lấy chat ID thật.'
        )

    url = f'https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage'
    try:
        response = requests.post(url, json={
            'chat_id': chat_id,
            'text': text,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True,
        }, timeout=15)
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise TelegramConfigurationError(_telegram_error_message(response, chat_id)) from exc
    except requests.RequestException as exc:
        raise TelegramConfigurationError(f'Không thể kết nối Telegram API: {exc}') from exc


def resolve_latest_telegram_chat_id(settings: Settings) -> str | None:
    if not settings.telegram_bot_token:
        return None

    url = f'https://api.telegram.org/bot{settings.telegram_bot_token}/getUpdates'
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        logger.warning('Telegram getUpdates failed: %s', exc)
        return None

    result = payload.get('result') if isinstance(payload, dict) else None
    if not isinstance(result, list):
        return None

    for update in reversed(result):
        chat_id = _chat_id_from_update(update)
        if chat_id:
            return chat_id
    return None


def chat_id_matches_bot_id(settings: Settings, chat_id: str) -> bool:
    bot_id = _telegram_bot_id(settings.telegram_bot_token)
    return bool(bot_id and chat_id.strip() == bot_id)


def _telegram_bot_id(bot_token: str) -> str | None:
    token_id = bot_token.split(':', 1)[0].strip()
    return token_id if token_id.isdigit() else None


def _chat_id_from_update(update: Any) -> str | None:
    if not isinstance(update, dict):
        return None
    for key in ('message', 'edited_message', 'channel_post', 'my_chat_member'):
        item = update.get(key)
        if not isinstance(item, dict):
            continue
        chat = item.get('chat') if key != 'my_chat_member' else item.get('chat')
        if isinstance(chat, dict) and chat.get('id') is not None:
            return str(chat['id'])
    return None


def _telegram_error_message(response: requests.Response, chat_id: str) -> str:
    description = ''
    try:
        payload = response.json()
        if isinstance(payload, dict):
            description = str(payload.get('description') or '')
    except ValueError:
        description = response.text[:180]

    lower_description = description.lower()
    if response.status_code == 403:
        if 'bot was blocked' in lower_description:
            return 'Telegram không gửi được vì người nhận đã chặn bot. Hãy mở bot và bấm Start lại.'
        if "can't initiate conversation" in lower_description:
            return 'Telegram không gửi được vì bạn chưa bấm Start với bot. Hãy mở bot, gửi /start, rồi bấm Gửi thử lại.'
        return (
            'Telegram từ chối gửi tin tới chat ID hiện tại. Hãy kiểm tra chat ID, đảm bảo bạn đã bấm Start với bot '
            'hoặc đã thêm bot vào group/channel.'
        )
    if response.status_code in {400, 404}:
        return f'Telegram chat ID không hợp lệ hoặc bot không thấy chat này: {chat_id}.'
    if response.status_code == 401:
        return 'Telegram bot token không hợp lệ. Hãy tạo/copy lại token từ BotFather rồi lưu lại.'
    return f'Telegram API trả về lỗi HTTP {response.status_code}: {description or "không rõ nguyên nhân"}.'


def send_telegram_posts(settings: Settings, posts: list[ScrapedPost], db: Session) -> None:
    if not posts:
        return
    telegram = get_telegram_settings(settings, db)
    if not telegram['enabled']:
        return
    chat_id = str(telegram['chat_id'])
    if not telegram['bot_token_configured'] or not chat_id:
        logger.warning('Telegram is enabled but token/chat_id is missing')
        return

    for post in posts[:30]:
        text = (
            '🚀 <b>SocialLead OS - Facebook Group Match</b>\n\n'
            f'<b>Group:</b> {html.escape(post.group_name or post.group_url)}\n'
            f'<b>Keyword:</b> {html.escape(post.matched_keywords or "") }\n'
            f'<b>Author:</b> {html.escape(post.author or "Unknown")}\n\n'
            f'{html.escape(post.content[:900])}\n\n'
            f'{html.escape(post.post_url or post.group_url)}'
        )
        try:
            send_telegram_message(settings, chat_id, text)
        except Exception as exc:
            logger.exception('Telegram send failed: %s', exc)
