import asyncio
import importlib.util
import json
from pathlib import Path
import secrets
import tempfile

from aiohttp import web, WSMsgType
from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from hermes_cli.config import get_env_value, get_hermes_home

from .passport_auth import DeviceStore, ProtocolError
from .valet_context import validate_context
from .valet_book_tools import BOOK_TOOLS_CAP, BookChannel, register_tools, registry as book_registry
from .valet_speech import synthesize, stream_speech, speech_config, openai_speech
from .passport_protocol import MAX_AUDIO_BYTES, MAX_FRAME_BYTES, PassportProtocol, wire_json
from .passport_speech import speech_language, transcription_hints
from .passport_talk import TalkSession, talk_config


class PassportAdapter(BasePlatformAdapter):
    REQUIRES_EDIT_FINALIZE = True

    def __init__(self, config):
        super().__init__(config, Platform("ai_passport"))
        extra = config.extra or {}
        self.host = extra.get("host", "127.0.0.1")
        self.port = extra.get("port", 18790)
        self.speech_language = extra.get("speech_language", "")
        if self.speech_language not in {"", "zh", "en"}:
            raise ValueError("speech_language must be zh, en, or an empty string for automatic detection")
        if self.host != "127.0.0.1" or type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("Passport must listen on 127.0.0.1 with a valid port; use a TLS tunnel for remote access")
        self.home = get_hermes_home()
        self.store = None
        self.protocol = None
        self.runner = None
        self.clients = set()
        self.turns = {}
        self.pending = set()
        self.calls = {}
        self.completions = {}
        self.extra = extra

    async def connect(self, *, is_reconnect=False):
        if self.runner:
            return True
        if not self._acquire_platform_lock("ai_passport", str(self.home), "Passport device store"):
            return False
        try:
            self.store = DeviceStore(self.home / "passport" / "devices.sqlite3")
            self.protocol = PassportProtocol(self.store)
            application = web.Application(client_max_size=MAX_FRAME_BYTES)
            application.router.add_get("/", self.websocket)
            application.router.add_get("/passport/v1", self.websocket)
            self.runner = web.AppRunner(application, access_log=None)
            await self.runner.setup()
            await web.TCPSite(self.runner, self.host, self.port).start()
        except BaseException:
            await self.disconnect()
            raise
        self._mark_connected()
        return True

    async def disconnect(self):
        for call in tuple(self.calls.values()):
            await call.close("disconnected")
        self.calls.clear()
        for client in tuple(self.clients):
            await client.close(code=1001, message=b"Adapter stopping")
        pending = tuple(self.pending)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for key in tuple(self._session_tasks):
            await self.cancel_session_processing(key)
        if self.runner:
            await self.runner.cleanup()
            self.runner = None
        if self.store:
            self.store.close()
            self.store = None
        self._release_platform_lock()
        self._mark_disconnected()

    async def websocket(self, request):
        if request.headers.get("Origin") or len(self.clients) >= 4:
            raise web.HTTPForbidden(text="Signed devices only; at most four connections")
        websocket = web.WebSocketResponse(max_msg_size=MAX_FRAME_BYTES, heartbeat=20, compress=False)
        if not websocket.can_prepare(request).ok:
            raise web.HTTPBadRequest(text="Signed WebSocket connection required")
        self.clients.add(websocket)
        challenge = self.protocol.challenge()
        identity = None
        sequence = 0
        capabilities = []
        scopes = []
        call = None
        reading = None
        book_feature = False
        speech_feature = False
        speech_task = None
        speech_tasks = set()
        book_tools_feature = False
        book_channel = None

        def select_book_channel():
            # Book tools follow the selected book session: only the book this device chose, only while online.
            nonlocal book_channel
            key = reading[0] if reading and reading[1] else None
            if book_channel is not None and book_channel.session_key == key:
                return
            if book_channel is not None:
                book_registry.detach(book_channel)
                book_channel.close()
                book_channel = None
            if key and book_tools_feature:
                book_channel = BookChannel(send, key, asyncio.get_running_loop())
                book_registry.attach(book_channel)

        async def send(frame):
            nonlocal sequence
            if frame.get("type") == "event":
                if identity is not None and not self.store.authorized(identity, token):
                    return
                sequence += 1
                frame = {**frame, "seq": sequence}
            if not websocket.closed:
                try:
                    await asyncio.wait_for(websocket.send_json(frame, dumps=wire_json), 3)
                except (ConnectionError, TimeoutError):
                    await websocket.close()

        try:
            await websocket.prepare(request)
            await send({"type": "event", "event": "connect.challenge", "payload": challenge})
            while not websocket.closed:
                message = await websocket.receive(timeout=15 if identity is None else None)
                if message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                    break
                request_id = None
                try:
                    if message.type != WSMsgType.TEXT:
                        raise ProtocolError("JSON text frames required")
                    frame = json.loads(message.data)
                    if not isinstance(frame, dict):
                        raise ProtocolError("Invalid request")
                    request_id = frame.get("id")
                    method = frame.get("method")
                    params = frame.get("params", {})
                    if (frame.get("type") != "req" or not isinstance(request_id, str) or not 1 <= len(request_id) <= 80 or
                            not isinstance(method, str) or not isinstance(params, dict)):
                        raise ProtocolError("Invalid request")
                    if identity is None:
                        if method != "connect":
                            raise ProtocolError("Authenticate first", "AUTH_FAILED")
                        identity, token = self.store.authenticate(params, challenge["nonce"], challenge["ts"])
                        capabilities = params.get("caps", [])
                        book_feature = isinstance(capabilities, list) and "valet-book-context-v1" in capabilities
                        speech_feature = isinstance(capabilities, list) and "valet-ask-speech-v1" in capabilities
                        book_tools_feature = book_feature and BOOK_TOOLS_CAP in capabilities
                        scopes = params["scopes"]
                        self.protocol.subscribers.setdefault(identity, set()).add(send)
                        result = {"type": "hello-ok", "protocol": 3, "passportProtocol": 1, "passportAsk": True, "backend": "hermes",
                                  "policy": {"maxPayload": MAX_FRAME_BYTES, "attachments": {"maxBytes": MAX_AUDIO_BYTES}},
                                  "auth": {"role": "operator", "scopes": params["scopes"], "deviceToken": token}}
                        if book_feature: result.update(valetBookContext=1, valetBookLive=True)
                        if speech_feature: result["valetAskSpeech"] = 1
                        if book_tools_feature: result["valetBookTools"] = 1
                    elif not self.store.authorized(identity, token):
                        if call:
                            await call.close("revoked")
                        raise ProtocolError("Device credential revoked", "AUTH_FAILED")
                    elif not book_feature and "-book-" in str(params.get("sessionKey", params.get("key", ""))):
                        raise ProtocolError("Book context was not negotiated", "NOT_SUPPORTED")
                    elif method == "valet.speech.cancel":
                        if not speech_feature: raise ProtocolError("Speech was not negotiated")
                        if speech_task: speech_task.cancel()
                        result = {}
                    elif method == "valet.speech.start":
                        if not speech_feature or "operator.talk" not in scopes or (call and not call.closed) or (speech_task and not speech_task.done()):
                            raise ProtocolError("Speech is unavailable or busy", "BUSY")
                        key = reading[0] if reading else f"agent:passport:passport-{identity}"
                        if params.get("sessionKey") != key: raise ProtocolError("Speech session mismatch", "FORBIDDEN")
                        run = self.store.db.execute("SELECT * FROM runs WHERE device=? AND session=? AND run=? AND state='final'", (identity,key,params.get("runId"))).fetchone()
                        if not run: raise ProtocolError("Completed reply not found", "NOT_FOUND")
                        record = dict(zip([column[1] for column in self.store.db.execute("PRAGMA table_info(runs)")], run))
                        async def speak(record=record, key=key):
                            async def speech_send(payload):
                                await send({"type":"event", "event":"valet.speech", "payload":{"sessionKey":key, **payload}})
                            try:
                                source = self.build_source(chat_id=identity, user_id=identity, role_authorized=True)
                                with self._media_delivery_scope(source): config = speech_config(self.extra, get_env_value)
                                pcm = await openai_speech(record["text"], config) if config else await synthesize(record["text"], lambda: self._media_delivery_scope(source))
                                await stream_speech(pcm, speech_send, record["run"], lambda: self.store is not None and self.store.authorized(identity,token), lambda: websocket.closed)
                            except asyncio.CancelledError: raise
                            except Exception:
                                await speech_send({"type":"error", "runId":record["run"], "message":"Configure a working Hermes TTS provider and ffmpeg"})
                        if len(speech_tasks) >= 2: raise ProtocolError("Previous speech is still stopping", "BUSY")
                        speech_task = asyncio.create_task(speak())
                        speech_tasks.add(speech_task)
                        speech_task.add_done_callback(speech_tasks.discard)
                        result = {"runId": record["run"]}
                    elif method == "valet.context.set":
                        if not book_feature: raise ProtocolError("Book context was not negotiated")
                        next_reading = validate_context(identity, params)
                        # During a call only the page may change (same book session): consultations then use the new page.
                        in_call = call is not None and not call.closed
                        if in_call and next_reading[0] != (reading[0] if reading else None):
                            raise ProtocolError("End the call first", "BUSY")
                        if speech_task and not in_call: speech_task.cancel()
                        reading = next_reading
                        select_book_channel()
                        result = {"sessionKey": reading[0]}
                    elif method == "valet.book.result":
                        if book_channel is None or not book_channel.result(params):
                            raise ProtocolError("No book request is waiting for this result", "NOT_FOUND")
                        result = {}
                    elif book_feature and (params.get("sessionKey", params.get("key")) is not None and
                            params.get("sessionKey", params.get("key")) != (reading[0] if reading else f"agent:passport:passport-{identity}")):
                        raise ProtocolError("Conversation context does not match selected session", "FORBIDDEN")
                    elif method == "chat.send":
                        if speech_task: speech_task.cancel()
                        if identity in self.calls and not self.calls[identity].closed:
                            raise ProtocolError("End the call before asking a separate question", "BUSY")
                        request_key = params.get("idempotencyKey")
                        existing = self.protocol.get_run(identity, request_key) if isinstance(request_key, str) else None
                        if not existing and len(self.pending) >= 4:
                            raise ProtocolError("Voice preparation slots are busy", "BUSY")
                        run, content, fresh = self.protocol.prepare(identity, {**params, "_valet_context": reading[1] if reading else None,
                                                                                "_valet_book_tools": book_channel is not None})
                        await send({"type": "res", "id": request_id, "ok": True, "payload": {"runId": run["run"]}})
                        if fresh:
                            task = asyncio.create_task(self.submit(identity, run, content))
                            self.pending.add(task)
                            task.add_done_callback(self.pending.discard)
                        continue
                    elif method == "chat.abort":
                        if speech_task: speech_task.cancel()
                        await self.abort(identity, params)
                        result = {}
                    elif method == "passport.status":
                        available = self.realtime_config(identity) is not None
                        result = {"protocol": 1, "backend": "hermes", "ask": True,
                                  "upstreamAvailable": available, "upstreamConfigured": available, "providerRegistered": True}
                    elif method == "talk.session.create":
                        if speech_task: speech_task.cancel()
                        if "operator.talk" not in scopes:
                            raise ProtocolError("Signed talk scope required", "FORBIDDEN")
                        key = params.get("sessionKey")
                        synthetic = key == f"agent:passport:passport-{identity}-synthetic"
                        self.protocol.session(identity, key.removesuffix("-synthetic") if synthetic else key)
                        if (params.get("mode") != "realtime" or params.get("transport") != "gateway-relay" or
                                params.get("brain") != "agent-consult" or params.get("language") not in {"zh", "en"} or
                                not isinstance(capabilities, list) or "passport-ima-v1" not in capabilities):
                            raise ProtocolError("Unsupported Passport call format")
                        running = self.store.db.execute("SELECT 1 FROM runs WHERE device = ? AND state = 'running'", (identity,)).fetchone()
                        if identity in self.calls and not self.calls[identity].closed or running:
                            raise ProtocolError("A question or call is already active", "BUSY")
                        config = self.realtime_config(identity)
                        if config is None:
                            raise ProtocolError("Realtime voice is not configured", "UNAVAILABLE")
                        config = {**config, "synthetic": synthetic, **({"reading_context": reading[1]} if reading and reading[1] else {})}
                        call_key = key
                        call_context = reading[1] if reading else None
                        persona_path = self.home / "SOUL.md"
                        persona = bounded_persona(persona_path)
                        async def consult(question, context):
                            # The page may have turned since the call began (same book): use the current one.
                            current = reading[1] if reading and reading[0] == call_key and reading[1] else call_context
                            return await self.consult(identity, question, context, session_key=call_key, reading_context=current)
                        call = TalkSession(identity, config, params["language"], persona, send, consult,
                                           lambda: self.store is not None and self.store.authorized(identity, token))
                        self.calls[identity] = call
                        await send({"type": "res", "id": request_id, "ok": True, "payload": call.description()})
                        call.reader = asyncio.create_task(call.start())
                        continue
                    elif method.startswith("talk.session."):
                        if call is None or call.closed or params.get("sessionId") != call.id:
                            raise ProtocolError("Call does not belong to this connection", "FORBIDDEN")
                        if method == "talk.session.appendAudio":
                            await call.append(params)
                        elif method == "talk.session.cancelOutput":
                            await call.interrupt()
                        elif method == "talk.session.acknowledgeMark":
                            await call.acknowledge(params.get("markName"))
                        elif method == "talk.session.close":
                            await call.close()
                            self.calls.pop(identity, None)
                        else:
                            raise ProtocolError("Unsupported call operation", "METHOD_NOT_FOUND")
                        result = {}
                    else:
                        result = self.protocol.dispatch(identity, method, params)
                    await send({"type": "res", "id": request_id, "ok": True, "payload": result})
                except (ProtocolError, ValueError, TypeError, AttributeError, OSError) as error:
                    await send({"type": "res", "id": request_id, "ok": False,
                                "error": {"code": getattr(error, "code", "INVALID_REQUEST"),
                                          "message": str(error) if isinstance(error, ProtocolError) else "Invalid request"}})
                    if identity is None:
                        break
        except (ConnectionError, TimeoutError):
            pass
        finally:
            if book_channel is not None:
                book_registry.detach(book_channel)
                book_channel.close()
            for speech in tuple(speech_tasks): speech.cancel()
            await asyncio.gather(*tuple(speech_tasks), return_exceptions=True)
            if call:
                await call.close("disconnected")
                if self.calls.get(identity) is call:
                    self.calls.pop(identity)
            self.clients.discard(websocket)
            subscribers = self.protocol.subscribers.get(identity)
            if subscribers is not None:
                subscribers.discard(send)
                if not subscribers:
                    self.protocol.subscribers.pop(identity, None)
            if websocket.prepared:
                await websocket.close()
        return websocket

    def realtime_config(self, identity):
        source = self.build_source(chat_id=identity, user_id=identity, role_authorized=True)
        with self._media_delivery_scope(source):
            return talk_config(self.extra, get_env_value)

    async def consult(self, identity, question, context, *, session_key=None, reading_context=None):
        request = "voice-" + secrets.token_hex(12)
        text = question + ("\n\nRelevant voice conversation context:\n" + context if context else "")
        from .valet_book_tools import registry as book_registry
        run, content, _ = self.protocol.prepare(identity, {"sessionKey": session_key or f"agent:passport:passport-{identity}", "_valet_context":reading_context,
                                                          "_valet_book_tools": bool(session_key and book_registry.get(session_key)),
                                                          "idempotencyKey": request, "message": text})
        future = asyncio.get_running_loop().create_future()
        self.completions[request] = future
        try:
            await self.submit(identity, run, content)
            result = await future
            if result["state"] != "final":
                raise ProtocolError("Hermes task failed or was cancelled")
            return {"answer": result["text"]}
        finally:
            self.completions.pop(request, None)
            await self.abort(identity, {"sessionKey": run["session"], "runId": run["run"]})

    async def submit(self, identity, run, content):
        question, audio = content
        audio_path = None
        source = self.build_source(chat_id=identity, chat_name="AI Passport", user_id=identity,
                                   user_name="Passport owner", role_authorized=True)
        source.thread_id = run["session"].split("-book-", 1)[1] if "-book-" in run["session"] else None
        grant = self.store.db.execute("SELECT token_hash FROM devices WHERE id = ?", (identity,)).fetchone()
        source.user_id_alt = "passport-grant-v1:" + grant[0] if grant else None
        event = MessageEvent(text=question, message_type=MessageType.TEXT,
                             source=source, message_id=run["request"], allow_gateway_control=False)
        try:
            if not await self.reauthorize_native_roles(source):
                raise ProtocolError("Device credential revoked", "AUTH_FAILED")
            if audio:
                cache = self.home / "passport" / "audio"
                cache.mkdir(mode=0o700, parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=cache, suffix=".wav", delete=False) as output:
                    audio_path = Path(output.name)
                    output.write(audio)
                from tools.transcription_tools import transcribe_audio

                language_token = speech_language.set(self.speech_language)
                try:
                    with self._media_delivery_scope(source):
                        recognition = asyncio.create_task(asyncio.to_thread(transcribe_audio, str(audio_path), source="ai_passport"))
                        try:
                            transcription = await asyncio.shield(recognition)
                        except asyncio.CancelledError:
                            await recognition
                            raise
                finally:
                    speech_language.reset(language_token)
                transcript = transcription.get("transcript")
                if not transcription.get("success") or not isinstance(transcript, str) or not transcript.strip():
                    raise ProtocolError("Voice transcription is unavailable")
                event.text = question + transcript.strip()
                await self.protocol.update(identity, run["request"], transcript=transcript.strip())
            if self.protocol.get_run(identity, run["request"])["state"] != "running":
                if audio_path:
                    audio_path.unlink(missing_ok=True)
                return
            self.turns[identity] = {"request": run["request"], "path": audio_path, "event": event}
            await self.handle_message(event)
            if not event._gateway_accepted:
                raise ProtocolError("Hermes did not accept this question")
        except (Exception, asyncio.CancelledError):
            await self.protocol.update(identity, run["request"], state="error")
            self.complete(run["request"], self.protocol.get_run(identity, run["request"]))
            if self.turns.get(identity, {}).get("request") == run["request"]:
                self.turns.pop(identity)
            if audio_path:
                audio_path.unlink(missing_ok=True)

    async def reauthorize_native_roles(self, source):
        if (self.store is None or source.platform != self.platform or source.chat_type != "dm" or
                source.user_id != source.chat_id or (source.thread_id is not None and (not isinstance(source.thread_id, str) or len(source.thread_id) != 32 or any(c not in "0123456789abcdef" for c in source.thread_id))) or not source.user_id_alt):
            return False
        grant = self.store.db.execute("SELECT token_hash FROM devices WHERE id = ?", (source.chat_id,)).fetchone()
        return bool(grant and secrets.compare_digest(source.user_id_alt, "passport-grant-v1:" + grant[0]))

    async def abort(self, identity, params):
        self.protocol.session(identity, params.get("sessionKey"))
        run_id = params.get("runId")
        if not isinstance(run_id, str):
            raise ProtocolError("Invalid task identity")
        row = self.store.db.execute("SELECT request, state FROM runs WHERE device = ? AND run = ? AND session = ?", (identity, run_id, params.get("sessionKey"))).fetchone()
        if not row:
            raise ProtocolError("Unknown task", "NOT_FOUND")
        if row[1] != "running":
            return
        turn = self.turns.get(identity)
        if not turn:
            await self.protocol.update(identity, row[0], state="aborted")
            return
        control = MessageEvent(text="/stop", source=turn["event"].source, message_id=f"abort-{run_id}")
        await self.handle_message(control)
        await self.protocol.update(identity, row[0], state="aborted")

    async def get_chat_info(self, chat_id):
        return {"name": "AI Passport", "type": "dm"}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        turn = self.turns.get(chat_id)
        if not turn:
            return SendResult(success=False, error="No active Passport question")
        if metadata and metadata.get("_interim_send"):
            return SendResult(success=True, message_id=turn["request"])
        await self.protocol.update(chat_id, turn["request"], text=content)
        return SendResult(success=True, message_id=turn["request"])

    async def edit_message(self, chat_id, message_id, content, *, finalize=False):
        turn = self.turns.get(chat_id)
        if not turn or message_id != turn["request"]:
            return SendResult(success=False, error="Unknown Passport question")
        return await self.send(chat_id, content)

    async def on_processing_complete(self, event, outcome):
        identity = event.source.chat_id
        turn = self.turns.get(identity)
        if not turn or turn["request"] != event.message_id:
            return
        self.turns.pop(identity)
        try:
            run = self.protocol.get_run(identity, turn["request"])
            complete = outcome.value == "success" and run and bool(run["text"].strip())
            await self.protocol.update(identity, turn["request"], state="final" if complete else "error")
            self.complete(turn["request"], self.protocol.get_run(identity, turn["request"]))
        finally:
            if turn["path"]:
                turn["path"].unlink(missing_ok=True)

    def complete(self, request, result):
        future = self.completions.get(request)
        if future and not future.done():
            future.set_result(result)


def check_requirements():
    return all(importlib.util.find_spec(name) is not None for name in ("aiohttp", "cryptography"))


def bounded_persona(path):
    if not path.is_file():
        return "A helpful personal Hermes assistant."
    with path.open("r", encoding="utf-8") as stream:
        return stream.read(8192)


def register(ctx):
    ctx.register_platform(name="ai_passport", label="AI Passport Agent", adapter_factory=PassportAdapter,
                          check_fn=check_requirements, max_message_length=0,
                          platform_hint="The user asks voice questions on a small wearable display. Reply concisely in the user's language.")
    ctx.register_hook("pre_transcription", transcription_hints)
    register_tools(ctx)
