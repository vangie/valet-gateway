import {randomBytes} from 'node:crypto';
import {bookLaunches, speechPcm, speechFrames, openaiSpeech} from './valet-media.mjs';
import {isIP} from 'node:net';
import {validateContext, bookPrompt} from './valet-context.mjs';
import {BOOK_TOOLS_CAP, bookRegistry, createBookChannel} from './valet-book-tools.mjs';
import {createPassportRelayOutput} from './passport-relay.mjs';
import {PASSPORT_PATH, PASSPORT_PROTOCOL, PASSPORT_PROVIDER} from './passport-plugin.mjs';

const MAX_FRAME_BYTES = 660000;
const MAX_PENDING_BYTES = 512 * 1024;
const MAX_PENDING_REQUESTS = 64;

export function gatewayTarget(config) {
    const port = config?.gateway?.port ?? 18789;
    if (!Number.isInteger(port) || port < 1 || port > 65535 || config?.gateway?.tls?.enabled)
        throw new Error('Passport requires a supported local gateway endpoint; run the Passport doctor');
    return `ws://127.0.0.1:${port}/`;
}

export function forwardedAddress(req) {
    const address = req.socket.remoteAddress?.replace(/^::ffff:/, '');
    return address && isIP(address) && address !== '::1' && !address.startsWith('127.') ? address : '192.0.2.1';
}

export function createPassportConnection({sendUpstream, sendClient, close, now, schedule, cancel, synthesizeSpeech, books = bookRegistry}) {
    let phase = 'challenge', connectId, createId, sessionId, ima = false, closed = false;
    let downstreamSequence = 0, upstreamSequence;
    let cancellingOutput = false;
    const requests = new Map();
    let deviceIdentity, bookFeature = false, speechFeature = false, talkAllowed = false, reading = null, launchRef;
    let bookToolsFeature = false, bookChannel = null;
    // Book tools follow the selected book session: the assistant can search / read only the book this device chose.
    function selectBookChannel() {
        const key = reading?.context ? reading.sessionKey : null;
        if (bookChannel && bookChannel.sessionKey === key) return;
        if (bookChannel) { books.detach(bookChannel.sessionKey, bookChannel); bookChannel.close(); bookChannel = null; }
        if (key && bookToolsFeature && phase === 'ready') { bookChannel = createBookChannel({emit, sessionKey: key}); books.attach(key, bookChannel); }
    }
    let speechJob = null, speechPending = null, speechTimer = null;
    const replies = new Map();
    function cancelSpeech() { clearTimeout(speechTimer); if (speechJob) speechJob.abort(); speechJob = null; speechPending = null; }
    async function deliverSpeech(frame, pending) {
        const controller = new AbortController(); speechJob = controller;
        const event = payload => { if (!closed && !controller.signal.aborted && speechJob === controller)
            emit({type:'event', event:'valet.speech', payload:{runId:pending.run, sessionKey:pending.key, ...payload}}); };
        try {
            if (!frame.ok) throw Error('The configured Talk TTS provider is unavailable');
            const pcm = pending.text && synthesizeSpeech ? await synthesizeSpeech(pending.text,controller.signal) :
                await speechPcm(frame.payload?.audioBase64, frame.payload?.outputFormat, {signal:controller.signal});
            if (controller.signal.aborted) return;
            event({type:'start', sampleRate:24000});
            for await (const chunk of speechFrames(pcm)) {
                if (controller.signal.aborted || closed) break;
                event(chunk);
                await new Promise(resolve => setTimeout(resolve, 80));
            }
            event({type:'done'});
        } catch { event({type:'error', message:'Configure a working Talk TTS provider and ffmpeg on the gateway'}); }
        finally { if (speechJob === controller) speechJob = null; }
    }
    function rememberReply(payload) {
        const key = reading?.sessionKey ?? `agent:passport:passport-${deviceIdentity}`;
        if (payload?.sessionKey !== key || payload.state !== 'final' || typeof payload.runId !== 'string') return;
        const content = payload.message?.content;
        const text = typeof content === 'string' ? content : Array.isArray(content) ? content.filter(x=>x.type==='text').map(x=>x.text).join('') : '';
        if (speechFeature && text && Buffer.byteLength(text) <= 4096) {
            if (replies.size >= 8) replies.delete(replies.keys().next().value);
            replies.set(payload.runId, {key, text});
        }
    }
    function emit(frame) {
        if (frame.type === 'event' && frame.event !== 'connect.challenge') frame = {...frame, seq: ++downstreamSequence};
        sendClient(frame);
    }
    const makeOutput = () => createPassportRelayOutput({enabled: true, ima: true,
        send: payload => emit({type: 'event', event: 'talk.event', payload}),
        fail: () => stop('Passport playback queue exceeded its bound'), now, schedule, cancel});
    let output = makeOutput();
    function resetSession() {
        output.close(); output = makeOutput(); sessionId = undefined; cancellingOutput = false;
    }
    function stop(reason) {
        if (closed) return;
        closed = true; cancelSpeech(); if (launchRef) bookLaunches.remove(launchRef);
        if (bookChannel) { books.detach(bookChannel.sessionKey, bookChannel); bookChannel.close(); bookChannel = null; }
        output.close(); requests.clear(); close(reason);
    }
    function reject(id, message) {
        emit({type: 'res', id, ok: false, error: {code: 'INVALID_REQUEST', message}});
    }
    function track(frame) {
        const pending = [...requests.values()].reduce((total, request) => total + request.count, 0);
        if (!frame.id || pending >= MAX_PENDING_REQUESTS || requests.has(frame.id) && requests.get(frame.id).method !== frame.method)
            throw new Error('Passport request window exceeded');
        const request = requests.get(frame.id) ?? {method: frame.method, key:frame.params?.sessionKey ?? frame.params?.key, count: 0};
        request.count++; requests.set(frame.id, request);
    }
    function client(frame) {
        if (closed) return;
        try {
            if (frame.type !== 'req' || typeof frame.id !== 'string' || typeof frame.method !== 'string')
                throw new Error('Invalid Passport request');
            if (phase === 'connect' && frame.method === 'connect') {
                const params = frame.params;
                if (!['ai-passport', 'e-ink4-plus'].includes(params?.client?.deviceFamily) || params.role !== 'operator' ||
                    !params.device?.signature || !params.device?.publicKey ||
                    !params.auth?.deviceToken && !params.auth?.bootstrapToken && !params.auth?.token)
                    throw new Error('Passport requires native signed device authentication');
                deviceIdentity = params.device.id;
                bookFeature = params.caps?.includes('valet-book-context-v1') === true;
                speechFeature = params.caps?.includes('valet-ask-speech-v1') === true;
                bookToolsFeature = bookFeature && params.caps?.includes(BOOK_TOOLS_CAP) === true;
                connectId = frame.id;
                ima = params.caps?.includes('passport-ima-v1') === true;
                phase = 'authenticating';
                sendUpstream(frame);
                return;
            }
            if (speechFeature && frame.id.startsWith('valet-tts-')) return reject(frame.id, 'Reserved request identity');
            if (phase !== 'ready') throw new Error('Passport device is not authenticated');
            if (frame.method === 'valet.speech.cancel') {
                if (!speechFeature) return reject(frame.id, 'Speech was not negotiated');
                cancelSpeech(); emit({type:'res',id:frame.id,ok:true,payload:{}}); return;
            }
            if (frame.method === 'valet.speech.start') {
                const reply = replies.get(frame.params?.runId);
                const currentKey = reading?.sessionKey ?? `agent:passport:passport-${deviceIdentity}`;
                if (!speechFeature || !talkAllowed || sessionId || speechJob || speechPending || !reply || reply.key !== currentKey || frame.params?.sessionKey !== currentKey)
                    return reject(frame.id, 'Speech requires a completed reply owned by this session');
                const internalId = `valet-tts-${randomBytes(12).toString('hex')}`;
                speechPending = {id:internalId, run:frame.params.runId, key:currentKey};
                speechTimer = setTimeout(() => {
                    if (speechPending?.id !== internalId) return;
                    const pending = speechPending; cancelSpeech();
                    emit({type:'event',event:'valet.speech',payload:{type:'error',runId:pending.run,sessionKey:pending.key,message:'Speech generation timed out'}});
                }, 60000); speechTimer.unref?.();
                emit({type:'res',id:frame.id,ok:true,payload:{runId:frame.params.runId}});
                if (synthesizeSpeech) { clearTimeout(speechTimer); speechPending=null; deliverSpeech({ok:true}, {run:frame.params.runId,key:currentKey,text:reply.text}); }
                else sendUpstream({type:'req',id:internalId,method:'talk.speak',params:{text:reply.text,outputFormat:'pcm_24000'}}); return;
            }
            if (frame.method === 'valet.context.set') {
                if (!bookFeature) return reject(frame.id, 'Book context was not negotiated');
                let next;
                try { next = validateContext(deviceIdentity, frame.params); }
                catch (error) { return reject(frame.id, error.message); }
                // During a call only the page may change (same book session): consultations then use the new page.
                if ((sessionId || createId) && next.sessionKey !== reading?.sessionKey) return reject(frame.id, 'End the call before switching context');
                if (!sessionId && !createId) { cancelSpeech(); replies.clear(); }
                reading = next; selectBookChannel();
                emit({type: 'res', id: frame.id, ok: true, payload: {sessionKey: reading.sessionKey}});
                return;
            }
            if (frame.method === 'valet.book.result') {
                if (!bookChannel || !bookChannel.result(frame.params)) return reject(frame.id, 'No book request is waiting for this result');
                emit({type: 'res', id: frame.id, ok: true, payload: {}});
                return;
            }
            if (bookFeature) {
                const key = frame.params?.sessionKey ?? frame.params?.key;
                if (key !== undefined && key !== (reading?.sessionKey ?? `agent:passport:passport-${deviceIdentity}`))
                    return reject(frame.id, 'Conversation context does not match selected session');

                if (reading?.context && frame.method === 'chat.send') {
                    if (typeof frame.params?.message !== 'string' || frame.params.message.length > 4096) return reject(frame.id, 'Invalid question');
                    frame = {...frame, params: {...frame.params, message: bookPrompt(reading.context) + frame.params.message}};
                }
            }
            if (frame.method === 'talk.session.create') {
                if (createId || sessionId) return reject(frame.id, 'Close the active Passport call before creating another');
                if (!ima) return reject(frame.id, 'Passport firmware requires passport-ima-v1');
                if (frame.params?.mode !== 'realtime' || frame.params.transport !== 'gateway-relay' || frame.params.brain !== 'agent-consult')
                    return reject(frame.id, 'Unsupported Passport call mode');
                cancelSpeech();
                if (reading?.context) {
                    launchRef = bookLaunches.issue(reading.context, frame.params.model);
                    frame = {...frame, params:{...frame.params, model:launchRef}};
                }
                createId = frame.id;
                frame = {...frame, params: {...frame.params, provider: PASSPORT_PROVIDER}};
            } else if (frame.method.startsWith('talk.session.') || frame.method === 'talk.client.toolCall') {
                const owner = frame.params?.sessionId ?? frame.params?.relaySessionId;
                if (!sessionId || owner !== sessionId) return reject(frame.id, 'Passport session ownership mismatch');
                if (reading?.context && frame.method === 'talk.client.toolCall') {
                    const args = frame.params?.args;
                    if (!args || typeof args !== 'object' || Array.isArray(args)) return reject(frame.id, 'Invalid consultation args');
                    frame = {...frame, params:{...frame.params, args:{...args, context:bookPrompt(reading.context) + (typeof args.context === 'string' ? args.context : '')}}};
                }
                if (frame.method === 'talk.session.appendAudio') {
                    const decoded = output.decodeAudio(frame.params.audioBase64);
                    if (!decoded) return reject(frame.id, 'Passport audio format was not negotiated');
                    frame = {...frame, params: {...frame.params, audioBase64: decoded.toString('base64')}};
                }
                if (frame.method === 'talk.session.cancelOutput') {
                    if (cancellingOutput) return reject(frame.id, 'Passport interruption is already pending');
                    cancellingOutput = true;
                    output.discardOutput();
                }
            }
            if (['chat.send','chat.abort'].includes(frame.method)) cancelSpeech();
            track(frame); sendUpstream(frame);
        } catch { stop('Invalid Passport protocol or unsupported audio'); }
    }
    function upstream(frame) {
        if (closed) return;
        try {
            if (frame.type === 'res' && speechPending?.id === frame.id) {
                const pending = speechPending; speechPending = null; clearTimeout(speechTimer);
                deliverSpeech(frame, pending); return;
            }
            if (frame.type === 'res' && frame.id?.startsWith('valet-tts-')) return;
            if (frame.type === 'event' && frame.event === 'chat') rememberReply(frame.payload);
            if (frame.type === 'res' && requests.get(frame.id)?.method === 'chat.history' && frame.ok) {
                const key = requests.get(frame.id).key;
                for (const message of frame.payload?.messages ?? []) if (message.role === 'assistant' && ['stop','end_turn'].includes(message.stopReason))
                    rememberReply({sessionKey:key, state:'final', runId:message.__openclaw?.runId, message});
            }
            if (frame.type === 'event' && frame.seq !== undefined) {
                if (!Number.isSafeInteger(frame.seq) || frame.seq < 0 || upstreamSequence !== undefined && frame.seq > upstreamSequence + 1)
                    throw new Error('Native gateway event sequence gap');
                upstreamSequence = frame.seq;
            }
            if (frame.event === 'connect.challenge' && phase === 'challenge') phase = 'connect';
            if (frame.type === 'res' && frame.id === connectId) {
                connectId = undefined;
                if (!frame.ok) { emit(frame); return stop('Native gateway device authentication failed'); }
                phase = 'ready';
                talkAllowed = frame.payload?.auth?.scopes?.some(s => ['operator.talk','operator.admin'].includes(s)) === true;
                const upstreamLimit = frame.payload?.policy?.maxPayload;
                const maxPayload = Number.isSafeInteger(upstreamLimit) && upstreamLimit > 0 ?
                    Math.min(upstreamLimit, MAX_FRAME_BYTES) : MAX_FRAME_BYTES;
                frame = {...frame, payload: {...frame.payload, passportProtocol: PASSPORT_PROTOCOL, passportAsk: true, ...(bookFeature ? {valetBookContext: 1, valetBookLive: true} : {}), ...(speechFeature ? {valetAskSpeech:1} : {}),
                    ...(bookToolsFeature ? {valetBookTools: 1} : {}),
                    policy: {...frame.payload?.policy, maxPayload}}};
            }
            const request = frame.type === 'res' ? requests.get(frame.id) : undefined;
            const method = request?.method;
            if (request && !--request.count) requests.delete(frame.id);
            if (frame.type === 'res' && frame.id === createId) {
                createId = undefined;
                if (launchRef) { bookLaunches.remove(launchRef); launchRef = undefined; }
                if (frame.ok) {
                    if (!frame.payload?.sessionId || frame.payload.provider !== PASSPORT_PROVIDER ||
                        frame.payload.audio?.inputEncoding !== 'pcm16' || frame.payload.audio?.inputSampleRateHz !== 24000 ||
                        frame.payload.audio?.outputEncoding !== 'pcm16' || frame.payload.audio?.outputSampleRateHz !== 24000)
                        throw new Error('Native Talk session contract changed');
                    sessionId = frame.payload.sessionId;
                    frame = {...frame, payload: {...frame.payload, passportIma: true, passportSilence: true, passportProtocol: PASSPORT_PROTOCOL}};
                }
            }
            if (method === 'talk.session.close' && frame.ok) resetSession();
            if (method === 'talk.session.cancelOutput') {
                output.discardOutput(); cancellingOutput = false;
                if (frame.ok) output.send({relaySessionId: sessionId, type: 'clear'});
            }
            if (frame.event === 'talk.event') {
                const payload = frame.payload;
                if (!payload || !sessionId || payload.relaySessionId !== sessionId) return;
                if (cancellingOutput && (['responseStarted', 'audio', 'mark', 'audioDone'].includes(payload.type) ||
                    ['transcript', 'partial'].includes(payload.type) && payload.role === 'assistant')) return;
                if (payload.type === 'close') {
                    resetSession(); emit(frame); return;
                }
                output.send(payload);
            } else emit(frame);
        } catch { stop('Native Talk protocol is incompatible; run the Passport doctor'); }
    }
    return {client, upstream, stop, phase: () => phase, diagnostics: () => output.diagnostics()};
}

export function createPassportTransport({WebSocket, WebSocketServer, config, maxConnections = 4, speech}) {
    const target = gatewayTarget(config);
    const server = new WebSocketServer({noServer: true, maxPayload: MAX_FRAME_BYTES, perMessageDeflate: false});
    const connections = new Set();
    function upgrade(req, socket, head) {
        if (new URL(req.url, 'http://localhost').pathname !== PASSPORT_PATH) return false;
        if (connections.size >= maxConnections) {
            socket.end('HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n\r\n');
            return true;
        }
        server.handleUpgrade(req, socket, head, downstream => {
            const upstream = new WebSocket(target, {maxPayload: MAX_FRAME_BYTES, perMessageDeflate: false,
                headers: {'X-Forwarded-For': forwardedAddress(req), 'X-Forwarded-Proto': 'https'}});
            let stopped = false, deadline;
            function send(peer, frame) {
                if (stopped) return;
                if (peer.readyState !== WebSocket.OPEN || peer.bufferedAmount > MAX_PENDING_BYTES)
                    return stop('Passport transport unavailable');
                peer.send(JSON.stringify(frame), error => { if (error) stop('Passport transport write failed'); });
            }
            function stop(reason) {
                if (stopped) return;
                stopped = true;
                clearTimeout(deadline);
                connection.stop(reason);
                connections.delete(stop);
                for (const peer of [upstream, downstream]) {
                    if (peer.readyState === WebSocket.OPEN) peer.close(1011, reason.slice(0, 100));
                    else peer.terminate();
                }
            }
            const connection = createPassportConnection({
                sendUpstream: frame => send(upstream, frame),
                sendClient: frame => send(downstream, frame),
                close: stop,
                ...(speech?.provider === 'openai' ? {synthesizeSpeech:(text,signal)=>openaiSpeech(text,speech,{signal})} : {})
            });
            connections.add(stop);
            deadline = setTimeout(() => stop('Passport native device authentication timed out'), 30000);
            deadline.unref?.();
            function receive(handler, data, binary) {
                if (stopped) return;
                try {
                    if (binary) throw new Error('Unexpected binary frame');
                    handler(JSON.parse(data.toString()));
                    if (connection.phase() === 'ready') { clearTimeout(deadline); deadline = undefined; }
                } catch { stop('Invalid Passport WebSocket frame'); }
            }
            upstream.on('message', (data, binary) => receive(connection.upstream, data, binary));
            downstream.on('message', (data, binary) => receive(connection.client, data, binary));
            upstream.on('ping', data => { if (downstream.readyState === WebSocket.OPEN) downstream.ping(data); });
            downstream.on('ping', data => { if (upstream.readyState === WebSocket.OPEN) upstream.ping(data); });
            for (const peer of [upstream, downstream]) {
                peer.on('error', () => stop('Passport upstream transport failed'));
                peer.on('close', () => stop('Passport transport closed'));
            }
        });
        return true;
    }
    return {upgrade, stop() { for (const close of [...connections]) close('Passport plugin stopped'); server.close(); },
        diagnostics: () => ({connections: connections.size, maxConnections, protocol: PASSPORT_PROTOCOL})};
}
