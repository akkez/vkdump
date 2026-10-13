from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone

from .models import ParsedAttachment, ParsedMessage


def save_profiles(conn, response):
    from ...core.message_store import upsert_user
    for p in response.get('profiles', []):
        name = ' '.join(p.get(k,'') for k in ('first_name','last_name')).strip() or None
        upsert_user(conn,int(p['id']),name,p.get('photo_100'))
    for g in response.get('groups', []):
        upsert_user(conn,-int(g['id']),g.get('name'),g.get('photo_100'))


def timestamp(value):
    return datetime.fromtimestamp(int(value or 0),timezone.utc).replace(tzinfo=None)


def _image(images):
    images = [i for i in images if i.get('url')]
    return max(images,key=lambda i:int(i.get('width',0))*int(i.get('height',0)),default={})


def normalize_message(item, owner, names, *, depth=0):
    sender = item.get('from_id')
    reply = item.get('reply_message')
    forwards = item.get('fwd_messages') or []
    m = ParsedMessage(vk_message_id=int(item.get('id') or 0),sent_at=timestamp(item.get('date')),
                      sender_vk_id=sender,sender_display_name=names.get(sender),sender_is_self=sender==owner,
                      text=item.get('text') or '',is_reply=bool(reply),reply_to_message_id=reply.get('id') if reply else None,
                      has_forwards=bool(forwards),forwarded_count=len(forwards),is_edited=bool(item.get('update_time')),
                      edited_at=timestamp(item['update_time']) if item.get('update_time') else None,
                      conversation_message_id=item.get('conversation_message_id'),source_kind='api')
    def nested(raw):
        if depth >= 20:
            return {'text': raw.get('text') or '', 'attachments': []}
        data = asdict(normalize_message(raw,owner,names,depth=depth+1))
        data['sent_at'] = data['sent_at'].isoformat(' ')
        if data['edited_at']:
            data['edited_at'] = data['edited_at'].isoformat(' ')
        return data
    if reply:
        m.reply_data = nested(reply)
    for raw in item.get('attachments') or []:
        type_ = raw.get('type') or 'unknown'
        data = raw.get(type_) or {}
        kind = {'doc':'file','wall':'wall_post','market':'market_item','wall_reply':'wall_comment','audio_playlist':'playlist','album':'photo_album'}.get(type_,type_)
        url = data.get('url')
        metadata = {k:data[k] for k in ('title','artist','duration','width','height','latitude','longitude') if k in data}
        if kind == 'photo':
            image = _image(data.get('sizes',[]));url=image.get('url')
            metadata.update({k:image[k] for k in ('width','height') if k in image})
        if kind == 'sticker':
            image=_image(data.get('images',[]));url=image.get('url')
        if kind == 'video' and not url and data.get('id') is not None:
            url=f"https://vk.com/video{data.get('owner_id',0)}_{data['id']}"
        if kind == 'wall_post' and not url and data.get('id') is not None:
            url=f"https://vk.com/wall{data.get('owner_id',0)}_{data['id']}"
        identity = str(data.get('id') or data.get('sticker_id') or '')
        if identity and data.get('owner_id') is not None:
            identity=f"{data['owner_id']}_{identity}"
        supported = {'photo','video','audio','file','link','sticker','wall_post','wall_comment','poll','story','gift','playlist','market_item','photo_album','call','graffiti','audio_message'}
        if kind not in supported:
            metadata = {'original_type': type_, 'payload': data}
            kind = 'unknown'
        if kind != 'unknown':
            consumed={'id','owner_id','access_key','url','title','artist','duration','width','height',
                      'latitude','longitude','sizes','images','sticker_id','date','size','ext'}
            extra={key:value for key,value in data.items() if key not in consumed}
            if extra:
                metadata['extra']=extra
                metadata['unrendered_fields']=sorted(extra)
            if 'size' in data:
                metadata['declared_size']=data['size']
            if 'ext' in data:
                metadata['file_ext']=data['ext']
        m.attachments.append(ParsedAttachment(kind=kind,description=data.get('title'),url=url,data=metadata or None,
                                              external_id=identity or None,access_key=data.get('access_key')))
    if item.get('geo'):
        m.attachments.append(ParsedAttachment(kind='geo',description=None,data=item['geo']))
    action = item.get('action')
    if action:
        subtype = {'chat_invite_user':'invite','chat_kick_user':'kick','chat_create':'chat_create',
                   'chat_title_update':'chat_rename','chat_photo_update':'chat_photo_set','chat_photo_remove':'chat_photo_removed',
                   'chat_pin_message':'message_pin','chat_unpin_message':'message_unpin','chat_invite_user_by_link':'join_by_link',
                   'chat_screenshot':'screenshot'}.get(action.get('type'),action.get('type','unknown'))
        def user(vk_id):
            return {'vk_id':vk_id,'display_name':names.get(vk_id) or str(vk_id),
                    'profile_url':f'https://vk.com/{"club" if vk_id < 0 else "id"}{abs(vk_id)}'}
        payload = {'subtype':subtype,'actor':user(sender) if sender else {'display_name':'','profile_url':''}}
        if action.get('member_id'):
            payload['target'] = user(int(action['member_id']))
        if action.get('text'):
            payload['title_after'] = action['text']
        m.attachments.append(ParsedAttachment(kind='chat_event',description=None,data=payload))
    for forwarded in forwards:
        m.attachments.append(ParsedAttachment(kind='forward',description=None,forward_count=1,data={'messages':[nested(forwarded)]}))
    for position,a in enumerate(m.attachments):
        a.position = position
    return m
