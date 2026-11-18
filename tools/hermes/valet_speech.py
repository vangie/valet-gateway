"""Bounded local TTS conversion; credentials stay in the Hermes host runtime."""
import asyncio
import base64
import json
from pathlib import Path
import tempfile
from urllib.parse import urlsplit
from aiohttp import ClientSession, ClientTimeout
from .passport_auth import ProtocolError
from .passport_ima import encode_ima

MAX_PCM = 24000 * 2 * 120

def speech_config(extra, get_secret):
    config = extra.get("speech", {})
    if not isinstance(config, dict): raise ProtocolError("Invalid speech configuration")
    if config.get("provider", "host") == "host": return None
    if config.get("provider") != "openai": raise ProtocolError("Unsupported speech provider")
    variable = config.get("api_key_env", "OPENAI_API_KEY")
    if not isinstance(variable, str) or not variable.isidentifier(): raise ProtocolError("Invalid speech credential variable")
    key = get_secret(variable)
    file = config.get("api_key_file") or extra.get("realtime", {}).get("api_key_file")
    if not key and file:
        path = Path(file).expanduser()
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077 or path.stat().st_size > 4096:
            raise ProtocolError("Speech key file must be private (0600)")
        key = path.read_text().strip()
    if not isinstance(key, str) or not key or any(c.isspace() for c in key): raise ProtocolError("OpenAI speech credential unavailable", "UNAVAILABLE")
    url = config.get("url", "https://api.openai.com/v1/audio/speech")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or any(c.isspace() for c in url):
        raise ProtocolError("Speech requires a credential-free HTTPS endpoint")
    model = config.get("model", "gpt-4o-mini-tts"); voice = config.get("voice", "coral")
    if any(not isinstance(v,str) or not v or len(v)>128 or any(c.isspace() for c in v) for v in (model,voice)):
        raise ProtocolError("Invalid speech model or voice")
    return dict(url=url,key=key,model=model,voice=voice)

async def openai_speech(text, config):
    if not isinstance(text,str) or not text.strip() or len(text.encode())>4096: raise ProtocolError("Invalid reply for speech")
    async with ClientSession(timeout=ClientTimeout(total=60)) as client:
        async with client.post(config["url"],headers={"Authorization":"Bearer "+config["key"]},
            json=dict(model=config["model"],voice=config["voice"],input=text,response_format="pcm"),allow_redirects=False) as response:
            if response.status != 200: raise ProtocolError("OpenAI speech request failed; check host credentials and model", "UNAVAILABLE")
            pcm=bytearray()
            async for chunk in response.content.iter_chunked(8192):
                pcm.extend(chunk)
                if len(pcm)>MAX_PCM: raise ProtocolError("Speech output exceeded device limit")
    if not pcm or len(pcm)%2: raise ProtocolError("Invalid OpenAI PCM speech")
    return bytes(pcm)

async def synthesize(text, scope):
    if not isinstance(text, str) or not text.strip() or len(text.encode()) > 4096:
        raise ProtocolError('Invalid reply for speech')
    from tools.tts_tool import text_to_speech_tool
    with tempfile.TemporaryDirectory(prefix='valet-speech-') as directory:
        destination = Path(directory) / 'reply.wav'
        def generate():
            with scope():
                return json.loads(text_to_speech_tool(text=text, output_path=str(destination)))
        # Thread-based host synthesis is awaited even on cancellation so temporary
        # output cannot be reused after its owning operation has disappeared.
        task = asyncio.create_task(asyncio.to_thread(generate))
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
        paths = result.get('file_paths') or [result.get('file_path')]
        if not result.get('success') or len(paths) != 1 or not isinstance(paths[0], str):
            raise ProtocolError('Configure a working Hermes TTS provider', 'UNAVAILABLE')
        path = Path(paths[0]).resolve()
        if path.parent != Path(directory).resolve() or not path.is_file() or path.stat().st_size > 8*1024*1024:
            raise ProtocolError('Speech output was not a bounded local file')
        process = await asyncio.create_subprocess_exec('ffmpeg','-hide_banner','-loglevel','error','-nostdin',
            '-protocol_whitelist','file,pipe','-i',str(path),'-t','120','-ac','1','-ar','24000','-f','s16le','pipe:1',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        pcm = bytearray()
        try:
            async with asyncio.timeout(20):
                while chunk := await process.stdout.read(8192):
                    pcm.extend(chunk)
                    if len(pcm) > MAX_PCM: raise ProtocolError('Speech output exceeded device limit')
                if await process.wait() != 0 or not pcm or len(pcm)%2: raise ProtocolError('Speech conversion failed')
        finally:
            if process.returncode is None: process.kill(); await process.wait()
        return bytes(pcm)

async def stream_speech(pcm, send, run, authorized, cancelled):
    if not pcm or len(pcm)%2 or len(pcm)>MAX_PCM: raise ProtocolError('Invalid PCM speech')
    index = 0
    if cancelled() or not authorized(): return
    await send({'type':'start', 'runId':run, 'sampleRate':24000})
    for offset in range(0, len(pcm), 3840):
        if cancelled() or not authorized(): return
        encoded, index = encode_ima(pcm[offset:offset+3840], index)
        await send({'type':'audio','runId':run,'sequence':offset//3840+1,
                    'audioBase64':'passport-ima:'+base64.b64encode(encoded).decode()})
        await asyncio.sleep(len(pcm[offset:offset+3840]) / 48000)
    if not cancelled() and authorized(): await send({'type':'done','runId':run})
