ISSUES = {
    'en': {'missing_sender':'Author ID is missing','missing_author_name':'Author name is missing',
           'missing_peer_id':'Chat ID is missing','reply_target_unavailable':'Reply target is absent from the saved history',
           'unknown_attachment':'Unrecognized attachment type','missing_file':'Attachment file is unavailable locally',
           'unparsed_html':'Some source HTML could not be parsed','forward_body_unavailable':'Forwarded content is unavailable',
           'source_value_conflict':'Sources disagree on a field value','unknown_chat_event':'Unrecognized chat event','normalized_field_unrendered':'An attachment field is preserved but has no HTML representation','unknown_api_field':'An API field has no normalized representation','missing_timestamp':'Message timestamp is missing; the message was not imported',
           'history_unavailable':'Could not retrieve the complete chat history',
           'original_chat_not_found':'The chat was not found in the selected archive; exported from the database',
           'original_archive_not_found':'The selected archive is unavailable; exported from the database'},
    'ru': {'missing_sender':'Неизвестен ID автора','missing_author_name':'Неизвестно имя автора',
           'missing_peer_id':'Неизвестен ID чата','reply_target_unavailable':'Цель ответа отсутствует в сохранённой истории',
           'unknown_attachment':'Неизвестный тип вложения','missing_file':'Файл вложения недоступен локально',
           'unparsed_html':'Часть исходного HTML не удалось разобрать','forward_body_unavailable':'Содержимое пересылки недоступно',
           'source_value_conflict':'Источники содержат разные значения поля','unknown_chat_event':'Неизвестное событие чата','normalized_field_unrendered':'Поле вложения сохранено, но не имеет HTML-представления','unknown_api_field':'Для поля API нет представления в общей модели','missing_timestamp':'Нет времени сообщения; сообщение не импортировано',
           'history_unavailable':'Не удалось получить полную историю чата',
           'original_chat_not_found':'Чат не найден в выбранном архиве; экспорт выполнен из базы',
           'original_archive_not_found':'Выбранный архив недоступен; экспорт выполнен из базы'},
}


def collect(conn,chat,renderer,extra=()):
    issues=list(extra)
    def add(code,mid=None,field=None):
        issues.append({'code':code,'message_id':mid,'field':field})
    if chat.get('peer_id') is None:
        add('missing_peer_id',field='peer_id')
    for row in conn.execute('SELECT vk_message_id,field,reason FROM chat_data_issues WHERE chat_id=?',(chat['id'],)):
        add(row['reason'],row['vk_message_id'] or None,row['field'])
    for row in conn.execute("SELECT m.vk_message_id,m.sender_vk_id,COALESCE(NULLIF(u.display_name,''),m.sender_display_name) AS name,m.raw_html,m.is_reply,m.reply_to_message_id FROM messages m LEFT JOIN users u ON u.id=m.sender_user_id WHERE m.chat_id=?",(chat['id'],)):
        mid=row['vk_message_id']
        if row['sender_vk_id'] is None:
            add('missing_sender',mid,'sender_vk_id')
        if not row['name']:
            add('missing_author_name',mid,'sender_display_name')
        if row['raw_html']:
            add('unparsed_html',mid,'raw_html')
        if row['is_reply'] and row['reply_to_message_id'] not in renderer.reply_pages:
            add('reply_target_unavailable',mid,'reply_to_message_id')
    media={'photo','video','audio','file','sticker','graffiti','audio_message'}
    for row in conn.execute('SELECT a.*,m.vk_message_id FROM attachments a JOIN messages m ON m.id=a.message_id WHERE m.chat_id=?',(chat['id'],)):
        a=dict(row)
        import json
        for field in json.loads(a['data_json'] or '{}').get('unrendered_fields',[]):
            add('normalized_field_unrendered',a['vk_message_id'],f"attachments.{a['kind']}.{field}")
        if a['kind']=='chat_event':
            from .templates import EVENTS
            import json
            if json.loads(a['data_json'] or '{}').get('subtype') not in EVENTS['en']:
                add('unknown_chat_event',a['vk_message_id'],'action')
        if a['kind']=='unknown':
            add('unknown_attachment',a['vk_message_id'],'attachments')
        if a['kind'] in media and not renderer.local_file(a):
            add('missing_file',a['vk_message_id'],'attachments')
        if a['kind']=='forward' and not a['data_json']:
            add('forward_body_unavailable',a['vk_message_id'],'forwards')
    for row in conn.execute('SELECT source_file,error FROM parse_errors WHERE chat_id=?',(chat['id'],)):
        issues.append({'code':'unparsed_html','field':'source_file','source_file':row['source_file']})
    return issues
