# Device protocol v1

The wire contract continues Passport v1; repository extraction is not a protocol version bump.

1. TLS WebSocket to `/passport/v1`; receive `connect.challenge`.
2. Sign protocol-v3 device identity proof with Ed25519. Use an unconsumed `bootstrapToken` on first connect, then per-device `deviceToken` on later connects. Keys persist on the device; the adapter validates proof before authorizing a session.
3. Ask: `sessions.messages.subscribe`, `chat.send` with a bounded 16 kHz mono mu-law WAV attachment; consume run-bound `chat` events, `passport.transcript`, and `chat.history` fallback. Cancel only the owned run with `chat.abort`.
4. Live: `talk.session.create` (`realtime`, `gateway-relay`, `agent-consult`), `appendAudio` at 24 kHz PCM16 or negotiated Passport IMA, `cancelOutput`, playback `acknowledgeMark`, `submitToolResult`, `close`. Events arrive through `talk.event`, bound to `relaySessionId` with playback sequencing.
5. Device families currently accepted: `esp32c3 / ai-passport`, `esp32s31 / e-ink4-plus`. A family is part of the signed proof and cannot be relabeled after signing.

## Optional book context v1 (v0.2.0)

A device opts in with `caps: ["valet-book-context-v1"]`. Only opted-in connections receive `valetBookContext: 1` and `valetBookLive: true` in hello. Old Passport requests, IDs, credentials, audio formats and standalone session keys remain unchanged. v0.1.0 does **not** include this extension.

After authentication and before subscription/recording, send `valet.context.set`:

```json
{"type":"req","id":"context","method":"valet.context.set","params":{"sessionKey":"agent:passport:passport-<device-id>-book-<32 lowercase hex book-id>","context":{"bookId":"<same book-id>","title":"三国演义","chapter":"第一回","page":2,"text":"当前可见页正文","truncated":false}}}
```

Wait for success with the exact returned `sessionKey` before sending audio. To switch to standalone, end the old turn/call and use the original device session key with `context: null`. Disconnect clears the connection-local context; the device must select it again on reconnect. A book key must belong to the signed identity; page is one-based, title/chapter <=240 UTF-8 bytes, text <=12000 UTF-8 bytes. Failed selection must not fall back to a standalone question.

`chat.send` keeps its existing audio attachment. Hermes combines the selected page with the transcribed question and gives each book a distinct native `thread_id`; history, transcript and abort are session-scoped. OpenClaw prepends the selected page as referenced user content in `chat.send.message`, retaining its owned book session key. Device keys include the assistant and stable file identity; changing pages keeps one book conversation, changing files creates another. Page text is quoted data, never a tool permission or system instruction. The instruction not to remember it automatically is a model instruction, not a guarantee that the backend stores no chat logs.

Book Live uses the selected page in the realtime provider instructions and carries it through tool consultations, in the same owned book session. Close the call before changing its page/context. The OpenClaw adapter uses a bounded, expiring opaque launch reference internally to inject page context into its own realtime provider; the reference is removed after creation and is never a model name sent to a provider. Old clients never create such references.

## Optional completed-answer speech v1

A device opts in using `valet-ask-speech-v1`; hello returns `valetAskSpeech: 1`. To speak a **completed** answer send `valet.speech.start` with `sessionKey` and `runId`. The gateway reads the owned final answer, never accepts arbitrary device text for synthesis, and requires `operator.talk`. Events use `event: "valet.speech"` with payload fields `sessionKey`, `runId`, `type` (`start`, `audio`, `done`, `error`). Audio includes a monotonically increasing `sequence` and `audioBase64` in the negotiated `passport-ima:` format, 24 kHz mono. `valet.speech.cancel` stops only the current connection's speech; starting another turn, switching context, closing the connection or revoking authority suppresses late output. Frames are paced at audio duration and bounded to 120 seconds; text remains available if TTS fails. Only one speech/call per connection is admitted. A gateway's optional speech feature does not imply host credentials or provider readiness.

OpenClaw defaults to its configured `talk.speak`; Hermes defaults to its host TTS. Both can select plugin-local OpenAI TTS using a private existing host credential. The host-global TTS provider is untouched. An OpenAI TTS key must have access to the chosen speech model (default `gpt-4o-mini-tts`, voice `coral`). No key or generated audio file path is returned to the device. Page text is supplied to Live, not sent to a separate TTS endpoint; TTS receives only the completed assistant answer.

All opt-in features are independent of old Passport functionality. No deployed gateway is upgraded automatically.
