"""Request-scoped identity, proxying and observability for the device-facing server.

Three middlewares, in the order aiohttp applies them (outermost first):

* `logging_middleware` — the one place every device request is logged, mirrored
  to the panel's live log and (optionally) written to a capture file, and the
  one place a device's `online`/`last_seen` is refreshed. Liveness lives here
  rather than in the handlers because *any* HTTP contact proves the device is
  up, and not every device model does MQTT or even an HTTP heartbeat.
* `device_middleware` — parses the firmware's `X-Device` header and the URL
  into ``request["x_device"]``, ``request["api_version"]`` and
  ``request["device_type"]``, so no handler has to re-derive them.
* `proxy_middleware` — when proxy mode is on, forwards the request to the real
  PetKit cloud and answers with its (redacted) reply instead of ours.

That order is load-bearing in both directions. `device_middleware` runs before
the proxy so `request["device_type"]` is set when the upstream is being chosen,
and `logging_middleware` stays outermost so the panel's live log and the capture
file record **what the device actually received**, not what our handler would
have said.

Nothing here rejects a request: identity is best-effort and every key it sets is
optional, because a device that cannot be identified must still get a valid
answer (see `http/server.py`'s "never 404 a device" rule). The proxy holds the
same line — every failure path falls back to the local answer.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Awaitable, Callable

from aiohttp import web

from petkit_local.http.handlers._common import device_id, request_device

log = logging.getLogger(__name__)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

#: Only the PetKit API prefix is device protocol traffic. The bucket, the face
#: photos and the patcher downloads share this app and must never be forwarded
#: upstream, logged as device traffic, or counted as liveness.
API_PREFIX = "/6/"

#: Endpoints whose answer describes OUR state, not the cloud's. They are still
#: forwarded — the upstream reply is recorded and captured, which is the point of
#: proxy mode — but the device is always served ours.
#:
#: `dev_ble_device` lists the accessories WE have paired (`devices/ble.py`); the
#: cloud's list is PetKit's, which for a taken-over device is empty. Serving the
#: cloud's answer is therefore always wrong — it would tell the device to forget
#: every accessory paired here — which is reason enough for this entry.
#:
#: There is a second, weaker reason recorded here honestly because it used to be
#: stated as fact. The firmware's own log shows:
#:
#:     res data :{"result": {"list": [], "nextTick": 3600}}
#:     [ble_relay_network.c]:[95][pk_schmg_parse_ble_dev_list]relay list prase, update:0
#:     E/ctrl [ble_relay_network.c]:[108][pk_schmg_parse_ble_dev_list]ERR:...parse item NULL
#:
#: and that was read as an empty `list` walking into a null dereference that
#: aborts the boot chain. Captures since then argue against it: PetKit's own
#: cloud answers `{"result":{"list":[],"nextTick":3600}}` to a device with no
#: accessories — 234 times in one session — so every unaccessorised PetKit
#: device in the world receives that payload routinely. The ERR line is real,
#: but a logged parse error is not the same as an aborted boot. We match the
#: cloud shape (empty `list`, not an omitted key): a bare `{"result":{}}` is
#: under the firmware's minimum length and logs `ble list len too short`.
#:
#: This set is for answers that would BREAK the device, not for answers that
#: are merely inconvenient to us. `dev_discern_pic` is deliberately absent: the
#: cloud's pets and face photos reaching the device is what proxy mode MEANS.
#: Turn proxy off to have our own pets take effect.
LOCAL_ONLY_ENDPOINTS = frozenset({"dev_ble_device"})

#: Endpoints whose REQUEST is the leak, withheld while the log-upload guard is
#: on and forwarded normally when it is off.
#:
#: `dev_upload_file_info_v2` is how the device says what it just uploaded:
#: `fileId`, `moduleType`, the AES IV, the `eventId` and the
#: pet/clean/toilet flags. Forwarded, that is a running account of what happened
#: in somebody's home — every visit, every recording, timestamped — sent to
#: PetKit by a device its owner has taken off PetKit. The media itself never
#: reaches them — it is PUT to our bucket — which makes this metadata the whole
#: of what they would learn, and it is enough.
#:
#: Redaction cannot help: it rewrites response bodies, and by the time there is
#: a body to rewrite the request has been delivered. So this is a request-side
#: gate, exactly like `_reports_a_local_log_upload` below and for the same
#: reason. It is NOT in `LOCAL_ONLY_ENDPOINTS`, because that would put it out of
#: proxy mode's reach permanently; switching the guard off proxies it again.
GUARDED_LOCAL_ENDPOINTS = frozenset({"dev_upload_file_info_v2"})


def _reports_a_local_log_upload(request: web.Request, config: dict) -> bool:
    """Whether this is a `dev_upload_log` naming an object in OUR bucket.

    Keyed on where the object actually is, not on the endpoint: a device still
    talking to PetKit's OSS reports a petkit.com URL here, and that exchange is
    ordinary proxied traffic worth forwarding and recording.
    """
    if request.path.rstrip("/").rsplit("/", 1)[-1] != "dev_upload_log":
        return False
    bucket = (config.get("bucket_endpoint") or "").rstrip("/")
    key = request.query.get("key", "")
    return bool(bucket) and key.startswith(bucket)


def parse_x_device(header: str) -> dict | None:
    """Parse the firmware's `X-Device` header into a flat dict of fields.

    The header is query-string shaped — ``id=10000001&type=T5&sn=...&...`` — so
    it is decoded with `parse_qs`. If that yields no ``id``, the header is
    re-split on raw ``&``/``=`` (values left percent-encoded) and that result is
    used instead, but only when it does produce one.

    Returns:
        The header's fields keyed by name, or None when no ``id`` field could be
        recovered at all — such a header identifies nothing, and callers treat
        it exactly like an absent header rather than trusting the other fields.
    """
    if not header:
        return None
    from urllib.parse import parse_qs
    parsed = parse_qs(header, keep_blank_values=True)
    result = {k: v[0] for k, v in parsed.items() if v}
    if "id" not in result:
        parts = dict(p.split("=", 1) for p in header.split("&") if "=" in p)
        if "id" in parts:
            return parts
        return None
    return result


async def parse_form_body(request: web.Request) -> dict[str, str]:
    """The urlencoded POST body as a flat dict, or {} for anything else.

    A third place a device may put its identity, and for some of them the only
    one. An ESP32 feeder signs up with no `X-Device` header and no query string
    at all -- everything is in the body::

        POST /6/d4/dev_signup   Content-Type: application/x-www-form-urlencoded
        hardware=1&firmware=1.267&mac=...&id=400090690&sn=20241223G11497&...

    Read here rather than in the handlers so `_common.py`'s accessors stay
    synchronous and every endpoint gets it at once, exactly as `X-Device` is.

    Costs nothing: `logging_middleware` already reads the body of every POST
    under `/6/`, and aiohttp caches it, so this is the same bytes a second time.
    It cannot starve `proxy_middleware` either -- that copies the body into a
    local before calling the handler.

    Never raises. A body that is not urlencoded, not decodable, or simply
    absent yields {}, which reads the same as a device that sent nothing.
    """
    if request.method not in ("POST", "PUT", "PATCH"):
        return {}
    ctype = request.headers.get("Content-Type", "")
    if "application/x-www-form-urlencoded" not in ctype.lower():
        return {}
    try:
        raw = await request.read()
    except Exception:  # noqa: BLE001 - a body we cannot read is a body we ignore
        return {}
    if not raw:
        return {}
    from urllib.parse import parse_qs
    try:
        parsed = parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
    except Exception:  # noqa: BLE001 - device input never raises
        return {}
    return {k: v[0] for k, v in parsed.items() if v}


@web.middleware
async def device_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Attach the requester's identity to the request, then hand it on.

    Sets, when derivable: ``request["x_device"]`` (the parsed `X-Device` header,
    only if it carries an id), ``request["form"]`` (the urlencoded POST body),
    ``request["api_version"]`` and ``request["device_type"]``. All are optional
    — handlers use `handlers/_common.py` to resolve a device and answer sensibly
    when none of this is present.
    """
    info = None
    x_device = request.headers.get("X-Device", "")
    if x_device:
        info = parse_x_device(x_device)
        if info and "id" in info:
            request["x_device"] = info

    request["form"] = await parse_form_body(request)

    path = request.path
    poll_m = re.match(r"^/(\d+)/poll/(\w+)/", path)
    m = re.match(r"^/(\d+)/(\w+)/", path)
    if poll_m:
        request["api_version"] = poll_m.group(1)
        request["device_type"] = poll_m.group(2)
    elif m and m.group(2) != "poll":
        request["api_version"] = m.group(1)
        request["device_type"] = m.group(2)

    # Fallback: device type from the X-Device `type` field (paths that omit it,
    # e.g. /6/dev_serverinfo or /6/poll/heartbeat).
    if "device_type" not in request and info and info.get("type"):
        request["device_type"] = info["type"].lower()

    return await handler(request)


def _is_heartbeat(path: str) -> bool:
    """Whether this path is one of the three heartbeat routes.

    Named rather than inlined because the heartbeat is the ONE endpoint whose
    local answer may not be thrown away: `handle_heartbeat` drains the device's
    command queue to build it (`devices/base.py::pop_commands` is destructive
    and at-most-once), so the two replies get merged instead of replaced.
    """
    return path.endswith("/heartbeat")


def _endpoint_selected(request: web.Request, config: dict) -> bool:
    """Whether `proxy_only` says to forward this endpoint.

    Empty (the normal case) forwards everything. A non-empty list is the bisect
    tool: hardware has twice shown that a reply which is entirely valid — just
    not the one we usually send — can put the firmware into a boot loop, and
    narrowing that to a single endpoint is otherwise guesswork on a live device.

    Matched on the last path segment, so `dev_device_info` covers both
    `/6/t5/dev_device_info` and any version-less spelling.
    """
    only = (config.get("proxy_only") or "").strip()
    if not only:
        return True
    wanted = {name.strip() for name in only.split(",") if name.strip()}
    return request.path.rstrip("/").rsplit("/", 1)[-1] in wanted


def _build_policy(request: web.Request, device):
    """The redaction policy for one request, from the live config and device.

    Imported lazily along with the proxy machinery: this runs on every request
    and must cost nothing while proxy mode is off.
    """
    from petkit_local.http.redact import RedactionPolicy
    from petkit_local.media.crypto import resolve_key_string

    config = request.app["config"]
    return RedactionPolicy(
        device=device,
        api_url=config.get("api_url", ""),
        mqtt_host=_self_mqtt_host(config),
        bucket_endpoint=config.get("bucket_endpoint", ""),
        aes_key=resolve_key_string(config),
        block_rce=config.get("proxy_block_run_cmd", True),
        block_ota=config.get("proxy_block_ota", True),
        block_log_upload=config.get("proxy_block_log_upload", True),
        media_to_real_oss=config.get("proxy_media_real_oss", False),
    )


def _self_mqtt_host(config: dict) -> str:
    """Our broker's hostname, derived from `api_url` exactly as the handler does.

    Same derivation as `handlers/iot_device_info.py::_self_mqtt_host`, and for
    the same reason: the device opens a separate MQTT connection, so the value
    has to be an address it can reach on its own rather than our request's Host.
    """
    from urllib.parse import urlparse
    return urlparse(config.get("api_url", "")).hostname or ""


@web.middleware
async def proxy_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Answer with the real cloud's reply, redacted, when proxy mode is on.

    Off, this is a pure pass-through with no measurable cost — the config flag is
    read from the same dict the panel mutates, so the mode flips live.

    On, the local handler still runs FIRST and in full. Its response is usually
    discarded, but its side effects are not: that is what keeps the event store,
    the HA entities and the media pipeline populated while a device is being
    observed against PetKit. Five things then decide what the device receives:

    1. **A heartbeat carrying a queued command is answered immediately, without
       forwarding at all.** Building that answer already drained the queue
       (`devices/base.py::pop_commands` is destructive and at-most-once), so any
       await between the pop and the send is a window in which the command can
       be lost — to a cancelled request, a slow upstream, or an exception. There
       is no way to put it back. The next heartbeat is ~15s away and almost
       always idle, so the observation this costs is nil.
    2. An unidentified request is never forwarded. Redaction substitutes OUR
       credentials and addresses into the reply, and without a registered device
       there is nothing to substitute — forwarding raw would be the one way this
       middleware could hand a device someone else's cloud.
    3. `forward` returning None (unreachable, too slow, breaker open) falls back
       to the local response. A device must never pay for PetKit being down.
    4. An upstream reply the device could not act on falls back to the local
       response too — a non-2xx, OR a 200 carrying PetKit's refusal envelope
       (`{"error": {"code": 704}}`, which is what a taken-over device gets on
       every session-bearing endpoint, since the session it presents is one WE
       issued). Relaying either breaks the never-404 rule from the far side:
       observed on real hardware, a `dev_serverinfo` with no server list puts the
       device into a boot loop every ~2.4s. The status, the error and the body
       are all still recorded — observing the refusal is the point, showing it to
       the device is not.
    5. Anything else is answered with the redacted upstream reply. A heartbeat
       that got this far was idle, so merging is a no-op on our side and simply
       lets the cloud's commands through.

    Everything after the local handler is wrapped: a failure in forwarding,
    redaction or recording answers with the local response rather than letting a
    500 reach a device that is waiting for one.
    """
    config = request.app["config"]
    if not config.get("proxy_mode") or not request.path.startswith(API_PREFIX):
        return await handler(request)

    # Read before the handler so we own the bytes whatever it does with them.
    # aiohttp caches the payload, so the handler's own `request.read()` still
    # works. A future handler using `request.multipart()` — a one-shot stream —
    # would need this revisited.
    try:
        body = await request.read()
    except Exception:
        body = b""

    local = await handler(request)

    if _is_heartbeat(request.path):
        from petkit_local.http.handlers.heartbeat import carries_commands
        if carries_commands(local):
            log.debug("Not forwarding %s: it is delivering a queued command",
                      request.path)
            return local

    if _reports_a_local_log_upload(request, config):
        # `dev_upload_log` reports the object URL as a QUERY parameter, and once
        # the device uploads to us that URL is this add-on's own LAN address and
        # bucket layout. Redaction only sanitises response bodies, so forwarding
        # this would hand PetKit exactly what the log-upload guard exists to
        # withhold — where the device's logs are going now — while the guard was
        # busy scrubbing the reply. Not forwarded at all rather than rewritten:
        # there is nothing upstream can usefully say about an object it cannot
        # see, and a doctored `key` would be a lie rather than a redaction.
        log.debug("Not forwarding %s: it reports an upload to our own bucket",
                  request.path)
        return local

    if (config.get("proxy_block_log_upload", True)
            and request.path.rstrip("/").rsplit("/", 1)[-1] in GUARDED_LOCAL_ENDPOINTS):
        log.debug("Not forwarding %s: the log-upload guard withholds it",
                  request.path)
        return local

    device = request_device(request)
    if device is None:
        return local

    if not _endpoint_selected(request, config):
        return local

    try:
        from petkit_local.http.dns import loops_back
        from petkit_local.http.proxy import forward, resolve_upstream

        upstream = resolve_upstream(config.get("proxy_upstream", ""))
        dns_server = config.get("proxy_dns", "")

        # A LAN that points PetKit's names here points them here for US too, and
        # forwarding into ourselves does not fail — our own handler answers, and
        # the reply is then recorded as the cloud's. Checked before the request
        # rather than detected after, because there is nothing in the answer to
        # detect it by. See `http/dns.py`.
        looped = await loops_back(upstream, _local_socket(request), dns_server)
        if looped:
            log.warning(
                "PROXY: not forwarding %s — %s resolves to %s, which is this add-on. "
                "Your DNS redirects it here. Set an upstream DNS server in Setup to "
                "reach the real server.", request.path, upstream, looped)
            hub = request.app.get("event_hub")
            if hub is not None:
                hub.record_upstream("dns_loop")
            return local

        exchange = await forward(request, body=body, upstream=upstream,
                                 policy=_build_policy(request, device),
                                 dns_server=dns_server)
        if exchange is None:
            return local

        await _record_exchange(request, device, exchange, body=body, local=local)
        _note_outcome(request, exchange)

        if request.path.rstrip("/").rsplit("/", 1)[-1] in LOCAL_ONLY_ENDPOINTS:
            return local

        if not exchange.usable:
            log.info("PROXY: upstream gave nothing usable for %s (status %d%s), "
                     "serving locally", request.path, exchange.status,
                     f", error {exchange.error.get('code')}" if exchange.error else "")
            return local

        return exchange.to_response()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("PROXY: failed on %s, serving the local answer", request.path)
        return local


#: Where `proxy_middleware` leaves what it did, for `logging_middleware` to fold
#: into the entry it was already going to write.
PROXY_OUTCOME = "proxy_outcome"


def _local_socket(request: web.Request) -> tuple[str, int] | None:
    """The `(address, port)` of ours this device's connection arrived on.

    Better than enumerating our own interfaces: in a container most of those are
    not what a device can reach, and host networking, bridged networking and
    Ingress each give a different answer. This one is not a guess.
    """
    transport = request.transport
    sockname = transport.get_extra_info("sockname") if transport is not None else None
    if not sockname or len(sockname) < 2:
        return None
    return (sockname[0], sockname[1])


def _note_outcome(request: web.Request, exchange) -> None:
    """Make one proxied call visible, without adding a log line of its own.

    The panel's Log tab already shows every device request with an expandable
    detail; proxy mode belongs IN that detail rather than beside it. Without
    this, a steady-state proxied session looks exactly like an unproxied one —
    the device polls only the heartbeat, PetKit refuses it, nothing is redacted,
    and so nothing anywhere says the request went to the cloud at all.
    """
    error = exchange.error or {}
    outcome = ("ok" if exchange.usable
               else f"error_{error.get('code')}" if error
               else f"http_{exchange.status}")

    request[PROXY_OUTCOME] = {
        "upstream": exchange.url,
        "status": exchange.status,
        "error": error or None,
        "outcome": outcome,
        "served": "upstream" if exchange.usable else "local",
        "redactions": [r.rule for r in exchange.records],
        "upstream_body": _short(_text(exchange.upstream_body)),
    }

    hub = request.app.get("event_hub")
    if hub is not None:
        hub.record_upstream(outcome)


async def _record_exchange(request: web.Request, device, exchange, *,
                           body: bytes, local: web.StreamResponse) -> None:
    """Report one proxied exchange to the panel, the database and the capture.

    Every failure here is swallowed: this is observability running on the
    device-facing request path, and a full disk or a closed store must cost a
    log line, not the answer the device is waiting for.
    """
    try:
        hub = request.app.get("event_hub")
        store = request.app.get("event_store")

        if hub is not None:
            for record in exchange.records:
                hub.record_redaction(
                    device.petkit_id, record.rule,
                    f"{record.rule} on {request.path}",
                    detail={"rule": record.rule, "path": record.path,
                            "endpoint": request.path, "upstream": exchange.url,
                            "original": record.original, "note": record.note},
                    blocked=record.blocking,
                )

        if store is not None and exchange.blocked:
            await store.add_blocked_attempts([{
                "device_id": device.petkit_id,
                "kind": record.rule,
                "transport": "http",
                "endpoint": request.path,
                "upstream": exchange.url,
                "field_path": record.path,
                "payload_json": record.original,
                "detail_json": {"note": record.note, "status": exchange.status},
            } for record in exchange.blocked])

        _capture_exchange(request, exchange, body=body, local=local)
        _remember_upstream_credentials(request, device, exchange)
    except Exception:
        log.exception("PROXY: could not record the exchange for %s", request.path)


def _capture_exchange(request: web.Request, exchange, *,
                      body: bytes, local: web.StreamResponse) -> None:
    """Append the full exchange to the proxy capture stream, if enabled.

    Deliberately gated on capture AND proxy both being on, and written to files
    of its own: a proxied session is a different kind of artifact from the
    ordinary `requests.jsonl`, and unlike that one it carries full bodies —
    which is the entire reason to turn it on.
    """
    config = request.app["config"]
    if not config.get("capture"):
        return

    from petkit_local.utils.capture import capture_record
    capture_dir = config.get("capture_dir", "/data/capture")

    capture_record(capture_dir, "proxy_http", {
        "method": request.method,
        "path": request.path,
        "query": dict(request.query),
        "headers": {h: request.headers[h] for h in _FORWARDED_HEADERS
                    if h in request.headers},
        "req_body": _text(body),
        "upstream_url": exchange.url,
        "upstream_status": exchange.status,
        "upstream_body": _text(exchange.upstream_body),
        "sent_body": _text(exchange.body),
        "local_status": local.status,
        "local_body": _text(getattr(local, "body", None)),
        "redactions": [r.rule for r in exchange.records],
    })

    for record in exchange.records:
        capture_record(capture_dir, "proxy_redactions", {
            "path": request.path,
            "upstream": exchange.url,
            "rule": record.rule,
            "field_path": record.path,
            "original": record.original,
            "replacement": record.replacement,
            "note": record.note,
        })


def _remember_upstream_credentials(request: web.Request, device, exchange) -> None:
    """Persist the real credentials a proxied reply just revealed.

    Two different ones, learned from two different endpoints:

    * The **API secret** from `dev_signup`, which the device signs its requests
      with. Adopting it is what stops PetKit answering 704 to everything — see
      `Device.signing_secret`. Stored on the device itself, because every local
      `dev_signup` from now on has to hand out the same value or the device
      reverts to a secret the cloud rejects.
    * The **Aliyun MQTT credentials** from `dev_iot_device_info`, for
      `mqtt/upstream.py`. Kept in their own file, not on the device.

    And, when present on the same bodies, the account **timezone / locale**
    (IANA name + offset). Those are what BLE provisioning should send, and what
    `to_signup` echoes locally once learned — see `_match_locale`.
    """
    api_secret = exchange.captured.get("api_secret")
    registry = request.app.get("registry")
    dirty = False
    if api_secret and api_secret != device.api_secret:
        device.api_secret = api_secret
        dirty = True
        log.info("Adopted the real PetKit API secret for device %d — its requests "
                 "will now verify upstream", device.petkit_id)

    time_settings = exchange.captured.get("time_settings") or {}
    if time_settings:
        changed = False
        if "timezone" in time_settings and device.config.get("timezone") != time_settings["timezone"]:
            device.config["timezone"] = time_settings["timezone"]
            changed = True
        if "locale" in time_settings and device.config.get("locale") != time_settings["locale"]:
            device.config["locale"] = time_settings["locale"]
            changed = True
        if changed:
            dirty = True
            log.info("Adopted cloud time settings for device %d: timezone=%s locale=%s",
                     device.petkit_id, device.config.get("timezone"),
                     device.config.get("locale"))

    if dirty and registry is not None:
        registry.mark_dirty()

    creds = exchange.captured.get("mqtt")
    store = request.app.get("proxy_upstream_creds")
    if not creds or store is None:
        return
    # All four or none. A partial capture would have `UpstreamMQTT` dialling an
    # empty host with an empty client id every 10s forever, with nothing but a
    # warning to say why — worse than simply not having the credentials.
    if not all(creds.get(k) for k in ("mqtt_host", "product_key",
                                      "device_name", "device_secret")):
        log.debug("Incomplete upstream MQTT credentials from %s, ignoring", request.path)
        return
    store.put(device.petkit_id, creds)


def _text(raw: bytes | None) -> str | None:
    """Decode a captured body, never raising on a binary one."""
    if raw is None:
        return None
    return bytes(raw).decode("utf-8", "replace")


#: Request headers echoed into the capture record — the ones `http/proxy.py`
#: forwards, so a capture shows exactly what upstream was told.
_FORWARDED_HEADERS = ("X-Device", "X-Session", "F-Session", "User-Agent", "Content-Type")


def _short(text: str | None, limit: int = 4000) -> str | None:
    """Cap a body at `limit` chars for the panel log, marking what was cut.

    The live log holds these in memory and ships them to every open browser, so
    an unbounded state_report or file_info body would be paid for repeatedly.
    None passes through as None to keep "no body" distinct from "empty body".
    """
    if text is None:
        return None
    return text if len(text) <= limit else text[:limit] + f"\n... (+{len(text) - limit} bytes truncated)"


@web.middleware
async def logging_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Log, mirror and capture a device request, and refresh device liveness.

    Only `/6/` paths — the PetKit API prefix — are treated as device traffic;
    the bucket, the face photos and the patcher downloads share this app but are
    not protocol calls and must not mark a device online or flood the panel log.

    For those paths it: emits the one-line access log, records a detailed entry
    (headers, query, capped bodies) on the panel's event hub, appends to the
    capture file when `capture` is configured, and stamps `last_seen` on the
    resolved device — firing the app's ``on_device_seen`` callback if that
    request is what brought it back online.
    """
    # Pre-read the request body for POST/PUT so we can surface it in the panel
    # log. aiohttp caches the payload, so the handler still reads it normally.
    req_body = None
    if request.method in ("POST", "PUT", "PATCH") and request.path.startswith(API_PREFIX):
        try:
            raw = await request.read()
            if raw:
                req_body = raw.decode("utf-8", "replace")
        except Exception:
            req_body = None

    resp = await handler(request)
    if request.path.startswith(API_PREFIX):
        dt = request.get("device_type", "?")
        pid = request.get("x_device", {}).get("id", "?")
        log.info("%s %s [%s id=%s] -> %d", request.method, request.path, dt, pid, resp.status)
        hub = request.app.get("event_hub")
        if hub is not None:
            did = device_id(request)
            resp_body = None
            try:
                if getattr(resp, "body", None) is not None:
                    resp_body = bytes(resp.body).decode("utf-8", "replace")
            except Exception:
                resp_body = None
            detail = {
                "method": request.method,
                "path": request.path,
                "status": resp.status,
                "device_type": dt,
                "headers": dict(request.headers),
                "query": dict(request.query),
                "req_body": _short(req_body),
                "resp_body": _short(resp_body),
            }
            # What proxy mode did with this request, if anything. Folded into
            # the entry rather than published separately — see `_note_outcome`.
            if PROXY_OUTCOME in request:
                detail["proxy"] = request[PROXY_OUTCOME]
            hub.record_http(did, request.method, request.path, resp.status, detail=detail)

        config = request.app.get("config", {})
        if config.get("capture"):
            from petkit_local.utils.capture import capture_record
            capture_record(config.get("capture_dir", "/data/capture"), "requests", {
                "method": request.method,
                "path": request.path,
                "status": resp.status,
                "xdevice": request.headers.get("X-Device", ""),
                "xsession": request.headers.get("X-Session", ""),
                "query": dict(request.query),
            })

        # Any HTTP contact keeps the device online (HTTP is a valid transport,
        # not every device does MQTT / an HTTP heartbeat).
        device = request_device(request)
        if device is not None:
            device.last_seen = time.time()
            if not device.online:
                device.online = True
                cb = request.app.get("on_device_seen")
                if cb is not None:
                    await cb(device)
    return resp


@web.middleware
async def never_fail_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Turn any unhandled handler exception into an empty success.

    `http/server.py`'s rule is that a device is never given a 4xx or 5xx,
    because the firmware reads one as a server fault and retries forever. That
    rule was enforced by every handler individually catching its own failures —
    and several could still escape: a SQLAlchemy error from `upsert_event` on a
    full or read-only disk, an unreadable media key file in `handle_oss_sts`, an
    `MqttError` from the HA publisher during a broker restart, a `RecursionError`
    from deeply nested JSON. Each of those became aiohttp's default 500, i.e.
    precisely the retry loop the rule exists to prevent.

    So this is the backstop, outermost of the four. It answers `{"result": {}}`,
    the same shape `handle_catchall` uses for an endpoint we do not implement —
    a device treats it as "nothing to do" and moves on.

    It deliberately does NOT swallow:

    * `web.HTTPException` — a handler that returns a status on purpose (the
      bucket's 403 refusals, a redirect) keeps it.
    * `asyncio.CancelledError` — shutdown must stay prompt.

    The log line is ERROR with a traceback: this firing is always a bug worth
    fixing, and swallowing it silently would hide exactly the failures the rule
    is protecting the device from.
    """
    try:
        return await handler(request)
    except (web.HTTPException, asyncio.CancelledError):
        raise
    except Exception:
        log.exception("Unhandled error in %s %s - answering empty success so the "
                      "device does not retry forever", request.method, request.path)
        return web.json_response({"result": {}})
