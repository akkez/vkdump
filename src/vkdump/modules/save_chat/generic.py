from __future__ import annotations

import hashlib
import json
import math
import shutil
from itertools import groupby
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ...core.db import app_dir, connection
from . import templates
from .index import find_slug_for_chat, upsert
from .pipeline import AttMeta, TransformContext
from .photos_page import render as render_photos_page
from .transforms import inline_photo_html

PAGE_SIZE = 50


def safe_url(raw):
    if not raw:
        return ''
    try:
        url = urlsplit(str(raw))
    except ValueError:
        return ''
    if url.scheme.lower() not in ('https','http') or not url.netloc or url.username or url.password:
        return ''
    sensitive = {'access_token','access_key','token','oauth','authorization','key'}
    query = urlencode([(k,v) for k,v in parse_qsl(url.query,keep_blank_values=True) if k.lower() not in sensitive])
    return urlunsplit((url.scheme,url.netloc,url.path,query,''))


def diagnostic(code, **fields):
    data=json.dumps({'code':code,**fields},ensure_ascii=False)
    data=data.replace('--','- -').replace('<',r'\u003c').replace('>',r'\u003e')
    return f'<!-- {data} -->'


def _link(url, label):
    url = safe_url(url)
    return f'<a href="{escape(url,quote=True)}">{escape(str(label))}</a>' if url else escape(str(label))


def _data(row):
    if isinstance(row.get('data'),dict):
        return row['data']
    try:
        return json.loads(row.get('data_json') or '{}')
    except (TypeError,ValueError):
        return {}


def _date(raw, lang):
    dt = datetime.fromisoformat(str(raw))
    ru = ('янв','фев','мар','апр','май','июн','июл','авг','сен','окт','ноя','дек')
    en = ('Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec')
    if lang == 'ru':
        return f'{dt.day} {ru[dt.month-1]} {dt.year} в {dt:%H:%M:%S}'
    hour = dt.hour % 12 or 12
    return f'at {hour}:{dt:%M:%S} {"pm" if dt.hour >= 12 else "am"} on {dt.day} {en[dt.month-1]} {dt.year}'


def _quote(kind, heading, content):
    return (
        f'<div class="attachment {kind}"><div class="attachment__description">{heading}</div>'
        f'<div style="margin:8px 0 8px 12px">{content}</div></div>'
    )


class Renderer:
    def __init__(self,lang,chat_out,reply_pages,local_by_url):
        self.lang=lang
        self.labels=templates.LABELS[lang]
        self.chat_out=chat_out
        self.reply_pages=reply_pages
        self.local_by_url=local_by_url
        self.copied={}
        self.static_root=(app_dir()/'data'/'static').resolve()

    def local_file(self,row):
        row = row if row.get('local_path') else self.local_by_url.get(row.get('url'),row)
        if row.get('download_status') != 'ok' or not row.get('local_path'):
            return ''
        source=(self.static_root/row['local_path']).resolve()
        if not source.is_relative_to(self.static_root) or not source.is_file():
            return ''
        if source in self.copied:
            return self.copied[source]
        stem=hashlib.sha256(str(source).encode()).hexdigest()[:20]
        destination=self.chat_out/'assets'/f'{stem}{source.suffix.lower()}'
        destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(source,destination)
        relative='../assets/'+destination.name
        self.copied[source]=relative
        return relative

    def attachment(self,row,depth):
        kind=row['kind'];data=_data(row);label=self.labels.get(kind,self.labels['unknown'])
        if kind == 'forward':
            nested=''.join(self.message(m,nested=True,depth=depth+1) for m in data.get('messages',[]))
            count=row.get('forward_count') or len(data.get('messages',[])) or 1
            return _quote('forward',templates.attached_message_count(count,self.lang),nested or diagnostic('forward_body_unavailable'))
        if kind == 'chat_event':
            if data.get('subtype') not in templates.EVENTS[self.lang]:
                return diagnostic('unknown_chat_event')
            actor=escape(str((data.get('actor') or {}).get('display_name') or ''))
            text=templates.EVENTS[self.lang].get(data.get('subtype'),self.labels['event'])
            target=escape(str((data.get('target') or {}).get('display_name') or ''))
            title=escape(str(data.get('title_after') or data.get('theme') or ''))
            return f'<div class="attachment chat-event">{actor} {text} {target} {title}</div>'
        if kind == 'unknown':
            caption=escape(str(row.get('description') or data.get('title') or ''))
            source=_link(row.get('url'),urlsplit(safe_url(row.get('url'))).netloc) if safe_url(row.get('url')) else ''
            return diagnostic('unknown_attachment')+(f'<div class="attachment">{caption}{source}</div>' if caption or source else '')
        caption=escape(str(row.get('description') or data.get('title') or label))
        local=self.local_file(row)
        source=_link(row.get('url'),urlsplit(safe_url(row.get('url'))).netloc) if safe_url(row.get('url')) else ''
        body=''
        if local:
            path=escape(local,quote=True)
            if kind in ('photo','sticker','graffiti'):
                stored=row if row.get('local_path') else self.local_by_url.get(row.get('url'),row)
                resolution=stored.get('resolution')
                if not resolution and data.get('width') and data.get('height'):
                    resolution=f'{data["width"]}x{data["height"]}'
                meta=AttMeta(stored.get('local_path') or '',resolution,stored.get('file_size'))
                return '<div class="attachment">'+inline_photo_html(local,row.get('description') or label,meta,safe_url(row.get('url')))+ '</div>'
            elif kind in ('audio','audio_message','video'):
                tag='video' if kind=='video' else 'audio'
                body=f'<{tag} src="{path}" controls preload="none"></{tag}>'
            else:
                body=f'<a href="{path}">{caption}</a>'
        elif kind in ('photo','video','audio','file','sticker','graffiti','audio_message'):
            body=diagnostic('missing_file')
        elif kind == 'geo':
            coords=data.get('coordinates') or {}
            if isinstance(coords,dict):
                body=escape(', '.join(str(coords[k]) for k in ('latitude','longitude') if k in coords))
        return f'<div class="attachment"><div class="attachment__description">{caption}</div>{body}{source}</div>'

    def render_attachments(self,rows,depth):
        rendered=[]
        for forwarded,group in groupby(rows,key=lambda row:row['kind']=='forward'):
            if not forwarded:
                rendered.extend(self.attachment(row,depth) for row in group)
                continue
            messages=[];count=0
            for row in group:
                nested=_data(row).get('messages',[])
                messages.extend(nested)
                count+=row.get('forward_count') or len(nested) or 1
            rendered.append(self.attachment({'kind':'forward','forward_count':count,'data':{'messages':messages}},depth))
        return ''.join(rendered)

    def message(self,m,*,nested=False,depth=0):
        if depth > 20:
            return diagnostic('nested_content_unavailable')
        name=m.get('sender_display_name') or (self.labels['you'] if m.get('sender_is_self') else '')
        sender=m.get('sender_vk_id')
        author=_link(f'https://vk.com/{"club" if sender < 0 else "id"}{abs(sender)}',name) if sender else escape(name)
        edited=f'<span class="edited">({self.labels["edited"]})</span>' if m.get('is_edited') else ''
        reply=''
        target=m.get('reply_to_message_id')
        preview=m.get('reply_data')
        if preview is None and m.get('reply_data_json'):
            preview=json.loads(m['reply_data_json'])
        if m.get('is_reply'):
            page=self.reply_pages.get(target)
            reply_label='Ответ на сообщение' if self.lang=='ru' else 'Reply to message'
            heading=f'<a class="attachment__link" href="messages{page}.html#m{target}">{reply_label}</a>' if page is not None else reply_label
            content=self.message(preview,nested=True,depth=depth+1) if preview else (diagnostic('reply_target_unavailable') if page is None else '')
            reply=_quote('reply',heading,content)
        body=escape(m.get('text') or '').replace('\n','<br>')
        attachments=self.render_attachments(m.get('attachments',[]),depth)
        date=_date(m['sent_at'],self.lang) if m.get('sent_at') else ''
        if nested:
            return f'<div class="message nested-message" style="margin:6px 0"><div class="message__header">{author}, {date}{edited}</div><div>{reply}{body}<div class="kludges">{attachments}</div></div></div>'
        return templates.render('message.html',id=m['vk_message_id'],sender=author,date=date,edited=edited,text=body,reply=reply,attachments=attachments)


def _crumb(href,text):
    return f'<a class="ui_crumb" href="{href}">{escape(text)}</a><div class="ui_crumb_sep"></div>'


def _page(lang,title,stylesheet,breadcrumbs,content,pagination='',footer=''):
    return templates.render('page.html',lang=lang,title=escape(title),stylesheet=stylesheet,breadcrumbs=breadcrumbs,content=content,pagination=pagination,footer=footer)


def write_indexes(output_dir, manifest, lang):
    labels=templates.LABELS[lang]
    shutil.copyfile(templates.ROOT/'style.css',output_dir/'messages.css')
    rows=[]
    for c in manifest.chats:
        photos_link=''
        if c.photos_page:
            href=escape(f'../{c.chat_slug}/{c.photos_page}',quote=True)
            photos_link=f'<div><a class="photos-link" href="{href}">{labels["photos"]} ({c.photos_count})</a></div>'
        rows.append(templates.render('dialog.html',id=c.chat_id,href=escape(f'../{c.chat_slug}/{c.first_page}',quote=True),
                                     title=escape(c.title),message_count=templates.message_count(c.message_count,lang),photos_link=photos_link))
    directory=output_dir/'messages';directory.mkdir(exist_ok=True)
    (directory/'index-messages.html').write_text(templates.render('dialogs.html',lang=lang,messages=labels['messages'],
        content=''.join(rows) or diagnostic('empty_history')),encoding='utf-8')
    (output_dir/'index.html').write_text(templates.render('index.html',lang=lang,messages=labels['messages']),encoding='utf-8')


def pagination(page, total, page_size):
    if total <= 1:
        return ''
    first=max(0,page-2)
    last=min(total-1,page+2)
    links=[]
    def link(index,label):
        class_name='pg_lnk_sel' if index==page else 'pg_lnk'
        return templates.render('page-link.html',class_name=class_name,href=f'messages{index*page_size}.html',label=label)
    if first > 0:
        links.append(link(0,'&laquo;'))
    links.extend(link(index,str(index+1)) for index in range(first,last+1))
    if last < total-1:
        links.append(link(total-1,'&raquo;'))
    return templates.render('pagination.html',links=''.join(links))


def render_chat(*,chat_meta,output_dir,progress,lang,page_size=PAGE_SIZE,extra_issues=()):
    from .run import _chat_folder_slug
    if lang not in templates.LABELS:
        raise ValueError('Export language must be ru or en.')
    page_size=int(page_size)
    if not 1 <= page_size <= 1000:
        raise ValueError('Page size must be 1..1000.')
    chat_id=chat_meta['id']
    slug=find_slug_for_chat(output_dir,chat_id) or _chat_folder_slug(chat_id,chat_meta.get('title'))
    chat_out=output_dir/slug;pages_dir=chat_out/'messages';pages_dir.mkdir(parents=True,exist_ok=True)
    labels=templates.LABELS[lang]
    with connection() as conn:
        ids=[r[0] for r in conn.execute('SELECT vk_message_id FROM messages WHERE chat_id=? ORDER BY sent_at DESC,vk_message_id DESC',(chat_id,))]
        reply_pages={mid:(i//page_size)*page_size for i,mid in enumerate(ids)}
        pages=max(1,math.ceil(len(ids)/page_size))
        local_by_url={r['url']:dict(r) for r in conn.execute('SELECT a.* FROM attachments a JOIN messages m ON m.id=a.message_id WHERE m.chat_id=? AND a.download_status=\'ok\' AND a.url IS NOT NULL',(chat_id,))}
        renderer=Renderer(lang,chat_out,reply_pages,local_by_url)
        from .issues import collect
        issues = collect(conn,chat_meta,renderer,extra_issues)
        (chat_out/'_issues.json').write_text(json.dumps({'chat_id':chat_id,'issues':issues},ensure_ascii=False,indent=2),encoding='utf-8')
        summary = dict.fromkeys(i['code'] for i in issues)
        if issues:
            progress.log(f'save-chat: {len(issues)} flagged fields → {chat_out / "_issues.json"}')
        cursor=conn.execute('SELECT m.*,COALESCE(NULLIF(u.display_name,\'\'),m.sender_display_name) AS author_name FROM messages m LEFT JOIN users u ON u.id=m.sender_user_id WHERE m.chat_id=? ORDER BY m.sent_at DESC,m.vk_message_id DESC',(chat_id,))
        for page in range(pages):
            progress.check_cancelled()
            batch=[dict(r) for r in cursor.fetchmany(page_size)]
            attachments={}
            if batch:
                keys=[r['id'] for r in batch]
                for a in conn.execute(f'SELECT * FROM attachments WHERE message_id IN ({",".join("?" for _ in keys)}) ORDER BY position',keys):
                    attachments.setdefault(a['message_id'],[]).append(dict(a))
            content=[]
            if issues:
                content.append(diagnostic('export_issues',count=len(issues),kinds=list(summary),report='../_issues.json'))
            if chat_meta.get('history_status') != 'complete':
                expected=chat_meta.get('total_expected_count')
                content.append(diagnostic('partial_history',saved=len(ids),expected=expected))
            for m in batch:
                m['sender_display_name']=m.pop('author_name')
                m['attachments']=attachments.get(m['id'],[])
                content.append(renderer.message(m))
            if not batch:
                content.append(diagnostic('empty_history'))
            navigation=pagination(page,pages,page_size)
            title=chat_meta.get('title') or str(chat_meta.get('peer_id') or chat_id)
            crumbs=_crumb('../../index.html','VK Dump')+_crumb('../../messages/index-messages.html',labels['messages'])+f'<div class="ui_crumb">{escape(title)}</div>'
            html=_page(lang,title,'../../messages.css',crumbs,''.join(content),navigation)
            (pages_dir/f'messages{page*page_size}.html').write_text(html,encoding='utf-8')
            progress.report(page+1,pages,f'{labels["page"]} {page+1}/{pages}')
    for old in pages_dir.glob('messages*.html'):
        if old.stem[8:].isdigit() and old.name not in {f'messages{n*page_size}.html' for n in range(pages)}:
            old.unlink()
    ctx=TransformContext(
        chat_id=chat_id,chat_source_folder=chat_meta.get('source_folder') or '',
        output_chat_dir=chat_out,pages_dir=pages_dir,static_root=renderer.static_root,
        url_to_meta={},log=progress.log,
        copied_assets={source:(pages_dir / relative).resolve() for source,relative in renderer.copied.items()},
    )
    photos_page,photos_count=render_photos_page(
        chat_meta,ctx,first_page='messages0.html',
        message_pages={mid:f'messages{offset}.html' for mid,offset in reply_pages.items()},
    )
    if photos_page:
        progress.log(f'save-chat: gallery — {photos_count} photos → {photos_page}')
    manifest=upsert(output_dir=output_dir,chat_slug=slug,peer_id=str(chat_meta.get('peer_id') or ''),title=chat_meta.get('title') or slug,
                    type_=chat_meta.get('type') or '?',message_count=len(ids),first_page='messages/messages0.html',chat_id=chat_id,
                    photos_page=photos_page or '',photos_count=photos_count)
    write_indexes(output_dir,manifest,lang)
    return {'chat_id':chat_id,'chat_slug':slug,'peer_id':str(chat_meta.get('peer_id') or ''),'pages':pages,'blocks':len(ids),
            'output_dir':str(output_dir),'chat_dir':str(chat_out),'assets_copied':len(ctx.copied_assets),'photos_page':photos_page or '',
            'photos_count':photos_count,'language':lang,'renderer':'database','flagged_fields':len(issues),'issues_report':str(chat_out/'_issues.json')}
