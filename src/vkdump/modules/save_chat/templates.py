from pathlib import Path
from string import Template

ROOT = Path(__file__).with_suffix('')

LABELS = {
    'en': {'messages':'Messages','empty':'No messages','partial':'Partial history','saved':'Saved',
           'edited':'edited','reply':'Reply','forward':'Forwarded message','missing':'File not downloaded',
           'unknown_author':'Unknown author','you':'You','page':'Page','unavailable':'Message unavailable',
           'source':'Open attachment','exported':'Exported','event':'Chat event',
           'photo':'Photo','photos':'Photos','video':'Video','audio':'Audio','file':'File','link':'Link','sticker':'Sticker',
           'unknown':'Attachment','geo':'Location','wall_post':'Wall post','wall_comment':'Wall comment',
           'poll':'Poll','story':'Story','gift':'Gift','playlist':'Playlist','market_item':'Market item',
           'photo_album':'Photo album','call':'Call','graffiti':'Graffiti','audio_message':'Voice message'},
    'ru': {'messages':'Сообщения','empty':'Нет сообщений','partial':'Неполная история','saved':'Сохранено',
           'edited':'изменено','reply':'Ответ','forward':'Пересланное сообщение','missing':'Файл не скачан',
           'unknown_author':'Неизвестный автор','you':'Вы','page':'Страница','unavailable':'Сообщение недоступно',
           'source':'Открыть вложение','exported':'Экспортировано','event':'Событие чата',
           'photo':'Фотография','photos':'Фотографии','video':'Видео','audio':'Аудиозапись','file':'Файл','link':'Ссылка','sticker':'Стикер',
           'unknown':'Вложение','geo':'Местоположение','wall_post':'Запись на стене','wall_comment':'Комментарий',
           'poll':'Опрос','story':'История','gift':'Подарок','playlist':'Плейлист','market_item':'Товар',
           'photo_album':'Фотоальбом','call':'Звонок','graffiti':'Граффити','audio_message':'Голосовое сообщение'},
}
EVENTS = {
    'en': {'leave':'left the chat','rejoin':'rejoined the chat','join_by_link':'joined via link','invite':'invited',
           'kick':'removed','chat_create':'created the chat','chat_rename':'renamed the chat',
           'chat_photo_set':'updated the chat photo','chat_photo_removed':'removed the chat photo',
           'message_pin':'pinned a message','message_unpin':'unpinned a message','screenshot':'took a screenshot',
           'call_start':'started a call','chat_theme_change':'changed the theme','chat_theme_reset':'reset the theme'},
    'ru': {'leave':'покинул(а) чат','rejoin':'вернулся(-ась) в чат','join_by_link':'вступил(а) по ссылке',
           'invite':'пригласил(а)','kick':'исключил(а)','chat_create':'создал(а) чат','chat_rename':'переименовал(а) чат',
           'chat_photo_set':'обновил(а) фото чата','chat_photo_removed':'удалил(а) фото чата',
           'message_pin':'закрепил(а) сообщение','message_unpin':'открепил(а) сообщение',
           'screenshot':'сделал(а) снимок экрана','call_start':'начал(а) звонок','chat_theme_change':'изменил(а) тему',
           'chat_theme_reset':'сбросил(а) тему'},
}


def message_count(count, lang):
    if lang == 'ru':
        suffix = 'сообщений'
        if not 11 <= count % 100 <= 14:
            if count % 10 == 1:
                suffix = 'сообщение'
            elif 2 <= count % 10 <= 4:
                suffix = 'сообщения'
    else:
        suffix = 'message' if count == 1 else 'messages'
    return f'{count} {suffix}'


def attached_message_count(count, lang):
    number, noun = message_count(count, lang).split(' ', 1)
    adjective = ('прикреплённое' if noun == 'сообщение' else 'прикреплённых') if lang == 'ru' else 'attached'
    return f'{number} {adjective} {noun}'


def render(name, **values):
    return Template((ROOT / name).read_text(encoding='utf-8')).substitute(values)
