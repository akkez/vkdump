from __future__ import annotations

import base64
import json
import shutil
from contextlib import contextmanager
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from vkdump.core import app_config, db, orchestrator, stats_views
from vkdump.core.api_store import persist_messages
from vkdump.core.message_store import upsert_chat, write_messages
from vkdump.modules import download_messages as download, parse_dump, save_chat, stats
from vkdump.parsers.vk import decode_dump_bytes
from vkdump.parsers.vk.messages import parse_page
from vkdump.parsers.vk.models import ParsedAttachment, ParsedMessage
from vkdump.tasks.registry import _chat_scope_choices, get_task
from .test_download_messages import FakeVK, Progress, run_fake, store


class Reporter(Progress):
    def hide_main(self):
        pass

    @contextmanager
    def sub(self, *args, **kwargs):
        yield self


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links=[]
        self.ids=set()

    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if attrs.get('id'):
            self.ids.add(attrs['id'])
        self.links.extend(attrs[k] for k in ('href','src') if attrs.get(k))


class Elements(HTMLParser):
    def __init__(self,html):
        super().__init__()
        self.nodes=[]
        self.stack=[]
        self.feed(html)

    def handle_starttag(self,tag,attrs):
        node={'tag':tag,'attrs':dict(attrs),'parents':self.stack[:],'text':[]}
        self.nodes.append(node)
        if tag not in ('meta','link','img','br','input','hr','source'):
            self.stack.append(node)

    def handle_endtag(self,tag):
        for i in range(len(self.stack)-1,-1,-1):
            if self.stack[i]['tag']==tag:
                del self.stack[i:]
                break

    def handle_data(self,data):
        for node in self.stack:
            node['text'].append(data)

    def with_class(self,name):
        return [n for n in self.nodes if name in n['attrs'].get('class','').split()]


def assert_portable(output):
    for page in output.rglob('*.html'):
        text=page.read_text(encoding='utf-8')
        assert '<meta charset="utf-8">' in text
        assert 'windows-1251' not in text
        assert not any(word in text for word in ('access_token','access_key','secret-token','secret-key','javascript:'))
        parsed=Links();parsed.feed(text)
        for link in parsed.links:
            url=urlsplit(link)
            if url.scheme:
                continue
            target=(page.parent/unquote(url.path)).resolve()
            assert target.is_relative_to(output.resolve()),link
            assert target.is_file(),link
            if url.fragment and target.suffix=='.html':
                destination=Links();destination.feed(target.read_text())
                assert url.fragment in destination.ids,link


def basic(mid,peer=10,**extra):
    return {'id':mid,'conversation_message_id':mid+500,'peer_id':peer,'from_id':10,'date':1700000000+mid,
            'text':f'Сообщение {mid} 🌍','attachments':[],**extra}


@pytest.mark.parametrize('lang,label,edited', [('ru','Сообщения','изменено'),('en','Messages','edited')])
def test_download_to_save_chat_task_is_portable_html(store,monkeypatch,tmp_path,lang,label,edited):
    history=[basic(mid) for mid in range(205,0,-1)]
    history[0].update(text='<script>alert("x")</script> Привет 👋',update_time=1700001000,
        reply_message=basic(1),fwd_messages=[basic(999,text='Вложенная пересылка',fwd_messages=[basic(998,text='Глубже')])],
        attachments=[{'type':'photo','photo':{'id':12,'owner_id':10,'access_key':'secret-key',
                     'sizes':[{'url':'https://cdn.example/photo?access_token=secret-token','width':640,'height':480}]}},
                     {'type':'doc','doc':{'id':13,'owner_id':10,'title':'Документ <x>','url':'https://cdn.example/doc'}},
                     {'type':'future','future':{'access_key':'secret-key','payload':'raw-secret'}}])
    history[1].update(from_id=-11,action={'type':'chat_title_update','text':'Название <новое>'})
    fake=FakeVK({10:history,-11:[],12:[]})
    monkeypatch.setattr(download,'VKApi',lambda token:fake)
    monkeypatch.setattr(orchestrator,'configure_logging',lambda:None)
    outcome=orchestrator.execute(get_task('download-messages'),{},Reporter())
    assert outcome.status=='success',outcome.error
    assert outcome.result['messages']==205
    with db.connection() as conn:
        chat=conn.execute("SELECT * FROM chats WHERE peer_id='10'").fetchone()
        assert chat['account_id']==1 and chat['source_folder'] is None
        assert chat['message_count']==205 and chat['history_status']=='complete'
        row=conn.execute('SELECT * FROM messages WHERE vk_message_id=205').fetchone()
        assert row['conversation_message_id']==705 and row['reply_to_message_id']==1
        assert row['source_file'] is None and row['raw_html'] is None
        assert row['sent_at']==datetime.utcfromtimestamp(1700000205)
        assert conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]==205
        assert conn.execute("SELECT display_name FROM users WHERE vk_id=-11").fetchone()[0]=='Bot or community'
        photo=conn.execute("SELECT id FROM attachments WHERE kind='photo'").fetchone()[0]
    static=tmp_path/'app'/'data'/'static';static.mkdir(parents=True)
    (static/'photo.jpg').write_bytes(b'local-photo')
    monkeypatch.setattr('vkdump.modules.save_chat.generic.app_dir',lambda:tmp_path/'app')
    with db.connection() as conn:
        conn.execute("UPDATE attachments SET local_path='photo.jpg',download_status='ok' WHERE id=?",(photo,))
    run_fake(monkeypatch,fake)
    with db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]==205
        assert conn.execute('SELECT COUNT(*) FROM chats').fetchone()[0]==3
        assert conn.execute('SELECT download_status,local_path FROM attachments WHERE id=?',(photo,)).fetchone()['download_status']=='ok'
    assert str(chat['id']) in [value for value,_ in _chat_scope_choices()]
    rollup,hidden=stats_views.chats_rollup(0)
    assert any(r['id']==chat['id'] and r['message_count']==205 for r in rollup)
    assert stats.run({},Reporter())
    output=tmp_path/lang
    outcome=orchestrator.execute(get_task('save-chat'),{'chat':str(chat['id']),'output':output,'language':lang,'page_size':200},Reporter())
    assert outcome.status=='success',outcome.error
    result=outcome.result
    assert result['renderer']=='database' and result['pages']==2 and result['blocks']==205
    first=Path(result['chat_dir'])/'messages'/'messages0.html'
    html=first.read_text()
    assert f'<html lang="{lang}">' in html and label in html and edited in html
    assert '&lt;script&gt;' in html and '<script>' not in html
    assert 'messages200.html#m1' in html
    assert 'Вложенная пересылка' in html and 'Глубже' in html and 'Название &lt;новое&gt;' in html
    assert html.count('id="m205"')==1
    assert '<img loading="lazy" src="../assets/' in html
    assert not any(x in html for x in ('⚠','Файл не скачан','File not downloaded','Файл вложения недоступен','date-divider',' · UTC','class="status"','class="empty"'))
    assert 'raw-secret' not in html
    issues=json.loads(Path(result['issues_report']).read_text())['issues']
    assert any(i['code']=='unknown_attachment' for i in issues)
    assert any(i['code']=='missing_file' for i in issues)
    assert not any(i['code']=='reply_target_unavailable' for i in issues)
    for file in (output/'index.html',output/'messages'/'index-messages.html'):
        text=file.read_text()
        assert label in text
        assert not any(x in text for x in ('likes','profile/','Friends','Друзья','payments','Лайки'))
    moved=tmp_path/'moved';shutil.copytree(output,moved)
    assert_portable(moved)
    again=save_chat.run({'chat':str(chat['id']),'output':output,'language':lang,'page_size':200},Reporter())
    assert again['pages']==2
    assert len(json.loads((output/'_export.json').read_text())['chats'])==1


def archive_html(owner,mid=1,body='Архивный текст',charset='utf-8'):
    jd=base64.b64encode(json.dumps({'user_id':owner}).encode()).decode()
    return f'''<html><head><meta charset="{charset}"><meta name="jd" content="{jd}"><link rel="stylesheet" href="../../style.css"></head><body>
<div class="ui_crumb">Диалог</div><div class="message" data-id="{mid}" id="m{mid}"><div class="message__header"><a href="https://vk.com/id10">Автор</a>, 14 ноя 2023 в 22:13:21</div><div>{body}<div class="kludges"></div></div></div></body></html>'''


def make_archive(tmp_path,owner=42,mid=1,charset='utf-8'):
    path=tmp_path/'archive';page=path/'messages'/'10'/'messages0.html';page.parent.mkdir(parents=True)
    html=archive_html(owner,mid,charset=charset)
    page.write_bytes(html.encode('cp1251' if charset=='windows-1251' else 'utf-8'))
    (path/'index.html').write_text('<html><head><meta charset="utf-8"></head><body>VK</body></html>')
    (path/'style.css').write_text('body { color: #222; }')
    return path


@pytest.mark.parametrize('first',['api','archive'])
def test_overlapping_sources_share_identity_and_accounts_stay_separate(store,monkeypatch,tmp_path,first):
    archive=make_archive(tmp_path)
    api=lambda:persist_messages(1,[basic(1,date=1699989201,text='',fwd_messages=[basic(777,text='forward')])])
    html=lambda:parse_dump.run({'source':archive,'source_timezone':'Europe/Moscow'},Reporter())
    if first=='api':
        api();html()
    else:
        html();api()
    html();api()
    with db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM chats').fetchone()[0]==1
        assert conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]==1
        row=conn.execute('SELECT * FROM messages').fetchone()
        assert row['text']=='Архивный текст'
        assert row['sent_at']==datetime(2023,11,14,19,13,21)
        assert row['source_kind']=='mixed'
        chat_id=row['chat_id']
        assert conn.execute('SELECT COUNT(*) FROM attachments').fetchone()[0]==1
    persist_messages(2,[basic(1,text='Другой аккаунт')])
    with db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM chats').fetchone()[0]==2
        assert conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]==2
    result=save_chat.run({'source':archive,'chat':str(chat_id),'output':tmp_path/'out','language':'ru'},Reporter())
    assert result['renderer']=='database'
    html=(Path(result['chat_dir'])/'messages'/'messages0.html').read_text()
    assert 'Архивный текст' in html and 'forward' in html
    assert_portable(tmp_path/'out')


def test_archive_path_still_exports_original_cp1251_as_utf8(store,tmp_path):
    archive=make_archive(tmp_path,charset='windows-1251')
    parse_dump.run({'source':archive},Reporter())
    with db.connection() as conn:
        chat_id=conn.execute('SELECT id FROM chats').fetchone()[0]
    result=save_chat.run({'source':archive,'chat':str(chat_id),'output':tmp_path/'out'},Reporter())
    assert result.get('renderer') is None
    html=(Path(result['chat_dir'])/'messages'/'messages0.html').read_text()
    assert 'Архивный текст' in html and 'windows-1251' not in html
    assert '<meta charset="utf-8">' in html
    assert (tmp_path/'out'/'style.css').is_file()


def test_empty_partial_missing_and_unparsed_fields_are_flagged(store,tmp_path):
    with db.connection() as conn:
        chat_id=upsert_chat(conn,1,None,title='Неизвестный чат',source_folder='unknown')
        write_messages(conn,chat_id,[ParsedMessage(vk_message_id=1,sent_at=datetime(2024,1,1),sender_vk_id=None,
                      sender_display_name=None,sender_is_self=False,text='?',is_reply=True,reply_to_message_id=999,
                      raw_html='<div>unparsed</div>',fully_parsed=False)])
    result=save_chat.run({'source':tmp_path/'missing','chat':str(chat_id),'output':tmp_path/'out','language':'ru'},Reporter())
    codes={i['code'] for i in json.loads(Path(result['issues_report']).read_text())['issues']}
    assert {'missing_sender','missing_author_name','missing_peer_id','reply_target_unavailable','unparsed_html','original_archive_not_found'} <= codes
    html=(Path(result['chat_dir'])/'messages'/'messages0.html').read_text()
    assert '<!--' in html and 'partial_history' in html
    assert 'Неполная история' not in html and 'Сообщение недоступно' not in html
    with db.connection() as conn:
        empty=upsert_chat(conn,1,99,title='Empty')
    result=save_chat.run({'chat':str(empty),'output':tmp_path/'empty','language':'en'},Reporter())
    assert result['pages']==1 and result['blocks']==0
    html=(Path(result['chat_dir'])/'messages'/'messages0.html').read_text()
    assert 'empty_history' in html and 'No messages' not in html
    with pytest.raises(ValueError,match='No chat in DB'):
        save_chat.run({'chat':'9999','output':tmp_path/'bad'},Reporter())


def test_missing_api_timestamp_is_flagged_without_fake_message(store,tmp_path):
    item=basic(1);item.pop('date');item['unexpected']='secret-token'
    persist_messages(1,[item])
    with db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]==0
        chat_id=conn.execute('SELECT id FROM chats').fetchone()[0]
    result=save_chat.run({'chat':str(chat_id),'output':tmp_path/'out'},Reporter())
    assert result['flagged_fields']==1
    assert json.loads(Path(result['issues_report']).read_text())['issues'][0]['code']=='missing_timestamp'


def test_newer_edits_preserve_downloads_and_remove_deleted_attachments(store):
    old=basic(1,attachments=[{'type':'photo','photo':{'id':1,'owner_id':10,'sizes':[{'url':'https://cdn.example/old'}]}}])
    persist_messages(1,[old])
    with db.connection() as conn:
        conn.execute("UPDATE attachments SET local_path='photo/a.jpg',download_status='ok'")
    edited=basic(1,text='Изменённый текст',update_time=1700001000,attachments=[{'type':'photo','photo':{'id':1,'owner_id':10,'sizes':[{'url':'https://cdn.example/new'}]}}])
    persist_messages(1,[edited]);persist_messages(1,[old])
    with db.connection() as conn:
        assert conn.execute('SELECT text FROM messages').fetchone()[0]=='Изменённый текст'
        assert conn.execute('SELECT local_path FROM attachments').fetchone()[0]=='photo/a.jpg'
    edited.update(update_time=1700002000,attachments=[],text='')
    persist_messages(1,[edited])
    with db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM attachments').fetchone()[0]==0
        assert conn.execute('SELECT text,attachment_count FROM messages').fetchone()[0]==''


def test_utf8_and_extra_id_attribute_round_trip():
    html=archive_html(42,body='Привет 🌍')
    messages,errors=parse_page(decode_dump_bytes(html.encode()),source_file='messages0.html')
    assert not errors and len(messages)==1 and messages[0].text=='Привет 🌍'


def test_cli_and_gui_allow_export_without_archive(store,monkeypatch,tmp_path):
    from typer.testing import CliRunner
    from vkdump.cli import main as cli
    task=get_task('save-chat')
    assert not next(p for p in task.params if p.name=='source').required
    called=[];monkeypatch.setattr(cli,'_run',lambda name,params:called.append((name,params)))
    result=CliRunner().invoke(cli.app,['save-chat','--output',str(tmp_path),'--language','ru'])
    assert result.exit_code==0,result.output
    assert called[0][1]['source'] is None and called[0][1]['language']=='ru'


def test_schema_update_preserves_archive_messages_and_local_files(tmp_path,monkeypatch):
    from types import SimpleNamespace
    original=db._migrations_dir()
    old=tmp_path/'migrations';old.mkdir()
    for sql in original.glob('*.sql'):
        if sql.name < '020':
            shutil.copyfile(sql,old/sql.name)
    monkeypatch.setattr(db,'get_settings',lambda:SimpleNamespace(db_path=str(tmp_path/'old.db'),sql_debug=False))
    monkeypatch.setattr(db,'_migrations_dir',lambda:old)
    db.apply_migrations()
    with db.connection() as conn:
        conn.execute("INSERT INTO accounts(id,provider,vk_id) VALUES(7,'vk',42)")
        conn.execute("INSERT INTO chats(id,provider,account_id,peer_id,source_folder,message_count) VALUES(3,'vk','42','10','10',1)")
        conn.execute("INSERT INTO messages(id,provider,account_id,chat_id,vk_message_id,sent_at,text,source_file) VALUES(5,'vk','42',3,101,'2024-01-01 00:00:00','Сохранённое','messages0.html')")
        conn.execute("INSERT INTO attachments(id,message_id,kind,position,local_path,download_status) VALUES(9,5,'photo',0,'photo/a.jpg','ok')")
        conn.execute("INSERT INTO parse_errors(chat_id,source_file,error) VALUES(3,'broken.html','error')")
    monkeypatch.setattr(db,'_migrations_dir',lambda:original)
    assert db.apply_migrations()==['020_common_messages.sql']
    with db.connection() as conn:
        tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {'chats','messages','attachments','parse_errors'} <= tables
        assert not any(name.endswith('_new') for name in tables)
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        assert conn.execute('SELECT account_id FROM chats').fetchone()[0]==7
        assert conn.execute('SELECT account_id,text FROM messages').fetchone()['text']=='Сохранённое'
        assert conn.execute('SELECT local_path,download_status FROM attachments').fetchone()['download_status']=='ok'
        assert conn.execute('SELECT chat_id FROM parse_errors').fetchone()[0]==3


def test_api_unknown_fields_and_timestamp_conflict_are_reported(store,tmp_path):
    archive=make_archive(tmp_path)
    parse_dump.run({'source':archive,'source_timezone':'Europe/Moscow'},Reporter())
    persist_messages(1,[basic(1,custom_field='secret-token')])
    with db.connection() as conn:
        chat_id=conn.execute('SELECT id FROM chats').fetchone()[0]
    result=save_chat.run({'chat':str(chat_id),'output':tmp_path/'out'},Reporter())
    raw=Path(result['issues_report']).read_text()
    issues=json.loads(raw)['issues']
    assert any(i['code']=='unknown_api_field' and i['field']=='custom_field' for i in issues)
    assert any(i['code']=='source_value_conflict' and i['field']=='sent_at' for i in issues)
    assert 'secret-token' not in raw


def test_actual_template_example_inline_photo_is_not_discarded():
    html=archive_html(42,body='Фото').replace('<div class="kludges"></div>',
       '<div class="kludges"><div class="attachment"><div class="vkdump-inline-photo"><a href="../assets/p.jpg"><img src="../assets/p.jpg" alt="Фотография"></a><div class="vkdump-caption"><a href="https://cdn.example/photo">VK</a></div></div></div></div>')
    messages,errors=parse_page(html,source_file='messages0.html')
    assert not errors
    assert messages[0].attachments[0].kind=='photo'
    assert messages[0].attachments[0].url=='https://cdn.example/photo'
    assert messages[0].attachments[0].data['source_local_path']=='../assets/p.jpg'


def test_failed_api_history_remains_exportable_and_is_flagged_until_resume(store,monkeypatch,tmp_path):
    from vkdump.core.vk_api import VKApiError
    fake=FakeVK({10:[basic(mid) for mid in range(205,0,-1)]})
    fake.fail_offset=200
    with pytest.raises(VKApiError):
        run_fake(monkeypatch,fake,threads=1,per_request=1)
    with db.connection() as conn:
        chat_id=conn.execute('SELECT id FROM chats').fetchone()[0]
        assert conn.execute('SELECT message_count FROM chats').fetchone()[0]==200
    result=save_chat.run({'chat':str(chat_id),'output':tmp_path/'partial','language':'ru'},Reporter())
    issues=json.loads(Path(result['issues_report']).read_text())['issues']
    assert any(i['code']=='history_unavailable' for i in issues)
    html=(Path(result['chat_dir'])/'messages'/'messages0.html').read_text()
    assert '"saved": 200' in html and '"expected": 205' in html
    assert 'Неполная история' not in html and 'date-divider' not in html
    fake.fail_offset=None
    run_fake(monkeypatch,fake,threads=1,per_request=1)
    result=save_chat.run({'chat':str(chat_id),'output':tmp_path/'complete'},Reporter())
    assert not any(i['code']=='history_unavailable' for i in json.loads(Path(result['issues_report']).read_text())['issues'])


def test_pagination_and_stylesheet_match_supplied_vk_reference_exactly():
    import hashlib
    from vkdump.modules.save_chat import templates
    from vkdump.modules.save_chat.generic import pagination
    fixture=Path(__file__).parent/'fixtures'/'vk-pagination.html'
    assert pagination(2,25,50)==fixture.read_text()
    assert hashlib.sha256((templates.ROOT/'style.css').read_bytes()).hexdigest()=='e2d1faf341c69ca7dd781c0fc4cc6a081db202e068701433b46132890256c7b8'


def test_diagnostics_are_comments_and_paginator_is_inside_original_container(store,tmp_path):
    from vkdump.modules.save_chat.generic import diagnostic
    persist_messages(1,[basic(i,attachments=[{'type':'future','future':{}}]) for i in range(1,52)])
    with db.connection() as conn:
        chat_id=conn.execute('SELECT id FROM chats').fetchone()[0]
    result=save_chat.run({'chat':str(chat_id),'output':tmp_path/'out','language':'ru'},Reporter())
    html=(Path(result['chat_dir'])/'messages'/'messages0.html').read_text()
    class Text(HTMLParser):
        def __init__(self):
            super().__init__();self.parts=[]
        def handle_data(self,data):
            self.parts.append(data)
    text=Text();text.feed(html);visible=''.join(text.parts)
    assert not any(x in visible for x in ('Неизвестный','Неполная','Файл не','UTC','export_issues','unknown_attachment'))
    assert 'VK Dump' in visible and 'Сообщения' in visible
    assert '<nav' not in html
    assert 'pagination clear_fix' in html and 'pg_lnk_sel fl_l' in html and 'class="fl_r"' in html
    assert '<div class="wrap_page_content">' in html[:html.index('<div class="pagination clear_fix">')]
    assert (Path(result['chat_dir'])/'messages'/'messages50.html').is_file()
    assert diagnostic('test',field='--> <script>')=='<!-- {"code": "test", "field": "- -\\u003e \\u003cscript\\u003e"} -->'


@pytest.mark.parametrize('lang,counts',[
    ('ru', [(0,'0 сообщений'),(1,'1 сообщение'),(4,'4 сообщения'),(11,'11 сообщений'),
            (14,'14 сообщений'),(21,'21 сообщение'),(24,'24 сообщения'),(111,'111 сообщений'),(205,'205 сообщений')]),
    ('en', [(0,'0 messages'),(1,'1 message'),(4,'4 messages'),(21,'21 messages')]),
])
def test_export_menu_and_dialog_counts_follow_archive_layout(tmp_path,lang,counts):
    from vkdump.modules.save_chat.generic import write_indexes
    from vkdump.modules.save_chat.index import ExportEntry, ExportManifest
    manifest=ExportManifest(chats=[ExportEntry(str(n),str(n),f'Dialog {n}','dm',n,'',chat_id=n+1) for n,_ in counts])
    write_indexes(tmp_path,manifest,lang)
    root=Elements((tmp_path/'index.html').read_text())
    link=root.with_class('menu__page-link')[0]
    assert link['attrs']['href']=='messages/index-messages.html'
    assert [p['attrs'].get('class') for p in link['parents'][3:]]==[
        'page_content clear_fix','menu','page_block','wrap_page_content','item','item__main','index-row']
    assert len(root.with_class('item'))==1
    assert not root.with_class('message-peer') and not root.with_class('item__tertiary')
    dialogs=Elements((tmp_path/'messages'/'index-messages.html').read_text())
    content=dialogs.with_class('wrap_page_content')[0]
    assert [p['attrs'].get('class') for p in content['parents'][-2:]]==['page_content clear_fix','page_block']
    assert dialogs.with_class('ui_crumb')[0]['attrs']['href']=='../index.html'
    assert [''.join(row['text']) for row in dialogs.with_class('item__tertiary')]==[label for _,label in counts]
    assert len(dialogs.with_class('message-peer--id'))==len(counts)
    assert not dialogs.with_class('meta')
    from vkdump.tasks.registry import _save_chat_open_path
    assert _save_chat_open_path({'output_dir':str(tmp_path),'chat_id':1})==f'{tmp_path / "messages" / "index-messages.html"}#chat-1'


@pytest.mark.parametrize('raw,expected',[
    ('2026-10-13 10:45:54+00:00',datetime(2026,10,13,10,45,54)),
    ('2026-10-13 10:45:55+00:00',datetime(2026,10,13,10,45,55)),
    ('2026-10-13T13:45:54.123456+03:00',datetime(2026,10,13,10,45,54,123456)),
    ('2026-10-13 10:45:54.123456Z',datetime(2026,10,13,10,45,54,123456)),
    ('2026-10-13 10:45:54',datetime(2026,10,13,10,45,54)),
])
def test_timezone_timestamps_export_all_chats_without_skips(store,tmp_path,raw,expected):
    persist_messages(1,[basic(1),basic(2,peer=12)])
    with db.connection() as conn:
        conn.execute('UPDATE messages SET sent_at=?,edited_at=?,is_edited=1',(raw,raw))
        conn.execute('UPDATE chats SET created_at=?,last_message_at=?',(raw,raw))
    with db.connection() as conn:
        assert conn.execute('SELECT sent_at,edited_at FROM messages').fetchone()['sent_at']==expected
        assert conn.execute('SELECT created_at FROM chats').fetchone()[0]==expected
    outcome=orchestrator.execute(get_task('save-chat'),{'output':tmp_path/'out','language':'ru'},Reporter())
    assert outcome.status=='success',outcome.error
    assert outcome.result['chats_rendered']==2 and not outcome.result['skipped']
    for result in outcome.result['per_chat']:
        text=(tmp_path/'out'/result['chat_slug']/'messages'/'messages0.html').read_text()
        assert '13 окт 2026 в 10:45:54' in text or '13 окт 2026 в 10:45:55' in text
    assert_portable(tmp_path/'out')


def test_download_media_then_save_chat_inlines_portable_photo(store,monkeypatch,tmp_path):
    from vkdump.modules import enrich
    from vkdump.modules.save_chat import generic
    photo=(Path(__file__).parents[1]/'assets'/'icon.png').read_bytes()
    requested=[]
    class Response:
        status=200
        headers={'Content-Type':'image/png'}
        async def __aenter__(self):
            return self
        async def __aexit__(self,*args):
            pass
        async def read(self):
            return photo
    class Session:
        def __init__(self,connector,**kwargs):
            self.connector=connector
        async def __aenter__(self):
            return self
        async def __aexit__(self,*args):
            await self.connector.close()
        def get(self,url):
            requested.append(url)
            return Response()
    url='https://cdn.example/photo.png'
    run_fake(monkeypatch,FakeVK({10:[basic(1,attachments=[{'type':'photo','photo':{'id':12,'owner_id':10,
        'sizes':[{'url':url,'width':640,'height':480}]}}])]}))
    app=tmp_path/'app'
    monkeypatch.setattr(enrich,'_static_root',lambda:app/'data'/'static')
    monkeypatch.setattr(generic,'app_dir',lambda:app)
    monkeypatch.setattr(enrich.aiohttp,'ClientSession',Session)
    outcome=orchestrator.execute(get_task('enrich-media'),{},Reporter())
    assert outcome.status=='success',outcome.error
    assert outcome.result['downloaded']==1 and outcome.result['failed']==0
    assert requested==[url]
    with db.connection() as conn:
        row=conn.execute('SELECT local_path,download_status,message_id FROM attachments').fetchone()
        assert row['download_status']=='ok'
        original=app/'data'/'static'/row['local_path']
        assert original.read_bytes()==photo
        conn.execute("UPDATE attachments SET remote_modified_at='2026-10-13 10:45:54+00:00'")
        chat_id=conn.execute('SELECT chat_id FROM messages WHERE id=?',(row['message_id'],)).fetchone()[0]
    outcome=orchestrator.execute(get_task('save-chat'),{'chat':str(chat_id),'output':tmp_path/'out','language':'ru'},Reporter())
    assert outcome.status=='success',outcome.error
    result=outcome.result
    page=Path(result['chat_dir'])/'messages'/'messages0.html'
    image=next(n for n in Elements(page.read_text()).nodes if n['tag']=='img')['attrs']
    assert 'max-width:100%' in image['style']
    copied=(page.parent/image['src']).resolve()
    assert copied.read_bytes()==photo
    original.unlink()
    assert copied.read_bytes()==photo
    assert_portable(tmp_path/'out')


@pytest.mark.parametrize('lang,reply_label,forward_label',[
    ('ru','Ответ на сообщение','2 прикреплённых сообщения'),
    ('en','Reply to message','2 attached messages'),
])
def test_replies_and_forwards_are_separate_quotes_before_reply_text(store,tmp_path,lang,reply_label,forward_label):
    original=basic(1,text='Original message')
    forwarded=basic(70,text='Forward text',from_id=10,fwd_messages=[basic(71,text='Nested forward')])
    response=basic(2,text='Actual response',reply_message=original,fwd_messages=[forwarded,basic(72,text='Second forward')])
    persist_messages(1,[response,original])
    with db.connection() as conn:
        chat_id=conn.execute('SELECT id FROM chats').fetchone()[0]
    output=tmp_path/lang
    result=save_chat.run({'chat':str(chat_id),'output':output,'language':lang,'page_size':1},Reporter())
    html=(Path(result['chat_dir'])/'messages'/'messages0.html').read_text()
    dom=Elements(html)
    reply=dom.with_class('reply')[0]
    forwards=dom.with_class('forward')
    assert reply['tag']=='div' and 'attachment' in reply['attrs']['class'].split()
    assert reply['attrs'].get('style') is None
    assert reply_label in ''.join(reply['text']) and 'Original message' in ''.join(reply['text'])
    assert 'Actual response' not in ''.join(reply['text'])
    link=next(n for n in dom.nodes if n['tag']=='a' and ''.join(n['text'])==reply_label)
    assert link['attrs']['href']=='messages1.html#m1'
    assert html.index('class="attachment reply"') < html.index('Actual response') < html.index('class="attachment forward"')
    assert len(forwards)==2 and all(n['tag']=='div' for n in forwards)
    assert forward_label in ''.join(forwards[0]['text'])
    assert 'Second forward' in ''.join(forwards[0]['text'])
    assert 'Second forward' not in ''.join(forwards[1]['text'])
    assert not dom.with_class('meta')
    assert len([n for n in dom.with_class('message') if n['attrs'].get('id')])==1
    assert all('message' in n['attrs']['class'].split() for n in dom.with_class('nested-message'))
    assert '<blockquote' not in html and 'background:' not in html and 'border-left:' not in html
    assert_portable(output)


def test_api_photos_use_identical_archive_photo_markup_and_remain_separate(tmp_path,monkeypatch):
    from vkdump.modules.save_chat.generic import Renderer
    from vkdump.modules.save_chat.pipeline import AttMeta
    from vkdump.modules.save_chat.transforms import InlinePhotosTransform
    app=tmp_path/'app';static=app/'data'/'static';static.mkdir(parents=True)
    monkeypatch.setattr('vkdump.modules.save_chat.generic.app_dir',lambda:app)
    chat=tmp_path/'chat';chat.mkdir()
    renderer=Renderer('ru',chat,{}, {})
    rendered=[]
    for n in (1,2):
        (static/f'{n}.jpg').write_bytes(b'photo')
        url=f'https://cdn.example/{n}.jpg'
        row={'kind':'photo','description':None,'url':url,'local_path':f'{n}.jpg','download_status':'ok','resolution':'640x480','file_size':203}
        html=renderer.attachment(row,0)
        image=next(n for n in Elements(html).nodes if n['tag']=='img')
        att=ParsedAttachment(kind='photo',description='Фотография',url=url)
        original=f'<div class="attachment"><div class="attachment__description">Фотография</div><a class="attachment__link" href="{url}">{url}</a></div>'
        expected=InlinePhotosTransform._inject(original,att,image['attrs']['src'],AttMeta(f'{n}.jpg','640x480',203))
        assert html==expected
        assert 'Открыть вложение' not in html and '>cdn.example</a>' in html
        assert '640x480, 203B' in html
        rendered.append(html)
    dom=Elements(''.join(rendered))
    assert len(dom.with_class('attachment'))==2
    assert len(dom.with_class('vkdump-inline-photo'))==2
    assert len(dom.with_class('vkdump-caption'))==2
