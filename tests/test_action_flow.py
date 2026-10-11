import json
import queue
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from action_flow import ActionFlow, calendar_times, reply_intent, save_draft, validate_result
from voice_control import VoiceControl
from test_analysis import app


def result(stage='offer', action='brain_dump', **fields):
    return validate_result({'stage': stage, 'action': action, 'summary': 'A useful idea.', **fields})


def test_brief_summary_and_single_relevant_offer():
    value = result(action='calendar_event', summary='word ' * 90)
    assert len(value['summary'].split()) == 35
    assert value['spoken_text'].endswith('Want me to draft a calendar event?')
    assert value['spoken_text'].count('?') == 1


@pytest.mark.parametrize('fields', [
    {'start': '2026-10-20T18:00:00', 'end': '2026-10-20T19:00:00'},
    {'start': '2026-10-20T18:00:00-07:00', 'end': ''},
    {'start': '2026-10-20', 'end': '2026-10-20'},
    {'start': '2026-10-20T19:00:00-07:00', 'end': '2026-10-20T18:00:00-07:00'},
])
def test_calendar_cannot_be_drafted_without_complete_valid_times(fields):
    with pytest.raises(ValueError):
        result('draft', 'calendar_event', title='Workshop', **fields)


def test_calendar_export_escapes_folds_and_keeps_exact_time(tmp_path):
    flow = ActionFlow()
    flow.reset(tmp_path / 'capture.png')
    flow.receive(result(action='calendar_event'))
    flow.follow_up('yes')
    flow.receive(result('draft', 'calendar_event', title='Workshop, photo; fun',
                        body='Café ' * 40 + '\nBring camera', location='Room 2',
                        start='2026-10-20T18:00:00-07:00', end='2026-10-20T19:00:00-07:00'))
    paths = save_draft(flow, tmp_path / 'Items')
    calendar = paths[1].read_bytes()
    assert b'DTSTART:20261021T010000Z\r\n' in calendar
    assert b'SUMMARY:Workshop\\, photo\\; fun' in calendar
    assert b'\\nBring camera' in calendar
    assert all(len(line) <= 75 for line in calendar.split(b'\r\n'))
    assert paths[0].stat().st_mode & 0o777 == 0o600


def test_all_day_calendar_preserves_exclusive_end():
    value = result('draft', 'calendar_event', title='Festival', start='2026-10-20', end='2026-10-21')
    assert calendar_times(value) == ('VALUE=DATE', '20261020', '20261021')


def test_memory_needs_users_own_words_and_stores_them_separately(tmp_path):
    flow = ActionFlow()
    flow.reset(tmp_path / 'photo.png')
    flow.receive(result(action='memory'))
    flow.follow_up('yes')
    with pytest.raises(ValueError):
        flow.receive(result('draft', 'memory', title='Sunset', body='Coastal sunset.', user_description='Invented memory'))
    description = 'My first evening here with my sister.'
    flow.follow_up(description)
    flow.receive(result('draft', 'memory', title='Sunset', body='Coastal sunset.', user_description=description))
    saved = save_draft(flow, tmp_path)[0].read_text()
    assert 'Coastal sunset.' in saved and 'Your description: ' + description in saved


def test_no_save_for_offer_and_no_automatic_initial_draft(tmp_path):
    flow = ActionFlow()
    flow.reset(tmp_path / 'photo.png')
    flow.receive(result())
    with pytest.raises(ValueError):
        save_draft(flow, tmp_path)
    with pytest.raises(ValueError):
        flow.receive(result('draft', title='Note', body='Content'))
    assert not list(tmp_path.glob('**/*.md'))


def test_context_contains_followup_and_conversation_is_bounded(tmp_path):
    flow = ActionFlow()
    flow.reset(tmp_path / 'slide.png')
    flow.receive(result(action='questions'))
    prompt = flow.follow_up('Explain the equation')
    assert 'Explain the equation' in prompt and 'quoted data' in prompt
    for _ in range(11):
        flow.follow_up('Another question')
    with pytest.raises(ValueError):
        flow.follow_up('One more')


def test_scribe_reply_mode_uses_final_only_and_never_capture_words():
    events = queue.Queue()
    voice = VoiceControl(events)
    voice.set_reply_mode(42)
    voice.handle_message({'message_type': 'partial_transcript', 'text': 'capture this memory'})
    assert events.empty()
    voice.handle_message({'message_type': 'committed_transcript', 'text': 'capture this memory'})
    assert events.get_nowait() == ('reply', 42, 'capture this memory')
    assert events.empty()


def test_stopped_voice_listener_cannot_emit_late_commands():
    voice = VoiceControl(queue.Queue())
    voice.stop()
    voice.handle_message({'message_type': 'committed_transcript', 'text': 'capture'})
    assert voice.events.empty()


@pytest.mark.parametrize('text,intent', [('yes please', 'accept'), ('no thanks', 'dismiss'),
                                       ('do not save it', 'reply'), ('explain this', 'reply')])
def test_reply_intents_never_accept_embedded_yes(text, intent):
    assert reply_intent(text) == intent


def prepare(app, tmp_path):
    app.args.items_out = str(tmp_path / 'Items')
    app.action_flow.reset(tmp_path / 'capture.png')
    app.action_flow.receive(result())
    app.awaiting_reply = True
    app.reply_token = 12
    app.analyzer = Mock()
    app.analyzer.analyze_async.return_value = 50
    app.voice_control = Mock()


def test_first_yes_prepares_but_does_not_save_and_second_yes_saves(app, tmp_path):
    prepare(app, tmp_path)
    app.handle_reply(12, 'yes')
    assert not list(tmp_path.glob('**/*.md'))
    assert app.analysis_id == 50
    assert 'yes' in app.analyzer.analyze_async.call_args.kwargs['prompt']
    app.analysis_event(('analysis_result', 50, result('draft', title='An idea', body='Try weekly planning.')))
    app.analysis_event(('analysis_finished', 50))
    assert app.awaiting_reply
    app.handle_reply(app.reply_token, 'yes')
    assert len(list(tmp_path.glob('**/*.md'))) == 1
    assert not app.awaiting_reply


def test_stale_reply_and_paused_app_never_save(app, tmp_path):
    prepare(app, tmp_path)
    app.handle_reply(11, 'yes')
    app.paused = True
    app.handle_reply(12, 'yes')
    app.analyzer.analyze_async.assert_not_called()
    assert not list(tmp_path.glob('**/*.md'))


def test_no_dismisses_without_action_and_cancels_listener(app, tmp_path):
    prepare(app, tmp_path)
    app.handle_reply(12, 'no thanks')
    assert not app.awaiting_reply and app.action_flow.image is None
    app.analyzer.analyze_async.assert_not_called()


def test_reply_timeout_returns_to_idle_without_saving(app, tmp_path):
    prepare(app, tmp_path)
    app.reply_deadline = time.monotonic() - 1
    app.tick()
    assert not app.awaiting_reply and app.reply_deadline is None
    assert not list(tmp_path.glob('**/*.md'))


def test_save_failure_preserves_draft_for_retry(app, tmp_path, monkeypatch):
    prepare(app, tmp_path)
    app.action_flow.follow_up('yes')
    app.action_flow.receive(result('draft', title='Note', body='Content'))
    monkeypatch.setattr('snapshotter.save_draft', Mock(side_effect=OSError('disk full')))
    app.handle_reply(12, 'yes')
    assert app.awaiting_reply and app.action_flow.result['stage'] == 'draft'


def test_config_upgrade_preserves_voice_model_and_existing_tools(monkeypatch):
    import image_analyzer as module
    existing = {'conversation_config': {'agent': {'prompt': {'prompt': 'Old', 'llm': 'gpt-4o',
        'tools': [{'name': 'other_tool', 'type': 'client'}]}},
        'conversation': {'client_events': ['audio']}, 'tts': {'voice_id': 'my-voice'}}}
    request = Mock(side_effect=[existing, {}])
    monkeypatch.setattr(module, 'request', request)
    module.configure_agent('secret', 'test')
    update = request.call_args.args[2]['conversation_config']
    assert 'tts' not in update and 'llm' not in update['agent']['prompt']
    assert update['agent']['prompt']['tools'][0]['name'] == 'other_tool'
    assert 'client_tool_call' in update['conversation']['client_events']


def test_transient_upload_timeout_retries_once_but_agent_creation_does_not(monkeypatch):
    import io
    import urllib.error
    import image_analyzer as module
    failure = urllib.error.HTTPError('https://api.elevenlabs.io', 408, 'timeout', {}, io.BytesIO())
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock()
    response.read.return_value = b'{"file_id":"file-test"}'
    urlopen = Mock(side_effect=[failure, response])
    monkeypatch.setattr(module.urllib.request, 'urlopen', urlopen)
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    assert module.request('secret', 'convai/conversations/test/files', b'png')['file_id'] == 'file-test'
    assert urlopen.call_count == 2
    urlopen.reset_mock(side_effect=True)
    urlopen.side_effect = failure
    with pytest.raises(module.AnalysisError):
        module.request('secret', 'convai/agents/create', {})
    assert urlopen.call_count == 1
