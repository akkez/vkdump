import asyncio
import json

import pytest

from tests.test_vk_api_decode import _FakeSession, _make_vk_with_session
from vkdump.core.vk_api import VKApiError


@pytest.mark.parametrize('method', ['messages.getConversations', 'execute'])
def test_api_error_preserves_method_and_redacts_credentials(method):
    error = {'error_code': 15, 'error_msg': 'Access denied synthetic-token-do-not-use',
             'request_params': [{'key': 'method', 'value': method},
                                {'key': 'access_token', 'value': 'synthetic-token-do-not-use'}],
             'nested': {'access_token': 'other-secret', 'password': 'private-password'}}
    vk = _make_vk_with_session(_FakeSession(json.dumps({'error': error}).encode()))
    with pytest.raises(VKApiError) as caught:
        asyncio.run(vk.execute('return [];') if method == 'execute' else vk.call(method, filter='all'))
    assert caught.value.method == method
    detail = json.dumps(caught.value.details)
    assert 'synthetic-token-do-not-use' not in detail
    assert 'other-secret' not in detail and 'private-password' not in detail
    assert 'synthetic-token-do-not-use' not in str(caught.value)
    assert '<redacted>' in detail
    assert caught.value.details['request_params'][0]['value'] == method


def test_execute_subcall_errors_are_redacted():
    error = {'error_code': 15, 'method': 'messages.getHistory',
             'request_params': [{'key': 'access_token', 'value': 'synthetic-token-do-not-use'}]}
    vk = _make_vk_with_session(_FakeSession(json.dumps({'response': [False], 'execute_errors': [error]}).encode()))
    response, errors = asyncio.run(vk.execute('return [];'))
    assert response == [False]
    assert errors[0]['method'] == 'messages.getHistory'
    assert 'synthetic-token-do-not-use' not in json.dumps(errors)
