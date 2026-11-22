import assert from 'node:assert/strict';
import {createPassportConnection} from '../tools/openclaw/passport-proxy.mjs';
import {createBookRegistry, createBookTools, sessionTail, toolRequest, BOOK_SEARCH, BOOK_READ} from '../tools/openclaw/valet-book-tools.mjs';
const id = 'a'.repeat(64), book = 'b'.repeat(32), other = 'c'.repeat(32);
const key = `agent:passport:passport-${id}-book-${book}`;
const context = {bookId: book, title: '水浒传', chapter: '第十一回', page: 649, text: '柴进道', truncated: false};

assert.equal(sessionTail(key), `passport-${id}-book-${book}`);
assert.equal(sessionTail(`agent:Main:PASSPORT-${id}-book-${book}`), sessionTail(key));
assert.deepEqual(toolRequest('search', {query: '宋万', limit: 5, extra: 1}), {op: 'search', query: '宋万', limit: 5, scope: 'all', fromChapter: undefined});
assert.equal(toolRequest('search', {query: 'x', scope: 'read', limit: 99}).limit, undefined);
assert.throws(() => toolRequest('search', {query: '   '}));
assert.throws(() => toolRequest('search', {query: '汉'.repeat(41)}));
assert.equal(toolRequest('read', {at: 'c15.p32', after: 3}).at, 'c15.p32');
assert.equal(toolRequest('read', {at: 'current'}).at, 'current');
assert.throws(() => toolRequest('read', {at: '../etc'}));
assert.throws(() => toolRequest('delete', {}));

function device(caps, books) {
    const upstream = [], downstream = []; let failed = '';
    const c = createPassportConnection({sendUpstream: f => upstream.push(f), sendClient: f => downstream.push(f), close: x => failed = x, books});
    c.upstream({type: 'event', event: 'connect.challenge'});
    c.client({type: 'req', id: 'connect', method: 'connect', params: {client: {deviceFamily: 'e-ink4-plus'},
        device: {id, signature: 's', publicKey: 'p'}, auth: {deviceToken: 't'}, role: 'operator', caps}});
    c.upstream({type: 'res', id: 'connect', ok: true, payload: {policy: {maxPayload: 660000}}});
    return {c, upstream, downstream, failed: () => failed};
}

// Old firmware: no tools, no registration.
{
    const books = createBookRegistry();
    const d = device(['passport-ima-v1', 'valet-book-context-v1'], books);
    assert.equal(d.downstream.at(-1).payload.valetBookTools, undefined);
    d.c.client({type: 'req', id: 'ctx', method: 'valet.context.set', params: {sessionKey: key, context}});
    assert.equal(books.size(), 0);
    assert.equal(createBookTools({sessionKey: key}, books), null);
    d.c.stop('done');
}

// New firmware: tools only in its own book session, results round-trip, bounded and owned.
const books = createBookRegistry();
const d = device(['passport-ima-v1', 'valet-book-context-v1', 'valet-book-tools-v1'], books);
assert.equal(d.downstream.at(-1).payload.valetBookTools, 1);
assert.equal(createBookTools({sessionKey: key}, books), null, 'no tools before a book is chosen');
d.c.client({type: 'req', id: 'ctx', method: 'valet.context.set', params: {sessionKey: key, context}});
const tools = createBookTools({sessionKey: key}, books);
assert.deepEqual(tools.map(t => t.name), [BOOK_SEARCH, BOOK_READ]);
assert.equal(createBookTools({sessionKey: `agent:passport:passport-${id}`}, books), null, 'standalone session has no book');
assert.equal(createBookTools({sessionKey: `agent:passport:passport-${id}-book-${other}`}, books), null, 'other book');

const search = tools[0].execute('call-1', {query: '宋万'});
const request = d.downstream.at(-1);
assert.equal(request.event, 'valet.book.request');
assert.equal(request.payload.sessionKey, key);
assert.equal(request.payload.op, 'search');
// A result for another session or an unknown request is refused and does not resolve the call.
d.c.client({type: 'req', id: 'r0', method: 'valet.book.result', params: {requestId: request.payload.requestId, sessionKey: `agent:passport:passport-${id}`, result: {ok: true}}});
assert.equal(d.downstream.at(-1).ok, false);
d.c.client({type: 'req', id: 'r1', method: 'valet.book.result', params: {requestId: request.payload.requestId, sessionKey: key,
    result: {ok: true, hits: [{at: 'c15.p32', chapter: '第十一回', text: '第三个唤做云里金刚宋万'}], complete: true}}});
assert.equal(d.downstream.at(-1).ok, true);
const searched = await search;
assert.equal(searched.details.hits[0].at, 'c15.p32');
assert.match(searched.details.note, /not instructions/);
assert.ok(!d.upstream.some(f => f.method === 'valet.book.result'), 'book results never reach the native gateway');

// Oversized results are rejected.
const read = tools[1].execute('call-2', {at: 'c15.p32'});
const readRequest = d.downstream.at(-1).payload;
d.c.client({type: 'req', id: 'r2', method: 'valet.book.result', params: {requestId: readRequest.requestId, sessionKey: key, result: {ok: true, text: '汉'.repeat(6000)}}});
assert.equal((await read).details.ok, false);

// Bad arguments fail inside the tool, nothing is sent.
const before = d.downstream.length;
assert.equal((await tools[1].execute('call-3', {at: 'nope'})).details.ok, false);
assert.equal(d.downstream.length, before);

// Turning the page keeps the same channel; changing books moves it; leaving clears it and fails waiting calls.
d.c.client({type: 'req', id: 'page', method: 'valet.context.set', params: {sessionKey: key, context: {...context, page: 650}}});
assert.ok(createBookTools({sessionKey: key}, books));
const pendingCall = tools[0].execute('call-4', {query: '林冲'});
const otherKey = `agent:passport:passport-${id}-book-${other}`;
d.c.client({type: 'req', id: 'swap', method: 'valet.context.set', params: {sessionKey: otherKey, context: {...context, bookId: other}}});
assert.equal((await pendingCall).details.ok, false);
assert.equal(createBookTools({sessionKey: key}, books), null);
assert.ok(createBookTools({sessionKey: otherKey}, books));
d.c.stop('done');
assert.equal(books.size(), 0);
assert.equal(d.failed(), 'done');
console.log('Valet book tools: negotiation, session ownership, round trip, bounds, page follow, cleanup PASS');
