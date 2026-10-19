"""Actual ASGI/production connector/real WebSocket closure; offline only."""
import asyncio
import base64
import hashlib
import json
import struct
import time

import pytest
from websockets.asyncio.client import connect as RealConnect
from gateway import speech
from gateway.adapters.xai_tts import connect_provider
from test_speech import Provider,Socket,connected,make_router


def frame(body, opcode=1):
    payload=json.dumps(body).encode() if opcode==1 else body
    size=bytes([len(payload)]) if len(payload)<126 else b'\x7e'+struct.pack('!H',len(payload))
    return bytes([0x80|opcode])+size+payload


async def read_frame(reader):
    header=await reader.readexactly(2);length=header[1]&127
    if length==126:length=struct.unpack('!H',await reader.readexactly(2))[0]
    if length==127:length=struct.unpack('!Q',await reader.readexactly(8))[0]
    mask=await reader.readexactly(4) if header[1]&128 else None
    body=await reader.readexactly(length)
    if mask:body=bytes(v^mask[i%4] for i,v in enumerate(body))
    return header[0]&15,body


@pytest.fixture
async def peer(monkeypatch):
    state={'delay':0,'connections':[],'exits':0,'attempts':0,'sessions':set(),
           'closing':asyncio.Event(),'kind':'audio.done','clear_controls':0}
    async def serve(reader,writer):
        task=asyncio.current_task();state['sessions'].add(task)
        try:
            headers=await reader.readuntil(b'\r\n\r\n')
            key=next(v.split(b':',1)[1].strip() for v in headers.split(b'\r\n') if v.lower().startswith(b'sec-websocket-key:'))
            accept=base64.b64encode(hashlib.sha1(key+b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
            writer.write(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: '+accept+b'\r\n\r\n');await writer.drain()
            for _ in range(2):
                opcode,raw=await read_frame(reader);assert opcode==1
            writer.write(frame({'type':'audio.delta','delta':base64.b64encode(b'\x01\x00'*128).decode()})+frame({'type':state['kind']}));await writer.drain()
            while True:
                opcode,raw=await read_frame(reader)
                if opcode==8:break
                if opcode==1:state['clear_controls']+=1
            if state['delay'] is None:
                assert await reader.read(1)==b''
            else:
                await asyncio.sleep(state['delay'])
                writer.write(frame(raw,8));await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass  # Deliberately forced local abort in owner tests.
        finally:
            writer.close()
            try:await writer.wait_closed()
            except ConnectionError:pass
            state['sessions'].discard(task)
    listener=await asyncio.start_server(serve,'127.0.0.1',0)
    port=listener.sockets[0].getsockname()[1]
    class Local(RealConnect):
        def __init__(self,uri,**kw):
            assert uri.startswith('wss://api.x.ai/v1/tts?')
            assert kw['close_timeout']==1 and kw['max_size']==262144
            state['attempts']+=1
            super().__init__(f'ws://127.0.0.1:{port}',**kw)
        async def __aenter__(self):
            upstream=await super().__aenter__();state['connections'].append(upstream);return upstream
        async def __aexit__(self,*args):
            state['exits']+=1;state['closing'].set()
            if state.get('exit_stall'):
                await asyncio.Future()
            if state.get('exit_error'):
                if state['exit_error']=='timeout':
                    raise TimeoutError('private provider detail')
                raise RuntimeError('private provider detail')
            await super().__aexit__(*args)
    monkeypatch.setattr('websockets.asyncio.client.connect',Local)
    yield state
    listener.close();await listener.wait_closed()
    for upstream in state['connections']:
        if not upstream.connection_lost_waiter.done():upstream.transport.abort()
        await upstream.wait_closed()
    await asyncio.gather(*state['sessions'])
    assert not state['sessions']


def assert_closed(state):
    assert state['attempts']==state['exits']==1
    assert all(c.connection_lost_waiter.done() and c.transport.is_closing() for c in state['connections'])


async def begin(tmp_path,peer):
    app,engine,_,key,_,_=await make_router(tmp_path,provider=connect_provider)
    ws,_=await connected(app,key)
    await ws.send({'type':'text.delta','delta':'Offline literal fixture.'})
    await ws.send({'type':'text.done'})
    assert (await ws.next())['type']=='audio.delta'
    await peer['closing'].wait()
    return app,engine,ws,key


@pytest.mark.parametrize('delay',[0,.5,1.25,None])
async def test_actual_native_delayed_or_absent_ack_completes_after_local_close(tmp_path,peer,delay):
    peer['delay']=delay
    app,engine,ws,_=await begin(tmp_path,peer)
    started=time.monotonic()
    assert await ws.next()=={'type':'audio.done','heard':False}
    await ws.close()
    assert time.monotonic()-started<speech.PROVIDER_CLOSE_SECONDS
    assert_closed(peer)
    observation=app.state.discovery.observation(engine.id)
    assert observation.inflight==0 and observation.circuit_until==0
    assert observation.last_success>0
    await app.state.proxy.http.aclose()


@pytest.mark.parametrize('control',['text.clear','disconnect'])
async def test_actual_client_interrupt_during_native_close_wins(tmp_path,peer,control):
    peer['delay']=.5
    app,engine,ws,_=await begin(tmp_path,peer)
    assert app.state.discovery.observation(engine.id).inflight==1
    if control=='disconnect':
        await ws.close()
    else:
        await ws.send({'type':control})
        terminal=await ws.next()
        assert terminal=={'type':'audio.clear','provider_acknowledged':False,'transport':'closed'}
        await ws.close()
    assert_closed(peer)
    observation=app.state.discovery.observation(engine.id)
    assert observation.inflight==0 and observation.circuit_until==0
    assert observation.last_success is None
    await app.state.proxy.http.aclose()


async def test_repeated_asgi_cancel_keeps_admission_until_positive_close_once(tmp_path,peer,monkeypatch):
    peer['delay']=.5
    releases=[];original=speech.InflightRequest.release
    async def release(slot):
        assert_closed(peer)
        releases.append(slot)
        await original(slot)
    monkeypatch.setattr(speech.InflightRequest,'release',release)
    app,engine,ws,_=await begin(tmp_path,peer)
    ws.task.cancel();await asyncio.sleep(.03);ws.task.cancel()
    assert app.state.discovery.observation(engine.id).inflight==1
    with pytest.raises(asyncio.CancelledError):await ws.task
    assert_closed(peer)
    assert len(releases)==1 and app.state.discovery.observation(engine.id).inflight==0
    await asyncio.sleep(0)
    active=[t for t in asyncio.all_tasks() if any(n in t.get_coro().__qualname__ for n in ['ProviderOwner.','Delivery.'])]
    assert not active
    await app.state.proxy.http.aclose()


async def test_actual_stalled_exit_forces_abort_and_proves_socket_closed(tmp_path,peer,monkeypatch):
    peer['exit_stall']=True
    monkeypatch.setattr(speech,'PROVIDER_GRACE_SECONDS',.08)
    monkeypatch.setattr(speech,'PROVIDER_CLOSE_SECONDS',.5)
    app,engine,ws,_=await begin(tmp_path,peer)
    assert await ws.next()=={'type':'audio.done','heard':False}
    await ws.close();assert_closed(peer)
    observation=app.state.discovery.observation(engine.id)
    assert observation.inflight==0 and observation.circuit_until==0
    await app.state.proxy.http.aclose()


@pytest.mark.parametrize('error_kind',['runtime','timeout'])
async def test_actual_exit_error_is_closed_but_never_reported_completed(tmp_path,peer,error_kind):
    peer['exit_error']=error_kind
    app,engine,ws,_=await begin(tmp_path,peer)
    assert await ws.next()=={'type':'error','code':'provider_close_failed','partial':True,'heard':False}
    await ws.close();assert_closed(peer)
    observation=app.state.discovery.observation(engine.id)
    assert observation.inflight==0 and observation.last_success is None
    assert observation.circuit_until>time.time()
    await app.state.proxy.http.aclose()


async def test_unknown_close_retains_capacity_and_refuses_second_attempt(tmp_path,monkeypatch):
    class Pending(Provider):
        async def __aenter__(self):
            value=await super().__aenter__()
            class Transport:
                def abort(self):pass
            self.transport=Transport()
            return value
        async def __aexit__(self,*args):
            self.closed+=1  # A label alone is deliberately NOT closure proof.
    monkeypatch.setattr(speech,'PROVIDER_CLOSE_SECONDS',.1)
    monkeypatch.setattr(speech,'PROVIDER_GRACE_SECONDS',.05)
    provider=Pending()
    app,engine,_,key,_,_=await make_router(tmp_path,provider)
    ws,_=await connected(app,key)
    await ws.send({'type':'text.delta','delta':'Offline fixture.'})
    await ws.send({'type':'text.done'})
    assert (await ws.next())['type']=='audio.delta'
    assert await ws.next()=={'type':'error','code':'provider_close_unknown','partial':True,'heard':False}
    await ws.close()
    assert app.state.discovery.observation(engine.id).inflight==1
    second=Socket(app,key)
    assert (await second.start())['type']=='websocket.close'
    await second.close()
    assert len(provider.calls)==provider.closed==1
    assert not [t for t in asyncio.all_tasks() if 'ProviderOwner.' in t.get_coro().__qualname__]
    provider.connection_lost_waiter.set_result(None)
    await app.state.proxy.http.aclose()


async def test_actual_native_provider_error_is_not_mistaken_for_completion(tmp_path,peer):
    peer['kind']='error'
    app,engine,ws,_=await begin(tmp_path,peer)
    assert await ws.next()=={'type':'error','code':'provider_error','partial':True,'heard':False}
    await ws.close();assert_closed(peer)
    observation=app.state.discovery.observation(engine.id)
    assert observation.inflight==0 and observation.last_success is None
    assert observation.circuit_until>time.time()
    await app.state.proxy.http.aclose()
