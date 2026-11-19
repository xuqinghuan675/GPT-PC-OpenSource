from unittest.mock import Mock

import pytest

from chatgpt_agent_store import AgentError
from test_chatgpt_agent_state import manager, make_caller


def test_startup_migrates_permission_without_creating_a_browser_or_watchdog(manager):
    manager.browser_factory = Mock(side_effect=AssertionError('must not start a browser'))
    manager.store.set_watchdog_scope('old-scope', enabled=True, mode='conversation', manual_disabled=False)
    result = manager.start_background_services()
    assert result['automatic_supervision'] is False
    manager.browser_factory.assert_not_called()
    state = manager.store.watchdog_scope('old-scope')
    assert state['enabled'] is False
    assert state['manual_enabled'] is True
    assert manager.store.watchdog_rows() == []


def test_manual_watchdog_permission_never_arms_the_legacy_automatic_monitor(manager):
    lease = make_caller(manager)
    result = manager.dispatch('watchdog_start', workspace_lease_id=lease)
    assert result['status'] == 'manual'
    assert result['automatic_supervision'] is False
    assert result['scope']['enabled'] is False
    assert result['scope']['manual_enabled'] is True
    assert manager.store.watchdog_scopes(enabled_only=True) == []
    assert manager.store.watchdog_rows() == []
    manager.dispatch('watchdog_stop', workspace_lease_id=lease)
    with pytest.raises(AgentError) as error:
        manager.dispatch('watchdog_start', workspace_lease_id=lease)
    assert error.value.code == 'WATCHDOG_MANUALLY_DISABLED'


@pytest.mark.parametrize('action', ['ensure_project', 'spawn', 'join', 'attach', 'followup', 'wake', 'interrupt'])
def test_legacy_browser_facades_fail_closed_without_web_side_effects(manager, action):
    lease = make_caller(manager)
    manager.runtime_facts = lambda _: {'ultra_mode': {'active': True}}
    manager.browser_factory = Mock(side_effect=AssertionError('must not drive a browser'))
    result = manager.dispatch(action, workspace_lease_id=lease, message='独立验收任务')
    assert result['status'] == 'legacy_flow_disabled'
    assert result['mode'] == 'legacy-readonly'
    assert result['task']['message'] == '独立验收任务'
    assert 'browser_helper_path' not in result
    assert '不会打开页面、点击、填写或发送' in result['instruction']
    assert '不得自行回退到旧手工浏览器编排' in result['instruction']
    assert manager.store.agents() == []
    manager.browser_factory.assert_not_called()
