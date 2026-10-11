"""ElevenAgents screenshot analysis with one worker and latest-capture priority."""
import base64
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from audio_player import AudioPlayer

CONFIG_PATH = Path(__file__).parent / '.analysis-config.json'
from action_flow import SYSTEM_PROMPT, result_tool, validate_result
PROMPT = 'Analyze this screenshot briefly and call publish_snapshot_result with one relevant offer.'


def configure_agent(key, agent):
    """Upgrade this dedicated agent, preserving its voice/model and unrelated settings."""
    current = request(key, 'convai/agents/' + agent)
    config = current['conversation_config']
    prompt = config['agent']['prompt']
    tools = prompt.get('tools', [])
    tool = result_tool()
    existing = next((item for item in tools if item.get('name') == tool['name']), None)
    if prompt.get('prompt') == SYSTEM_PROMPT and existing == tool:
        return
    # Update only our prompt/tool and required event subscription.
    request(key, 'convai/agents/' + agent, {
        'conversation_config': {
            'agent': {'prompt': {'prompt': SYSTEM_PROMPT,
                'tools': [item for item in tools if item.get('name') != tool['name']] + [tool]}},
            'conversation': {'client_events': list(dict.fromkeys(
                config.get('conversation', {}).get('client_events', []) +
                ['audio', 'agent_response', 'agent_response_complete', 'client_tool_call']))}}}, method='PATCH')



class AnalysisError(RuntimeError):
    pass


class Cancelled(Exception):
    pass


def tls_context():
    import certifi
    return ssl.create_default_context(cafile=certifi.where())


def agent_id():
    configured = os.environ.get('ELEVENLABS_AGENT_ID', '').strip()
    if configured:
        return configured
    try:
        return json.loads(CONFIG_PATH.read_text()).get('agent_id', '')
    except (OSError, ValueError):
        return ''


def request(key, path, data=None, method=None, content_type='application/json'):
    if isinstance(data, dict):
        data = json.dumps(data).encode()
    req = urllib.request.Request('https://api.elevenlabs.io/v1/' + path, data=data,
        headers={'xi-api-key': key, 'Content-Type': content_type}, method=method)
    # Repeating a GET or screenshot upload is safe; never retry agent creation/mutations.
    retryable = (data is None and method in (None, 'GET')) or path.endswith('/files')
    for attempt in range(2 if retryable else 1):
        try:
            with urllib.request.urlopen(req, timeout=15, context=tls_context()) as response:
                body = response.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            if retryable and attempt == 0 and exc.code in (408, 502, 503, 504):
                time.sleep(0.4)
                continue
            if exc.code in (401, 403):
                raise AnalysisError('ElevenLabs denied access; check the key and Agents permissions') from None
            if exc.code == 429:
                raise AnalysisError('ElevenLabs quota or rate limit reached') from None
            raise AnalysisError(f'ElevenLabs returned HTTP {exc.code}') from None
        except (urllib.error.URLError, TimeoutError):
            if retryable and attempt == 0:
                time.sleep(0.4)
                continue
            raise AnalysisError('Could not reach ElevenLabs; check the connection') from None



def upload(key, conversation, path):
    if path.stat().st_size > 15 * 1024 * 1024:
        raise AnalysisError('Screenshot exceeds the 15 MB upload limit')
    boundary = 'snapshotter-' + uuid.uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="capture.png"\r\n'
            'Content-Type: image/png\r\n\r\n').encode()
    body += path.read_bytes() + f'\r\n--{boundary}--\r\n'.encode()
    return request(key, f'convai/conversations/{conversation}/files', body,
                   content_type='multipart/form-data; boundary=' + boundary)['file_id']


class ElevenLabsAnalyzer:
    def __init__(self, events, agent, key_provider=None, player_factory=AudioPlayer, configure=False):
        if not agent:
            raise AnalysisError('Run python tools/setup_analysis.py to configure an analysis agent')
        if key_provider is None:
            from voice_control import get_api_key
            key_provider = get_api_key
        self._configure = configure
        self._require_result = configure
        self.events, self.agent = events, agent
        self.key_provider, self.player_factory = key_provider, player_factory
        self._condition = threading.Condition()
        self._generation = 0
        self._pending = None
        self._prompt = PROMPT
        self._closed = False
        self._player = None
        self._socket = None
        self._thread = threading.Thread(target=self._run, name='image-analysis', daemon=True)
        self._thread.start()

    def _invalidate(self):
        self._generation += 1
        if self._player:
            self._player.cancel()
        if self._socket:
            socket, self._socket = self._socket, None
            # Closing may wait for the peer; never block Qt while cancelling.
            threading.Thread(target=self._close_socket, args=(socket,), daemon=True).start()
        return self._generation

    @staticmethod
    def _close_socket(socket):
        try:
            socket.close()
        except Exception:
            pass

    def analyze_async(self, path, prompt=None):
        with self._condition:
            if self._closed:
                raise AnalysisError('Analysis is closed')
            generation = self._invalidate()
            self._pending = (generation, Path(path), prompt or PROMPT)
            self._condition.notify()
            return generation

    def cancel(self):
        with self._condition:
            self._invalidate()
            self._pending = None

    def close(self):
        with self._condition:
            self._closed = True
            self._invalidate()
            self._pending = None
            self._condition.notify()
        self._thread.join(timeout=0.5)

    def _current(self, generation):
        with self._condition:
            return not self._closed and generation == self._generation

    def _check(self, generation):
        if not self._current(generation):
            raise Cancelled()

    def _emit(self, generation, kind, *data):
        with self._condition:
            if not self._closed and generation == self._generation:
                self.events.put((kind, generation, *data))

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._pending is not None)
                if self._closed:
                    return
                generation, path, self._prompt = self._pending
                self._pending = None
            try:
                self._analyze(generation, path)
            except Cancelled:
                pass
            except Exception as exc:
                # Never echo arbitrary server errors, signed URLs, or API keys.
                message = str(exc) if isinstance(exc, AnalysisError) else (
                    f'Analysis failed ({type(exc).__name__}); check connection and audio output')
                self._emit(generation, 'analysis_failed', message)
            finally:
                if self._player:
                    try:
                        self._player.close()
                    except Exception:
                        pass
                    self._player = None
                self._socket = None
                self._emit(generation, 'analysis_finished')

    def _analyze(self, generation, path):
        from websockets.sync.client import connect
        key = self.key_provider()
        if not key:
            raise AnalysisError('ElevenLabs API key is missing')
        if not path.is_file():
            raise AnalysisError('Captured image is no longer available')
        self._emit(generation, 'analysis_started')
        if self._configure:
            configure_agent(key, self.agent)
            self._configure = False
            self._check(generation)
        signed = request(key, 'convai/conversation/get-signed-url?' +
                         urllib.parse.urlencode({'agent_id': self.agent}))['signed_url']
        self._check(generation)
        # Only send the credential-bearing URL to the expected ElevenLabs endpoint.
        url = urllib.parse.urlsplit(signed)
        if url.scheme != 'wss' or url.hostname != 'api.elevenlabs.io':
            raise AnalysisError('Unexpected ElevenLabs connection endpoint')
        with connect(signed, ssl=tls_context(), open_timeout=10, close_timeout=1, max_size=4*1024*1024) as ws:
            self._socket = ws
            ws.send(json.dumps({'type': 'conversation_initiation_client_data'}))
            conversation = None
            deadline = time.monotonic() + 15
            while conversation is None:
                self._check(generation)
                if time.monotonic() > deadline:
                    raise AnalysisError('ElevenLabs session setup timed out')
                try:
                    event = json.loads(ws.recv(timeout=0.2))
                except TimeoutError:
                    continue
                kind = event.get('type')
                if kind == 'ping':
                    ws.send(json.dumps({'type': 'pong', 'event_id': event['ping_event']['event_id']}))
                elif kind == 'conversation_initiation_metadata':
                    meta = event['conversation_initiation_metadata_event']
                    if meta.get('agent_output_audio_format') != 'pcm_16000':
                        raise AnalysisError('Configure the agent output as pcm_16000')
                    conversation = meta['conversation_id']
                    self._emit(generation, 'analysis_session', conversation)
                elif kind == 'client_error':
                    raise AnalysisError('ElevenLabs could not start the analysis session')
            file_id = upload(key, conversation, path)
            self._check(generation)
            ws.send(json.dumps({'type': 'multimodal_message',
                'text': {'type': 'user_message', 'text': self._prompt},
                'file': {'type': 'file_input', 'file_id': file_id}}))
            self._player = self.player_factory()
            deadline = time.monotonic() + 60
            last_audio = None
            text_seen, complete = False, False
            tool_seen = False
            while True:
                self._check(generation)
                now = time.monotonic()
                # Also check during pings, which otherwise keep the receive loop alive forever.
                if last_audio and text_seen and now - last_audio > (1.5 if complete else 3):
                    break
                if now > deadline:
                    raise AnalysisError('ElevenLabs analysis timed out')
                try:
                    event = json.loads(ws.recv(timeout=0.2))
                except TimeoutError:
                    continue
                kind = event.get('type')
                self._check(generation)
                if kind == 'ping':
                    ws.send(json.dumps({'type': 'pong', 'event_id': event['ping_event']['event_id']}))
                elif kind == 'audio':
                    if self._require_result and not tool_seen:
                        continue  # do not speak unvalidated guesses before the result tool
                    if last_audio is None:
                        ready = threading.Event()
                        self._emit(generation, 'speech_started', ready)
                        # The GUI must gate microphone input before any sound is played.
                        limit = time.monotonic() + 3
                        while not ready.wait(0.02):
                            self._check(generation)
                            if time.monotonic() > limit:
                                raise AnalysisError('Audio playback was not acknowledged')
                        self._check(generation)
                    pcm = base64.b64decode(event['audio_event']['audio_base_64'], validate=True)
                    self._player.feed(pcm)
                    last_audio = time.monotonic()
                elif kind == 'client_tool_call':
                    call = event['client_tool_call']
                    error = False
                    try:
                        if call.get('tool_name') != 'publish_snapshot_result' or tool_seen:
                            raise ValueError('Unsupported or duplicate result tool')
                        result = validate_result(call.get('parameters'))
                        if self._prompt == PROMPT and result['stage'] != 'offer':
                            raise ValueError('Initial screenshot must offer an action before drafting')
                        self._emit(generation, 'analysis_result', result)
                        tool_seen = True
                        reply = json.dumps({'spoken_text': result['spoken_text'],
                                            'saved': False})
                    except ValueError as exc:
                        error = True
                        reply = str(exc) + '. Correct the result and call the tool again.'
                    ws.send(json.dumps({'type': 'client_tool_result',
                        'tool_call_id': call['tool_call_id'], 'result': reply, 'is_error': error}))
                elif kind == 'agent_response':
                    text_seen = True
                    self._emit(generation, 'analysis_text', event['agent_response_event']['agent_response'])
                elif kind == 'agent_response_complete':
                    complete = True
                elif kind == 'client_error':
                    raise AnalysisError('ElevenLabs could not analyze this image; check model and file access')
            if self._require_result and not tool_seen:
                raise AnalysisError('No structured result received; rerun tools/setup_analysis.py')
            # Close the billable connection before waiting for local playback to finish.
        self._socket = None
        self._player.drain(lambda: not self._current(generation))
        self._check(generation)
