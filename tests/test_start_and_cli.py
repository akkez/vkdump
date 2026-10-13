import os
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QWizard
from typer.testing import CliRunner

from vkdump.cli import main as cli
from vkdump.core import app_config, db, vk_auth
from vkdump.gui.auth_dialog import AuthDialog
from vkdump.gui.main_window import MainWindow
from vkdump.gui.start_view import WELCOME, SOURCE, IMPORT, DOWNLOAD


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'get_settings', lambda: SimpleNamespace(db_path=str(tmp_path / 'test.db'), sql_debug=False))
    db.apply_migrations()


@pytest.fixture(scope='module')
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(store, qapp):
    window = MainWindow()
    yield window
    window._close_confirmed = True
    window.close()
    qapp.processEvents()
    QThreadPool.globalInstance().waitForDone(2000)
    window.deleteLater()
    qapp.processEvents()


def auth_info(vk_id=42):
    return {'vk_id': vk_id, 'display_name': 'Test Account', 'scope': 4096,
            'app_id': 123, 'avatar_url': None, 'avatar_binary': None, 'avatar_content_type': None}


def _go_to_download(start):
    start.reset()
    start._wizard.button(QWizard.WizardButton.NextButton).click()
    assert start._wizard.currentId() == SOURCE
    start._source_page.account.click()
    start._wizard.button(QWizard.WizardButton.NextButton).click()
    assert start._wizard.currentId() == DOWNLOAD


def test_start_navigation_and_same_authorization_dialog(window, monkeypatch):
    assert window._tabs.tabText(0) == 'Start'
    assert window._tabs.currentIndex() == 0
    start = window._start_view
    assert start._wizard.currentId() == WELCOME
    start._wizard.button(QWizard.WizardButton.NextButton).click()
    assert not start._wizard.button(QWizard.WizardButton.NextButton).isEnabled()
    start._source_page.archive.click()
    start._wizard.button(QWizard.WizardButton.NextButton).click()
    assert start._wizard.currentId() == IMPORT
    assert any('https://vk.ru/data_protection?section=rules#archive' in label.text() for label in start.findChildren(QLabel))
    start._import_btn.click()
    assert window._tabs.currentWidget() is window._import_panel
    window._tabs.setCurrentIndex(0)
    _go_to_download(start)
    assert not start._download_btn.isEnabled()
    def authorize(self):
        assert isinstance(self, AuthDialog)
        assert any('Authorize and grant access' in label.text() for label in self.findChildren(QLabel))
        vk_auth.persist('synthetic-token', auth_info())
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(AuthDialog, 'exec', authorize)
    start._auth_btn.click()
    assert start._download_btn.isEnabled()
    assert 'Test Account (id42)' in start._account_label.text()
    assert start._options.isHidden()
    assert app_config.get('active_account_id') == '42'
    start._threads.setValue(7)
    start._calls.setValue(13)
    started = []
    monkeypatch.setattr(window._download_panel, '_on_run', lambda: started.append(window._download_panel._collect_params()))
    start._download_btn.click()
    assert window._tabs.currentWidget() is window._download_panel
    assert started == [{'threads': 7, 'per_request': 13}]
    window._download_panel._editors['threads'].setValue(6)
    assert start._threads.value() == 6


def test_start_follows_settings_account_and_reauthorization(window):
    vk_auth.persist('synthetic-token', auth_info())
    window.data_changed.emit()
    _go_to_download(window._start_view)
    assert window._start_view._download_btn.isEnabled()
    with db.connection() as conn:
        conn.execute("INSERT INTO accounts(provider, vk_id, display_name) VALUES('vk', 43, 'Unverified Account')")
    window.data_changed.emit()
    combo = window._settings_view._account_cb
    combo.setCurrentIndex(combo.findData('43'))
    assert not window._start_view._download_btn.isEnabled()
    assert window._start_view._auth_btn.text() == 'Click to authorize through VK'
    combo.setCurrentIndex(combo.findData('42'))
    assert window._start_view._download_btn.isEnabled()
    assert window._start_view._auth_btn.text() == 'Reauthorize through VK'


def test_wizard_back_can_change_the_route_and_cancel_confirms_exit(window, monkeypatch):
    from vkdump.gui.xp_widgets import XPExitDialog
    start = window._start_view
    assert not start._wizard.button(QWizard.WizardButton.BackButton).isEnabled()
    _go_to_download(start)
    assert start._wizard.button(QWizard.WizardButton.BackButton).isEnabled()
    start._wizard.button(QWizard.WizardButton.BackButton).click()
    assert start._wizard.currentId() == SOURCE
    start._source_page.archive.click()
    start._wizard.button(QWizard.WizardButton.NextButton).click()
    assert start._wizard.currentId() == IMPORT
    asked = []
    monkeypatch.setattr(XPExitDialog, 'confirm', lambda *args: asked.append(True) or False)
    window.show()
    start._wizard.button(QWizard.WizardButton.CancelButton).click()
    assert asked == [True]
    assert window.isVisible()
    assert start._wizard.currentId() == IMPORT
    assert window._tabs.currentIndex() == 0


def test_radio_descriptions_select_the_corresponding_source(window, qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from vkdump.gui.start_view import _RadioDescription
    window.show()
    start = window._start_view
    start._wizard.next()
    qapp.processEvents()
    descriptions = start._source_page.findChildren(_RadioDescription)
    assert not start._wizard.button(QWizard.WizardButton.NextButton).isEnabled()
    QTest.mouseClick(descriptions[0], Qt.MouseButton.LeftButton)
    assert start._source_page.archive.isChecked()
    assert start._source_page.nextId() == IMPORT
    assert start._wizard.button(QWizard.WizardButton.NextButton).isEnabled()
    QTest.mouseClick(descriptions[1], Qt.MouseButton.LeftButton)
    assert start._source_page.account.isChecked()
    assert not start._source_page.archive.isChecked()
    start._wizard.button(QWizard.WizardButton.NextButton).click()
    assert start._wizard.currentId() == DOWNLOAD


def test_wizard_and_exit_dialog_have_no_white_border_gutters(window, qapp):
    from PySide6.QtCore import Qt
    from vkdump.gui.start_view import _icon
    from vkdump.gui.xp_widgets import XPExitDialog
    window.show()
    qapp.processEvents()
    image = window._start_view.grab().toImage()
    scale = image.devicePixelRatio()
    assert image.pixelColor(round(3 * scale), round(100 * scale)).name() != '#ffffff'
    assert image.pixelColor(round((window.width() - 3) * scale), round(100 * scale)).name() != '#ffffff'
    dialog = XPExitDialog(window, _icon('setup.png', 16))
    assert dialog.windowModality() == Qt.WindowModality.ApplicationModal
    dialog.show()
    qapp.processEvents()
    image = dialog.grab().toImage()
    scale = image.devicePixelRatio()
    assert image.pixelColor(round(3 * scale), round(100 * scale)).name() == '#ece9d8'
    assert image.pixelColor(round((dialog.width() - 4) * scale), round(100 * scale)).name() == '#ece9d8'
    dialog.reject()


def test_start_morphs_into_workspace_and_back_to_a_fresh_wizard(window, qapp):
    from PySide6.QtCore import Qt
    from vkdump.gui.start_view import WIZARD_WIDTH, WIZARD_HEIGHT
    window.show()
    qapp.processEvents()
    assert window._wizard_mode
    assert window._start_view.isVisible()
    assert not window._tabs.isVisible()
    assert (window.width(), window.height()) == (WIZARD_WIDTH, WIZARD_HEIGHT)
    assert window.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert not window._start_view.caption.maximize_button.isEnabled()
    window._start_view._wizard.next()
    window._start_view._source_page.archive.click()
    window._start_view._wizard.next()
    window._start_view._import_btn.click()
    qapp.processEvents()
    assert not window._wizard_mode
    assert window._tabs.isVisible()
    assert not window._start_view.isVisible()
    assert not window.windowFlags() & Qt.WindowType.FramelessWindowHint
    window.resize(1080, 720)
    window.move(120, 120)
    qapp.processEvents()
    geometry = window.geometry()
    window._tabs.setCurrentIndex(0)
    qapp.processEvents()
    assert window._wizard_mode
    assert not window._tabs.isVisible()
    assert window._start_view._wizard.currentId() == WELCOME
    assert not window._start_view._source_page.archive.isChecked()
    assert (window.width(), window.height()) == (WIZARD_WIDTH, WIZARD_HEIGHT)
    window._tabs.setCurrentWidget(window._import_panel)
    qapp.processEvents()
    assert window.geometry() == geometry


def test_wizard_close_button_and_cancel_share_confirmation(window, monkeypatch, qapp):
    from vkdump.gui.xp_widgets import XPExitDialog
    asked = []
    answer = [False]
    monkeypatch.setattr(XPExitDialog, 'confirm', lambda *args: asked.append(True) or answer[0])
    window.show()
    window._start_view.caption.close_button.click()
    assert asked == [True]
    assert window.isVisible()
    answer[0] = True
    window._start_view._wizard.button(QWizard.WizardButton.CancelButton).click()
    qapp.processEvents()
    assert asked == [True, True]
    assert not window.isVisible()


def test_wizard_minimizes_and_keeps_its_page(window, qapp):
    window.show()
    window._start_view._wizard.next()
    window._start_view.caption.minimize_button.click()
    qapp.processEvents()
    assert window.isMinimized()
    window.showNormal()
    qapp.processEvents()
    assert window._wizard_mode
    assert window._start_view._wizard.currentId() == SOURCE


def test_fetch_data_prefills_execute_even_with_a_stored_sequential_choice(store, qapp):
    app_config.set('fetch_data.last_strategy', 'get-by-id')
    from vkdump.gui.task_panel import TaskPanel
    from vkdump.tasks.registry import get_task
    panel = TaskPanel(get_task('fetch-data'))
    assert panel._editors['strategy'].currentData() == 'execute'
    panel.deleteLater()
    qapp.processEvents()


def test_download_and_fetch_controls_have_matching_alignment(window, qapp):
    window.show()
    positions = []
    for panel in window._task_panels:
        if panel._task.name not in ('download-messages', 'fetch-data'):
            continue
        window._tabs.setCurrentWidget(panel)
        qapp.processEvents()
        threads = panel._editors['threads']
        calls = panel._editors['per_request']
        assert threads.x() == calls.x()
        assert threads.width() == calls.width()
        positions.append((threads.x(), threads.width()))
    assert positions[0] == positions[1]


def test_cli_fetch_defaults_to_execute_and_keeps_the_sequential_option(monkeypatch):
    tasks = []
    monkeypatch.setattr(cli, '_run', lambda name, params: tasks.append((name, params)))
    assert CliRunner().invoke(cli.app, ['fetch-data']).exit_code == 0
    assert tasks[-1][1]['strategy'] == 'execute'
    assert CliRunner().invoke(cli.app, ['fetch-data', '--strategy', 'get-by-id']).exit_code == 0
    assert tasks[-1][1]['strategy'] == 'get-by-id'


@pytest.mark.parametrize('args', [[], ['db'], ['runs'], ['logs']])
def test_cli_has_no_help_or_completion_options(args):
    result = CliRunner().invoke(cli.app, args)
    assert '--help' not in result.output
    assert '--install-completion' not in result.output
    assert '--show-completion' not in result.output
    assert 'Start' not in result.output


@pytest.mark.parametrize('flag', ['--help', '--install-completion', '--show-completion'])
def test_removed_flags_are_unavailable(flag):
    assert CliRunner().invoke(cli.app, [flag]).exit_code == 2
    assert CliRunner().invoke(cli.app, ['fetch-data', flag]).exit_code == 2


def test_cli_download_passes_settings_to_the_shared_task(monkeypatch):
    tasks = []
    monkeypatch.setattr(cli, '_run', lambda name, params: tasks.append((name, params)))
    default_result = CliRunner().invoke(cli.app, ['download-messages'])
    assert default_result.exit_code == 0
    assert tasks.pop() == ('download-messages', {'threads': 4, 'per_request': 15, 'account': None})
    result = CliRunner().invoke(cli.app, ['download-messages', '--threads', '3', '--per-request', '12', '--account', '42'])
    assert result.exit_code == 0, result.output
    assert tasks == [('download-messages', {'threads': 3, 'per_request': 12, 'account': 42})]
    for args in (['--threads', '0'], ['--per-request', '26']):
        assert CliRunner().invoke(cli.app, ['download-messages'] + args).exit_code == 2


def test_cli_authorize_uses_shared_validation_and_does_not_echo_token(store, monkeypatch):
    token = 'synthetic-access-token-value'
    async def validate(value):
        assert value == token
        return auth_info()
    monkeypatch.setattr(vk_auth, '_validate', validate)
    result = CliRunner().invoke(cli.app, ['authorize'], input=f'https://oauth.vk.com/blank.html#access_token={token}\n')
    assert result.exit_code == 0, result.output
    assert token not in result.output
    assert 'Authorized as Test Account (id42)' in result.output
    with db.connection() as conn:
        account = conn.execute('SELECT vk_access_token FROM accounts WHERE vk_id=42').fetchone()
        assert account[0] == token
    assert app_config.get('active_account_id') == '42'


def test_start_runs_real_download_job_and_reports_completion(window, monkeypatch, qapp):
    import time
    from tests.test_download_messages import FakeVK, message
    from vkdump.core import orchestrator
    from vkdump.modules import download_messages

    fake = FakeVK({10: [message(i, 10) for i in range(1000, 749, -1)]}, archived=[])
    monkeypatch.setattr(download_messages, 'VKApi', lambda token: fake)
    monkeypatch.setattr(download_messages, '_MAX_RPS', 100000)
    monkeypatch.setattr(orchestrator, 'configure_logging', lambda: None)
    vk_auth.persist('synthetic-token', auth_info())
    window.data_changed.emit()
    _go_to_download(window._start_view)
    window._start_view._download_btn.click()
    assert window._download_panel.has_active_run()
    assert not window._download_panel._run_btn.isEnabled()
    deadline = time.monotonic() + 5
    while window._download_panel.has_active_run() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert not window._download_panel.has_active_run()
    assert window._download_panel._status.text().startswith('ok — run #')
    with db.connection() as conn:
        assert conn.execute("SELECT task, status FROM runs").fetchone()[:] == ('download-messages', 'success')
        assert conn.execute('SELECT COUNT(*) FROM api_messages').fetchone()[0] == 251
    window._tabs.setCurrentIndex(0)
    assert window._start_view._download_btn.isEnabled()


def test_download_error_details_reach_the_text_area(window, monkeypatch, qapp):
    import time
    from tests.test_download_messages import FakeVK
    from vkdump.core import orchestrator
    from vkdump.core.vk_api import VKApiError
    from vkdump.modules import download_messages

    fake = FakeVK({})
    async def denied(method, **params):
        if method == 'users.get':
            return [{'id': 42}]
        raise VKApiError(15, 'Access denied', method=method,
                         details={'error_code': 15, 'error_msg': 'Access denied'})
    fake.call = denied
    monkeypatch.setattr(download_messages, 'VKApi', lambda token: fake)
    monkeypatch.setattr(download_messages, '_MAX_RPS', 100000)
    monkeypatch.setattr(orchestrator, 'configure_logging', lambda: None)
    vk_auth.persist('synthetic-token', auth_info())
    window.data_changed.emit()
    _go_to_download(window._start_view)
    window._start_view._download_btn.click()
    deadline = time.monotonic() + 5
    while window._download_panel.has_active_run() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert not window._download_panel.has_active_run()
    text = window._download_panel._log.toPlainText()
    assert 'method="messages.getConversations"' in text
    assert '"filter": "all"' in text
    assert '"offset": 0' in text
    assert 'code=15' in text and 'Access denied' in text
    assert 'synthetic-token' not in text
    assert window._download_panel._status.text().startswith('failed — run #')


def test_xp_frame_border_has_no_retina_gaps(qapp):
    from PySide6.QtGui import QColor, QPixmap
    from vkdump.gui.xp_widgets import XPFrame

    frame = XPFrame()
    frame.resize(100, 80)
    image = QPixmap(200, 160)
    image.setDevicePixelRatio(2)
    image.fill(QColor('#ffffff'))
    frame.render(image)
    pixels = image.toImage()
    for y in (60, 120, 159):
        for x in (*range(6), *range(194, 200)):
            assert pixels.pixelColor(x, y).name() not in ('#ffffff', '#ece9d8')
    for y in range(154, 160):
        for x in range(200):
            assert pixels.pixelColor(x, y).name() not in ('#ffffff', '#ece9d8')


def test_wizard_fox_appears_after_welcome(window, qapp):
    wizard = window._start_view._wizard
    window.show()
    qapp.processEvents()
    assert window.windowTitle() == 'VK Dump Wizard'
    assert not wizard._fox.isVisible()
    wizard.next()
    assert wizard._fox.isVisible()
    wizard._owner._source_page.archive.click()
    wizard.next()
    assert wizard._fox.isVisible()
    wizard.back()
    assert wizard._fox.isVisible()
    wizard.back()
    assert not wizard._fox.isVisible()

    wizard.next()
    wizard._owner._source_page.account.click()
    wizard.next()
    assert wizard.currentId() == DOWNLOAD
    assert wizard._fox.isVisible()
    assert wizard._owner._source_page.layout().contentsMargins().left() == 23


def test_wizard_keyboard_navigation_and_space_activation(window, qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    window.show()
    window.activateWindow()
    qapp.processEvents()
    start = window._start_view
    wizard = start._wizard
    next_button = wizard.button(QWizard.WizardButton.NextButton)
    cancel = wizard.button(QWizard.WizardButton.CancelButton)
    next_button.setFocus()
    QTest.keyClick(next_button, Qt.Key.Key_Tab)
    assert cancel.hasFocus()
    QTest.keyClick(cancel, Qt.Key.Key_Backtab)
    assert next_button.hasFocus()
    QTest.keyPress(next_button, Qt.Key.Key_Space)
    assert next_button.isDown()
    assert wizard.currentId() == WELCOME
    QTest.keyRelease(next_button, Qt.Key.Key_Space)
    assert not next_button.isDown()
    assert wizard.currentId() == SOURCE
    archive = start._source_page.archive
    account = start._source_page.account
    assert archive.hasFocus()
    QTest.keyClick(archive, Qt.Key.Key_Space)
    assert archive.isChecked()
    QTest.keyClick(archive, Qt.Key.Key_Down)
    assert account.isChecked()
    assert account.hasFocus()
    QTest.keyClick(account, Qt.Key.Key_Tab)
    assert start._source_page.archive_link.hasFocus()
    QTest.keyClick(start._source_page.archive_link, Qt.Key.Key_Tab)
    assert wizard.button(QWizard.WizardButton.BackButton).hasFocus()
    QTest.keyClick(wizard.button(QWizard.WizardButton.BackButton), Qt.Key.Key_Tab)
    assert next_button.hasFocus()
    QTest.keyClick(next_button, Qt.Key.Key_Space)
    assert wizard.currentId() == DOWNLOAD
    assert start._auth_btn.hasFocus()
    QTest.keyClick(start._auth_btn, Qt.Key.Key_Tab)
    assert wizard.button(QWizard.WizardButton.BackButton).hasFocus()
    QTest.keyClick(wizard.button(QWizard.WizardButton.BackButton), Qt.Key.Key_Tab)
    assert cancel.hasFocus()
    QTest.keyClick(cancel, Qt.Key.Key_Tab)
    assert start._auth_btn.hasFocus()


def test_wizard_archive_link_opens_with_enter(window, qapp, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtTest import QTest
    from vkdump.gui.start_view import ARCHIVE_URL

    opened = []
    monkeypatch.setattr(QDesktopServices, 'openUrl', lambda url: opened.append(url.toString()) or True)
    window.show()
    window.activateWindow()
    wizard = window._start_view._wizard
    wizard.next()
    qapp.processEvents()
    link = window._start_view._source_page.archive_link
    link.setFocus()
    QTest.keyClick(link, Qt.Key.Key_Return)
    assert opened == [ARCHIVE_URL]
    assert wizard.currentId() == SOURCE
