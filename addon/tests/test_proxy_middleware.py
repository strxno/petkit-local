"""End-to-end tests for proxy mode as a middleware over the real route table.

These use `create_app`, so what they exercise is the whole chain — logging,
device resolution, the proxy, the actual handlers — against a fake PetKit cloud
standing up as a second test server. The questions they answer are the ones that
decide whether proxy mode is safe to leave switched on:

* off, does anything change at all?
* on, does the device still end up talking to US?
* on, does a queued command still get delivered?
* on, does the local side effect (an event row) still happen?
"""
import json

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from petkit_local.devices.ble import MIN_BLE_REPLY_BYTES
from petkit_local.devices.registry import DeviceRegistry
from petkit_local.events.store import EventStore
from petkit_local.http.proxy import close_proxy_session
from petkit_local.http.server import create_app
from petkit_local.web.hub import EventHub

HDR = {"X-Device": "id=100&sn=SN100&type=T5"}


def _config(**kw):
    base = {
        "api_url": "http://192.0.2.199:8080/6/",
        "mqtt_port": 1883,
        "bucket_endpoint": "https://192.0.2.199:9000",
        "data_dir": "/tmp",
        "capture": False,
        "capture_dir": "/tmp/capture",
        "proxy_mode": False,
        "proxy_upstream": "",
        "proxy_block_run_cmd": True,
        "proxy_block_ota": True,
        "proxy_media_real_oss": False,
    }
    base.update(kw)
    return base


async def _cloud(handler):
    """A fake PetKit cloud; returns (client, base_url) with no trailing slash."""
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client, str(client.make_url("")).rstrip("/")


async def _device_app(config, *, registry=None, store=None, hub=None):
    registry = registry if registry is not None else DeviceRegistry()
    app = create_app(registry, config)
    app["event_hub"] = hub if hub is not None else EventHub()
    if store is not None:
        app["event_store"] = store
    client = TestClient(TestServer(app))
    await client.start_server()
    return client, registry


async def _register(client):
    """Sign a device up so requests resolve to a registered device."""
    r = await client.post("/6/t5/dev_signup", headers=HDR)
    assert r.status == 200
    return (await r.json())["result"]


# --- off: nothing changes ---------------------------------------------------

async def test_proxy_off_never_dials_upstream():
    """The mode has to be free when it is off — it wraps every device request."""
    hits = []

    async def cloud(request):
        hits.append(request.path)
        return web.json_response({"result": {"from": "cloud"}})

    up, base = await _cloud(cloud)
    client, _ = await _device_app(_config(proxy_upstream=base))
    try:
        await _register(client)
        r = await client.post("/6/t5/dev_serverinfo", headers=HDR)
        body = await r.json()

        assert body["result"]["apiServers"] == ["http://192.0.2.199:8080/6/"]
        assert hits == []
    finally:
        await close_proxy_session(client.app)
        await client.close()
        await up.close()


# --- on: the device gets the cloud's answer, minus what we take out ---------

async def test_proxy_on_delivers_the_cloud_reply():
    async def cloud(request):
        return web.json_response({"result": {"from": "cloud", "path": request.path}})

    up, base = await _cloud(cloud)
    client, _ = await _device_app(_config(proxy_upstream=base))
    try:
        await _register(client)
        client.app["config"]["proxy_mode"] = True

        r = await client.post("/6/t5/dev_device_info", headers=HDR)
        body = await r.json()
        assert body["result"]["from"] == "cloud"
        # And the path was not doubled up (see normalize_upstream).
        assert body["result"]["path"] == "/6/t5/dev_device_info"
    finally:
        await close_proxy_session(client.app)
        await client.close()
        await up.close()


async def test_the_device_is_never_handed_back_to_petkit():
    """The load-bearing one. If this regresses, a device silently migrates to
    the real cloud at its next poll and never comes back."""
    async def cloud(request):
        return web.json_response({"result": {
            "apiServers": ["https://api-eu.petkt.com/6/"],
            "ipServers": ["1.2.3.4"], "dns": "223.5.5.5",
            "linked": 1, "nextTick": 300,
        }})

    up, base = await _cloud(cloud)
    client, _ = await _device_app(_config(proxy_upstream=base))
    try:
        await _register(client)
        client.app["config"]["proxy_mode"] = True

        r = await client.post("/6/t5/dev_serverinfo", headers=HDR)
        result = (await r.json())["result"]

        assert result["apiServers"] == ["http://192.0.2.199:8080/6/"]
        assert result["ipServers"] == []
        assert result["dns"] == ""
        # Forced wholesale, so even `nextTick` is ours — this is the one
        # endpoint where a guaranteed-correct answer beats observing upstream's.
        # Upstream's own body is still captured verbatim.
        assert result["nextTick"] == 3600
    finally:
        await close_proxy_session(client.app)
        await client.close()
        await up.close()


async def test_mqtt_credentials_are_never_handed_back_either():
    async def cloud(request):
        return web.json_response({"result": {"ali": {
            "deviceName": "realdn", "deviceSecret": "realsecret", "productKey": "realpk",
            "mqttHost": "realpk.iot-as-mqtt.cn-shanghai.aliyuncs.com",
        }}})

    up, base = await _cloud(cloud)
    client, registry = await _device_app(_config(proxy_upstream=base))
    try:
        await _register(client)
        client.app["config"]["proxy_mode"] = True
        device = registry.get(100)

        r = await client.post("/6/t5/dev_only_iot_device_info", headers=HDR)
        ali = (await r.json())["result"]["ali"]

        assert ali["mqttHost"] == "192.0.2.199"
        assert ali["deviceSecret"] == device.mqtt_device_secret
        assert ali["productKey"] == device.mqtt_product_key
    finally:
        await close_proxy_session(client.app)
        await client.close()
        await up.close()


async def test_media_stays_local_unless_the_toggle_says_otherwise():
    async def cloud(request):
        return web.json_response({"result": {"type": "oci", "capability": [
            {"cycleType": "fullVideo", "primaryParUrl": "https://petkit-oss.aliyuncs.com/",
             "primaryAesKeyStr": "REALKEYREALKEY01"},
        ]}})

    up, base = await _cloud(cloud)
    client, _ = await _device_app(_config(proxy_upstream=base))
    try:
        await _register(client)
        client.app["config"]["proxy_mode"] = True

        r = await client.post("/6/t5/dev_oss_sts_info_new_v2", headers=HDR)
        entries = (await r.json())["result"]["capability"]
        assert all("192.0.2.199:9000" in e["primaryParUrl"] for e in entries)

        # Flipped on, the device is sent to PetKit's OSS untouched.
        client.app["config"]["proxy_media_real_oss"] = True
        r = await client.post("/6/t5/dev_oss_sts_info_new_v2", headers=HDR)
        entries = (await r.json())["result"]["capability"]
        assert entries[0]["primaryParUrl"] == "https://petkit-oss.aliyuncs.com/"
    finally:
        await close_proxy_session(client.app)
        await client.close()
        await up.close()


# --- on: failure never reaches the device -----------------------------------

async def test_unreachable_upstream_falls_back_to_the_local_answer():
    """Never a 502 to a device — firmware reads one as a server fault and
    retries forever."""
    client, _ = await _device_app(_config(proxy_upstream="http://127.0.0.1:1"))
    try:
        await _register(client)
        client.app["config"]["proxy_mode"] = True

        r = await client.post("/6/t5/dev_serverinfo", headers=HDR)
        assert r.status == 200
        assert (await r.json())["result"]["apiServers"] == ["http://192.0.2.199:8080/6/"]
    finally:
        await close_proxy_session(client.app)
        await client.close()


async def test_an_unidentified_request_is_not_forwarded():
    """Redaction substitutes OUR credentials; with no device there is nothing to
    substitute, so forwarding raw is exactly what must not happen."""
    hits = []

    async def cloud(request):
        hits.append(request.path)
        return web.json_response({"result": {"apiServers": ["https://api-eu.petkt.com/6/"]}})

    up, base = await _cloud(cloud)
    client, _ = await _device_app(_config(proxy_upstream=base, proxy_mode=True))
    try:
        r = await client.post("/6/t5/dev_serverinfo")  # no X-Device header
        assert (await r.json())["result"]["apiServers"] == ["http://192.0.2.199:8080/6/"]
        assert hits == []
    finally:
        await close_proxy_session(client.app)
        await client.close()
        await up.close()


async def test_non_api_paths_are_never_forwarded():
    hits = []

    async def cloud(request):
        hits.append(request.path)
        return web.json_response({"result": {}})

    up, base = await _cloud(cloud)
    client, _ = await _device_app(_config(proxy_upstream=base, proxy_mode=True))
    try:
        r = await client.get("/patcher/download/nope.bin")
        assert r.status == 404
        assert hits == []
    finally:
        await close_proxy_session(client.app)
        await client.close()
        await up.close()


# --- on: the heartbeat merge ------------------------------------------------

async def _heartbeat_client(cloud_result, *, queued=None):
    async def cloud(request):
        return web.json_response(cloud_result)

    up, base = await _cloud(cloud)
    client, registry = await _device_app(_config(proxy_upstream=base))
    await _register(client)
    client.app["config"]["proxy_mode"] = True
    if queued:
        for cmd in queued:
            registry.get(100).command_queue.append(cmd)
    return up, client, registry


async def test_a_heartbeat_carrying_a_command_is_not_forwarded_at_all():
    """`pop_commands` has already run by the time forwarding could start, and
    there is no way to put a command back — so a reply that is delivering one
    goes straight out with no await in between. Anything else leaves a window
    where a cancelled request, a slow upstream or an exception loses it for good
    while `wait_for_heartbeat` reports success."""
    hits = []

    async def cloud(request):
        hits.append(request.path)
        return web.json_response({"result": [{"time": 9, "content": "{}"}]})

    up, base = await _cloud(cloud)
    client, registry = await _device_app(_config(proxy_upstream=base))
    try:
        await _register(client)
        client.app["config"]["proxy_mode"] = True
        registry.get(100).command_queue.append(
            json.dumps({"msgType": 0, "user_cmd": {"reboot": 1}}))

        r = await client.get("/6/poll/t5/heartbeat", headers=HDR)
        entries = (await r.json())["result"]

        assert len(entries) == 1 and "reboot" in entries[0]["content"]
        assert hits == []
        # Delivery is at-most-once, so the queue must be spent.
        assert registry.get(100).command_queue == []
    finally:
        await close_proxy_session(client.app)
        await client.close()
        await up.close()


async def test_an_idle_heartbeat_still_carries_the_clouds_commands():
    """The cost of the rule above is nil: heartbeats are ~15s apart and almost
    always idle, so the cloud's own commands still get through."""
    up, client, _ = await _heartbeat_client(
        {"result": [{"time": 9, "content": json.dumps({"user_cmd": {"set_state": 1}})}]})
    try:
        r = await client.get("/6/poll/t5/heartbeat", headers=HDR)
        entries = (await r.json())["result"]
        assert len(entries) == 1 and "set_state" in entries[0]["content"]
    finally:
        await close_proxy_session(client.app)
        await client.close()
        await up.close()


async def test_heartbeat_command_survives_an_unreachable_upstream():
    """The highest-severity failure mode in the whole change: `pop_commands` has
    already run by the time forwarding is attempted."""
    client, registry = await _device_app(_config(proxy_upstream="http://127.0.0.1:1"))
    try:
        await _register(client)
        client.app["config"]["proxy_mode"] = True
        registry.get(100).command_queue.append(json.dumps({"user_cmd": {"reboot": 1}}))

        r = await client.get("/6/poll/t5/heartbeat", headers=HDR)
        entries = (await r.json())["result"]
        assert len(entries) == 1 and "reboot" in entries[0]["content"]
    finally:
        await close_proxy_session(client.app)
        await client.close()


async def test_heartbeat_command_survives_a_garbage_upstream_reply():
    async def cloud(request):
        return web.Response(body=b"<html>503 from a load balancer</html>")

    up, base = await _cloud(cloud)
    client, registry = await _device_app(_config(proxy_upstream=base))
    try:
        await _register(client)
        client.app["config"]["proxy_mode"] = True
        registry.get(100).command_queue.append(json.dumps({"user_cmd": {"reboot": 1}}))

        r = await client.get("/6/poll/t5/heartbeat", headers=HDR)
        entries = (await r.json())["result"]
        assert len(entries) == 1 and "reboot" in entries[0]["content"]
    finally:
        await close_proxy_session(client.app)
        await client.close()
        await up.close()


async def test_heartbeat_command_survives_an_upstream_error_status():
    async def cloud(request):
        return web.json_response({"result": []}, status=500)

    up, base = await _cloud(cloud)
    client, registry = await _device_app(_config(proxy_upstream=base))
    try:
        await _register(client)
        client.app["config"]["proxy_mode"] = True
        registry.get(100).command_queue.append(json.dumps({"user_cmd": {"reboot": 1}}))

        r = await client.get("/6/poll/t5/heartbeat", headers=HDR)
        assert r.status == 200
        entries = (await r.json())["result"]
        assert len(entries) == 1 and "reboot" in entries[0]["content"]
    finally:
        await close_proxy_session(client.app)
        await client.close()
        await up.close()


async def test_idle_heartbeat_collapses_to_one_marker():
    """Both sides emit `{"time": ms}` when they have nothing; a device has never
    been sent two of them."""
    up, client, _ = await _heartbeat_client({"result": [{"time": 9}]})
    try:
        r = await client.get("/6/poll/t5/heartbeat", headers=HDR)
        entries = (await r.json())["result"]
        assert len(entries) == 1
        assert set(entries[0]) <= {"time", "timestamp"}
    finally:
        await close_proxy_session(client.app)
        await client.close()
        await up.close()


async def test_a_cloud_run_cmd_never_reaches_the_device():
    up, client, _ = await _heartbeat_client(
        {"result": [{"time": 9, "content": json.dumps({"user_cmd": {"run_cmd": "rm -rf /"}})}]})
    try:
        r = await client.get("/6/poll/t5/heartbeat", headers=HDR)
        assert "run_cmd" not in await r.text()
    finally:
        await close_proxy_session(client.app)
        await client.close()
        await up.close()


# --- on: the local side effects still happen --------------------------------

async def test_local_handler_side_effects_survive_being_overridden(event_store: EventStore):
    """The device gets the cloud's reply, but our event row is still written —
    that is what keeps the timeline and HA entities alive during a session."""
    async def cloud(request):
        return web.json_response({"result": "cloud says hi"})

    up, base = await _cloud(cloud)
    client, _ = await _device_app(_config(proxy_upstream=base), store=event_store)
    try:
        await _register(client)
        client.app["config"]["proxy_mode"] = True

        r = await client.post("/6/t5/dev_event_report", headers=HDR,
                              data="eventType=10&eventId=abc123&timestamp=1700000000")
        assert (await r.json())["result"] == "cloud says hi"

        rows = await event_store.all_events()
        assert len(rows) == 1
        assert rows[0]["device_id"] == 100
    finally:
        await close_proxy_session(client.app)
        await client.close()
        await up.close()


# --- on: what gets recorded -------------------------------------------------

async def test_a_blocked_attempt_is_persisted_but_a_rewrite_is_not(event_store: EventStore):
    """The split the table exists for: address rewrites fire on every routine
    poll, so recording each one would bury the handful that matter."""
    async def cloud(request):
        if request.path.endswith("dev_ota_check"):
            return web.json_response({"result": [{"url": "http://petkt.com/fw.bin",
                                                  "md5": "deadbeef"}]})
        return web.json_response({"result": {"apiServers": ["https://api-eu.petkt.com/6/"]}})

    hub = EventHub()
    up, base = await _cloud(cloud)
    client, _ = await _device_app(_config(proxy_upstream=base), store=event_store, hub=hub)
    try:
        await _register(client)
        client.app["config"]["proxy_mode"] = True

        await client.post("/6/t5/dev_serverinfo", headers=HDR)
        assert await event_store.recent_blocked_attempts() == []
        assert hub.redaction_counts().get("server") == 1

        r = await client.post("/6/t5/dev_ota_check", headers=HDR)
        assert (await r.json()) == {"result": {}}

        rows = await event_store.recent_blocked_attempts()
        assert [row["kind"] for row in rows] == ["ota"]
        assert rows[0]["device_id"] == 100
        assert rows[0]["endpoint"] == "/6/t5/dev_ota_check"
        assert rows[0]["transport"] == "http"
    finally:
        await close_proxy_session(client.app)
        await client.close()
        await up.close()


async def test_recording_failures_do_not_cost_the_device_its_answer():
    """Observability runs on the request path; a broken store must be a log
    line, not a failed device call."""
    class _BrokenStore:
        async def add_blocked_attempts(self, rows):
            raise RuntimeError("disk full")

    async def cloud(request):
        return web.json_response({"result": [{"url": "http://petkt.com/fw.bin",
                                              "md5": "deadbeef"}]})

    up, base = await _cloud(cloud)
    client, _ = await _device_app(_config(proxy_upstream=base), store=_BrokenStore())
    try:
        await _register(client)
        client.app["config"]["proxy_mode"] = True

        r = await client.post("/6/t5/dev_ota_check", headers=HDR)
        assert r.status == 200
        assert (await r.json()) == {"result": {}}
    finally:
        await close_proxy_session(client.app)
        await client.close()
        await up.close()


# --- capture streams --------------------------------------------------------

async def _capture_run(tmp_path, *, capture, proxy):
    async def cloud(request):
        return web.json_response({"result": {"apiServers": ["https://api-eu.petkt.com/6/"],
                                             "dns": "223.5.5.5"}})

    up, base = await _cloud(cloud)
    client, _ = await _device_app(_config(proxy_upstream=base, capture=capture,
                                          capture_dir=str(tmp_path)))
    try:
        await _register(client)
        client.app["config"]["proxy_mode"] = proxy
        await client.post("/6/t5/dev_serverinfo", headers=HDR)
    finally:
        await close_proxy_session(client.app)
        await client.close()
        await up.close()
    return sorted(p.name for p in tmp_path.iterdir()) if tmp_path.exists() else []


async def test_proxy_capture_needs_both_switches(tmp_path):
    """A proxied session is a different artifact from ordinary traffic, so it
    gets files of its own — and only when both toggles are on."""
    assert await _capture_run(tmp_path / "a", capture=False, proxy=True) == []
    assert "proxy_http.jsonl" not in await _capture_run(tmp_path / "b",
                                                        capture=True, proxy=False)


async def test_proxy_capture_records_both_bodies(tmp_path):
    """The whole reason to turn it on: `requests.jsonl` records no bodies at
    all, and here we need the cloud's reply AND what the device was given."""
    names = await _capture_run(tmp_path, capture=True, proxy=True)
    assert "proxy_http.jsonl" in names
    assert "proxy_redactions.jsonl" in names

    line = json.loads((tmp_path / "proxy_http.jsonl").read_text().splitlines()[0])
    assert line["path"] == "/6/t5/dev_serverinfo"
    assert line["upstream_status"] == 200
    assert "api-eu.petkt.com" in line["upstream_body"]      # what PetKit sent
    assert "192.0.2.199" in line["sent_body"]             # what the device got
    assert line["redactions"] == ["server"]
    assert "X-Device" in line["headers"]

    red = json.loads((tmp_path / "proxy_redactions.jsonl").read_text().splitlines()[0])
    assert red["rule"] == "server"
    # `dev_serverinfo` is forced wholesale, so the record carries the entire
    # upstream body rather than just the fields that were swapped.
    assert red["original"]["result"]["dns"] == "223.5.5.5"


# --- the device never pays for the cloud's behaviour ------------------------

async def test_a_refusing_upstream_is_recorded_but_not_relayed():
    """A device whose serial the real cloud does not know gets 401 on every
    endpoint. Relaying that breaks the never-404 rule from the far side —
    firmware reads a 4xx as a server fault and retries forever."""
    async def cloud(request):
        return web.json_response({"error": "unknown device"}, status=401)

    up, base = await _cloud(cloud)
    client, _ = await _device_app(_config(proxy_upstream=base))
    try:
        await _register(client)
        client.app["config"]["proxy_mode"] = True

        r = await client.post("/6/t5/dev_serverinfo", headers=HDR)
        assert r.status == 200
        assert (await r.json())["result"]["apiServers"] == ["http://192.0.2.199:8080/6/"]
    finally:
        await close_proxy_session(client.app)
        await client.close()
        await up.close()


async def test_an_upstream_500_on_the_catchall_still_answers_200():
    async def cloud(request):
        return web.Response(status=500, text="upstream on fire")

    up, base = await _cloud(cloud)
    client, _ = await _device_app(_config(proxy_upstream=base))
    try:
        await _register(client)
        client.app["config"]["proxy_mode"] = True

        r = await client.post("/6/t5/dev_something_new", headers=HDR)
        assert r.status == 200
        assert (await r.json()) == {"result": {}}
    finally:
        await close_proxy_session(client.app)
        await client.close()
        await up.close()


async def test_a_body_that_breaks_redaction_still_answers_locally():
    """Redaction runs outside `forward`'s own try/except, so an exception there
    used to become a 500 — and on a heartbeat, a lost command."""
    async def cloud(request):
        # Deep enough to blow the recursion limit in the walker. RecursionError
        # is not one of the exceptions `_decode` catches.
        body = "[" * 3000 + "]" * 3000
        return web.Response(body=('{"result": %s}' % body).encode(),
                            content_type="application/json")

    up, base = await _cloud(cloud)
    client, _ = await _device_app(_config(proxy_upstream=base))
    try:
        await _register(client)
        client.app["config"]["proxy_mode"] = True

        r = await client.post("/6/t5/dev_serverinfo", headers=HDR)
        assert r.status == 200
        assert (await r.json())["result"]["apiServers"] == ["http://192.0.2.199:8080/6/"]
    finally:
        await close_proxy_session(client.app)
        await client.close()
        await up.close()


async def test_routine_polling_does_not_fill_the_attempts_table(event_store: EventStore):
    """`/api/blocked` is empty on a healthy session, or it is useless. Both of
    these fire a redaction rule on every single poll."""
    async def cloud(request):
        if request.path.endswith("dev_ota_heartbeat"):
            return web.json_response({"result": []})       # "no update", as always
        return web.json_response({"result": {"id": 100, "sn": "SN100",
                                             "secret": "0123456789abcdef"}})

    hub = EventHub()
    up, base = await _cloud(cloud)
    client, _ = await _device_app(_config(proxy_upstream=base), store=event_store, hub=hub)
    try:
        await _register(client)
        client.app["config"]["proxy_mode"] = True

        for _ in range(3):
            await client.post("/6/t5/dev_device_info", headers=HDR)
            await client.post("/6/t5/dev_ota_heartbeat", headers=HDR)

        assert await event_store.recent_blocked_attempts() == []
    finally:
        await close_proxy_session(client.app)
        await client.close()
        await up.close()


async def test_a_partial_mqtt_credential_capture_is_discarded():
    """Half an identity has `mqtt/upstream.py` dialling an empty host every 10s
    forever, with nothing but a warning to say why."""
    class _Creds:
        def __init__(self):
            self.stored = {}

        def put(self, petkit_id, creds):
            self.stored[petkit_id] = creds

    async def cloud(request):
        # Host in one object, credentials nowhere — the rule matches on either.
        return web.json_response({"result": {"mqttHost": "realpk.iot-as-mqtt.example"}})

    creds = _Creds()
    up, base = await _cloud(cloud)
    client, _ = await _device_app(_config(proxy_upstream=base))
    client.app["proxy_upstream_creds"] = creds
    try:
        await _register(client)
        client.app["config"]["proxy_mode"] = True

        await client.post("/6/t5/dev_only_iot_device_info", headers=HDR)
        assert creds.stored == {}
    finally:
        await close_proxy_session(client.app)
        await client.close()
        await up.close()


async def test_credentials_split_across_two_objects_are_merged():
    """The rule matches on `mqttHost` OR the trio, so one reply can trip it
    twice — the second match must not blank out what the first found."""
    class _Creds:
        def __init__(self):
            self.stored = {}

        def put(self, petkit_id, creds):
            self.stored[petkit_id] = creds

    async def cloud(request):
        return web.json_response({"result": {
            "ali": {"productKey": "realpk", "deviceName": "realdn",
                    "deviceSecret": "realsecret"},
            "zzz": {"mqttHost": "realpk.iot-as-mqtt.example"},
        }})

    creds = _Creds()
    up, base = await _cloud(cloud)
    client, _ = await _device_app(_config(proxy_upstream=base))
    client.app["proxy_upstream_creds"] = creds
    try:
        await _register(client)
        client.app["config"]["proxy_mode"] = True

        await client.post("/6/t5/dev_only_iot_device_info", headers=HDR)
        assert creds.stored[100]["mqtt_host"] == "realpk.iot-as-mqtt.example"
        assert creds.stored[100]["device_secret"] == "realsecret"
    finally:
        await close_proxy_session(client.app)
        await client.close()
        await up.close()


async def test_a_200_carrying_petkits_refusal_is_served_locally():
    """The failure that showed up on hardware. PetKit answers a taken-over
    device with HTTP 200 + `{"error": {"code": 704}}` on every session-bearing
    endpoint; passing that on put the device into a ~2.4s boot loop."""
    async def cloud(request):
        return web.json_response({"error": {"code": 704, "msg": "refused"}})

    up, base = await _cloud(cloud)
    client, _ = await _device_app(_config(proxy_upstream=base))
    try:
        await _register(client)
        client.app["config"]["proxy_mode"] = True

        # The takeover-critical one.
        r = await client.post("/6/t5/dev_serverinfo", headers=HDR)
        assert (await r.json())["result"]["apiServers"] == ["http://192.0.2.199:8080/6/"]

        # And every other endpoint gets our working answer, not the error.
        r = await client.post("/6/t5/dev_device_info", headers=HDR)
        body = await r.json()
        assert "error" not in body
        assert body["result"]["id"] == 100

        r = await client.get("/6/poll/t5/heartbeat", headers=HDR)
        assert "error" not in await r.json()
    finally:
        await close_proxy_session(client.app)
        await client.close()
        await up.close()


async def test_the_refusal_is_still_recorded_for_the_operator(tmp_path):
    """"The cloud is refusing everything" is the single most useful thing to
    know after turning proxy on, so it must stay observable even though the
    device never sees it."""
    async def cloud(request):
        return web.json_response({"error": {"code": 704, "msg": "refused"}})

    up, base = await _cloud(cloud)
    client, _ = await _device_app(_config(proxy_upstream=base, capture=True,
                                          capture_dir=str(tmp_path)))
    try:
        await _register(client)
        client.app["config"]["proxy_mode"] = True
        await client.post("/6/t5/dev_device_info", headers=HDR)

        rec = json.loads((tmp_path / "proxy_http.jsonl").read_text().splitlines()[-1])
        assert rec["upstream_status"] == 200
        assert "704" in rec["upstream_body"]
    finally:
        await close_proxy_session(client.app)
        await client.close()
        await up.close()


# --- proxy mode has to be VISIBLE ------------------------------------------
#
# Once a taken-over device settles down it polls only the heartbeat, PetKit
# refuses that, and we answer locally — so without this, nothing anywhere
# differs from proxy being off and the mode looks broken.

async def test_a_proxied_call_is_visible_in_the_request_it_belongs_to():
    async def cloud(request):
        return web.json_response({"error": {"code": 704, "msg": "refused"}})

    hub = EventHub()
    up, base = await _cloud(cloud)
    client, _ = await _device_app(_config(proxy_upstream=base), hub=hub)
    try:
        await _register(client)
        client.app["config"]["proxy_mode"] = True
        await client.get("/6/poll/t5/heartbeat", headers=HDR)

        entry = [e for e in hub.recent() if e["kind"] == "http"][-1]
        proxy = entry["detail"]["proxy"]
        assert proxy["status"] == 200
        assert proxy["error"]["code"] == 704
        assert proxy["served"] == "local"
        assert base in proxy["upstream"]
        assert "704" in proxy["upstream_body"]
    finally:
        await close_proxy_session(client.app)
        await client.close()
        await up.close()


async def test_upstream_outcomes_are_counted():
    """"Forwarded 312 calls, 312 refused (704)" is the answer to "is this
    doing anything?" — nothing else answers it in steady state."""
    async def cloud(request):
        if request.path.endswith("dev_device_info"):
            return web.json_response({"result": {"id": 100}})
        return web.json_response({"error": {"code": 704, "msg": "refused"}})

    hub = EventHub()
    up, base = await _cloud(cloud)
    client, _ = await _device_app(_config(proxy_upstream=base), hub=hub)
    try:
        await _register(client)
        client.app["config"]["proxy_mode"] = True
        await client.get("/6/poll/t5/heartbeat", headers=HDR)
        await client.get("/6/poll/t5/heartbeat", headers=HDR)
        await client.post("/6/t5/dev_device_info", headers=HDR)

        assert hub.upstream_counts() == {"error_704": 2, "ok": 1}
    finally:
        await close_proxy_session(client.app)
        await client.close()
        await up.close()


async def test_nothing_is_noted_when_proxy_is_off():
    hub = EventHub()
    client, _ = await _device_app(_config(), hub=hub)
    try:
        await _register(client)
        await client.get("/6/poll/t5/heartbeat", headers=HDR)
        entry = [e for e in hub.recent() if e["kind"] == "http"][-1]
        assert "proxy" not in entry["detail"]
        assert hub.upstream_counts() == {}
    finally:
        await client.close()


async def test_the_real_api_secret_is_adopted_and_then_served_locally():
    """End to end for the 704 fix: the device must keep signing with PetKit's
    secret even on requests we answer ourselves, or the next boot reverts it."""
    async def cloud(request):
        return web.json_response({"result": {
            "id": 100, "sn": "SN100", "signupAt": "1700000000",
            "createdAt": 1700000000, "secret": "0123456789abcdef"}})

    up, base = await _cloud(cloud)
    client, registry = await _device_app(_config(proxy_upstream=base))
    try:
        await _register(client)
        client.app["config"]["proxy_mode"] = True

        r = await client.post("/6/t5/dev_signup", headers=HDR)
        assert (await r.json())["result"]["secret"] == "0123456789abcdef"
        assert registry.get(100).api_secret == "0123456789abcdef"

        # Proxy off: our own answer now carries the adopted secret too.
        client.app["config"]["proxy_mode"] = False
        r = await client.post("/6/t5/dev_signup", headers=HDR)
        assert (await r.json())["result"]["secret"] == "0123456789abcdef"

        # The broker credential is separate and untouched.
        assert registry.get(100).mqtt_device_secret != "0123456789abcdef"
    finally:
        await close_proxy_session(client.app)
        await client.close()
        await up.close()


async def test_the_ble_list_is_always_ours():
    """We answer locally (LOCAL_ONLY) so a taken-over device is not told to
    drop accessories paired here, even if the cloud has nothing to report."""
    hits = []

    async def cloud(request):
        hits.append(request.path)
        return web.json_response({"result": {"list": [], "nextTick": 3600}})

    hub = EventHub()
    up, base = await _cloud(cloud)
    client, _ = await _device_app(_config(proxy_upstream=base), hub=hub)
    try:
        await _register(client)
        client.app["config"]["proxy_mode"] = True

        r = await client.get("/6/t5/dev_ble_device", headers=HDR)
        raw = await r.read()
        body = json.loads(raw)

        assert body["result"] == {}
        # Still observed — proxy mode's whole purpose.
        assert hits == ["/6/t5/dev_ble_device"]
        entry = [e for e in hub.recent() if e["kind"] == "http"][-1]
        assert '"list": []' in entry["detail"]["proxy"]["upstream_body"]
    finally:
        await close_proxy_session(client.app)
        await client.close()
        await up.close()


async def test_media_file_info_is_never_forwarded():
    """`dev_upload_file_info_v2` names objects in OUR bucket with full URLs.
    Forwarding it hands PetKit the LAN layout STS redaction exists to hide;
    upstream also has nothing useful to say (error 1). Unlike ble_device, do
    not dial at all."""
    hits = []

    async def cloud(request):
        hits.append(request.path)
        body = await request.read()
        hits.append(body.decode())
        return web.json_response({"error": {"code": 1, "msg": "系统繁忙"}})

    up, base = await _cloud(cloud)
    client, _ = await _device_app(_config(proxy_upstream=base))
    try:
        await _register(client)
        client.app["config"]["proxy_mode"] = True

        infos = [{"fileId": "f1",
                  "fileUrl": "https://192.0.2.199:9000/t5/100/eventImage/f1",
                  "cycleType": "eventImage", "eventId": "r1"}]
        from urllib.parse import quote
        body = "fileInfos=" + quote(json.dumps(infos))

        r = await client.post("/6/t5/dev_upload_file_info_v2", headers=HDR, data=body)
        assert r.status == 200
        assert (await r.json())["result"] == "success"
        assert hits == [], "must not leak our bucket URLs upstream"
    finally:
        await close_proxy_session(client.app)
        await client.close()
        await up.close()
