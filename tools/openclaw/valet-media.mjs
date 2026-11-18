import {randomBytes} from 'node:crypto';
import {spawn} from 'node:child_process';
import {encodeIma} from './passport-ima.mjs';

// Internal launch references never cross the device wire. A reference is bound to
// already-validated, connection-owned context, then removed after session creation.
export function createBookLaunches({now = Date.now} = {}) {
    const entries = new Map();
    function prune() { for (const [key, value] of entries) if (value.expires <= now()) entries.delete(key); }
    return {
        issue(context, model) {
            prune(); if (entries.size >= 32) throw Error('Book call launch slots are busy');
            const key = `valet-book:${randomBytes(24).toString('hex')}`;
            entries.set(key, {context: structuredClone(context), model, expires: now() + 30000}); return key;
        },
        get(key) { prune(); const value = entries.get(key); if (!value) throw Error('Book call launch expired'); return value; },
        remove(key) { entries.delete(key); }
    };
}
export const bookLaunches = createBookLaunches();

export function speechPcm(encoded, format, {spawnProcess = spawn, signal} = {}) {
    if (typeof encoded !== 'string' || encoded.length > 8 * 1024 * 1024 || !/^[A-Za-z0-9+/]*={0,2}$/.test(encoded))
        return Promise.reject(Error('Invalid speech audio'));
    const audio = Buffer.from(encoded, 'base64');
    if (!audio.length) return Promise.reject(Error('Empty speech audio'));
    if (format === 'pcm_24000') {
        if (audio.length % 2 || audio.length > 24000 * 2 * 120) return Promise.reject(Error('Invalid PCM speech'));
        return Promise.resolve(audio);
    }
    // Conversion is local, bounded, and has no network-capable demuxer.
    return new Promise((resolve, reject) => {
        const process = spawnProcess('ffmpeg', ['-hide_banner','-loglevel','error','-nostdin','-protocol_whitelist','pipe',
            '-i','pipe:0','-t','120','-ac','1','-ar','24000','-f','s16le','pipe:1'], {stdio:['pipe','pipe','pipe'], signal});
        let done = false, total = 0; const chunks = [];
        const finish = error => { if (done) return; done = true; clearTimeout(timer); signal?.removeEventListener('abort', abort); if (error) { process.kill(); reject(error); } else resolve(Buffer.concat(chunks)); };
        const abort = () => finish(Error('Speech cancelled'));
        const timer = setTimeout(() => finish(Error('Speech conversion timeout')), 20000); timer.unref?.();
        signal?.addEventListener('abort', abort, {once:true});
        if (signal?.aborted) return abort();
        process.stdout.on('data', chunk => { total += chunk.length; if (total > 24000*2*120) finish(Error('Speech too long')); else chunks.push(chunk); });
        process.stderr.on('data', () => {}); process.stdin.on('error', () => {});
        process.on('error', () => finish(Error('Install ffmpeg on the gateway for this speech format')));
        process.on('close', code => finish(code || !total || total % 2 ? Error('Speech conversion failed') : null));
        process.stdin.end(audio);
    });
}
export async function* speechFrames(pcm) {
    const state = {index:0}; let sequence = 0;
    for (let offset = 0; offset < pcm.length; offset += 3840) {
        const encoded = encodeIma(pcm.subarray(offset, offset + 3840), state);
        yield {type:'audio', audioBase64:'passport-ima:' + encoded.toString('base64'), sequence:++sequence};
    }
}

export async function openaiSpeech(text, config, {signal, fetchImpl = fetch} = {}) {
    if (typeof text !== 'string' || !text.trim() || Buffer.byteLength(text)>4096) throw Error('Invalid speech text');
    const {lstat,readFile} = await import('node:fs/promises');
    const {homedir} = await import('node:os');
    const variable = config.apiKeyEnv ?? 'OPENAI_API_KEY';
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(variable)) throw Error('Invalid speech credential variable');
    let key = process.env[variable];
    if (!key && config.apiKeyFile) {
        const file = config.apiKeyFile.replace(/^~(?=\/)/, homedir());
        const stat = await lstat(file);
        if (!stat.isFile() || stat.isSymbolicLink() || stat.mode & 0o077 || stat.size>4096) throw Error('Speech key file must be private (0600)');
        key = (await readFile(file,'utf8')).trim();
    }
    if (typeof key!=='string' || !key || /\s/.test(key)) throw Error('OpenAI speech credential unavailable');
    const url = new URL(config.url ?? 'https://api.openai.com/v1/audio/speech');
    if (url.protocol!=='https:' || /\s/.test(config.url ?? '') || url.username || url.password || url.search || url.hash) throw Error('Speech requires credential-free HTTPS');
    const model=config.model ?? 'gpt-4o-mini-tts', voice=config.voice ?? 'coral';
    if ([model,voice].some(x=>typeof x!=='string'||!x||x.length>128||/\s/.test(x))) throw Error('Invalid speech model or voice');
    const response = await fetchImpl(url,{method:'POST',redirect:'error',signal:AbortSignal.any([AbortSignal.timeout(60000),...(signal?[signal]:[])]),
        headers:{Authorization:`Bearer ${key}`,'Content-Type':'application/json'},body:JSON.stringify({input:text,model,voice,response_format:'pcm'})});
    if (!response.ok) { await response.body?.cancel(); throw Error('OpenAI speech request failed; check credentials and model'); }
    const chunks=[];let length=0;
    for await(const chunk of response.body) { length+=chunk.length; if(length>24000*2*120) { throw Error('Speech exceeds device limit'); } chunks.push(chunk); }
    if(!length||length%2)throw Error('Invalid OpenAI PCM speech');
    return Buffer.concat(chunks);
}
