import plugin
import signin


def test_signin_uses_the_plugin_clock(run, monkeypatch):
    run()
    s = plugin._s()
    monkeypatch.setattr(plugin, "_mono", lambda: 1000.0)
    s.flow_backoff = 60.0
    signin.retry_device_code_later(s, plugin._hooks())
    assert s.flow_retry_at == 1060.0
