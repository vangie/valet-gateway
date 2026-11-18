# Device protocol v1

The wire contract continues Passport v1; repository extraction is not a protocol version bump.

1. TLS WebSocket to `/passport/v1`; receive `connect.challenge`.
2. Sign protocol-v3 device identity proof with Ed25519. Use an unconsumed `bootstrapToken` on first connect, then per-device `deviceToken` on later connects. Keys persist on the device; the adapter validates proof before authorizing a session.
3. Ask: `sessions.messages.subscribe`, `chat.send` with a bounded 16 kHz mono mu-law WAV attachment; consume run-bound `chat` events, `passport.transcript`, and `chat.history` fallback. Cancel only the owned run with `chat.abort`.
4. Live: `talk.session.create` (`realtime`, `gateway-relay`, `agent-consult`), `appendAudio` at 24 kHz PCM16 or negotiated Passport IMA, `cancelOutput`, playback `acknowledgeMark`, `submitToolResult`, `close`. Events arrive through `talk.event`, bound to `relaySessionId` with playback sequencing.
5. Device families currently accepted: `esp32c3 / ai-passport`, `esp32s31 / e-ink4-plus`. A family is part of the signed proof and cannot be relabeled after signing.

Reserved for the next compatible extension: explicit capability negotiation for book contexts and independent sessions, plus a shared onboarding page/prompt module. Unsupported capabilities must not be advertised as ready. Do not silently reuse a general conversation for a book session.
