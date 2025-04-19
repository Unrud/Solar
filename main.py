import asyncio
import bluetooth
import json
import math
import network
import os
import requests
import sys
import time
import binascii
from machine import WDT

import aioble

# https://github.com/miguelgrinberg/microdot
from microdot import Microdot, Response, redirect

import config
from locale import get_translation


SERVICE_ID = bluetooth.UUID(0xA002)
NOTIFY_ID = bluetooth.UUID(0xC305)
WRITE_ID = bluetooth.UUID(0xC304)


__wdt = WDT()
__wdt_monitors = []

network.country(config.WIFI_COUNTRY)
network.hostname(config.HOSTNAME)
__nic = network.WLAN(network.STA_IF)

__data = {}
__last_update_ticks_ms = None

__ble_write_char = None  # None when disconnected

__meter_available = bool(
    config.METER_ENDPOINT
    and config.METER_POWER_FIELD
    and config.METER_POWER_DISPLAY_FIELD
)
__auto_power_limit = __meter_available and "auto-power-limit" in os.listdir()

__auto_power_info_data = {}
__auto_power_info_incoming = None
__auto_power_info_total = None
__auto_power_info_remaining = None
__auto_power_info_new_limit = None
__auto_power_info_skip = None
__auto_power_info_active = None


async def watchdog_task():
    good_times = {}
    while True:
        now = time.ticks_ms()
        good = True
        for monitor_func, timeout in __wdt_monitors:
            good_time = good_times.get(id(monitor_func), now)
            if not monitor_func():
                good_time = now
            elif time.ticks_diff(now, good_time) >= timeout:
                good = False
            good_times[id(monitor_func)] = good_time
        if good:
            __wdt.feed()
        await asyncio.sleep(1)


async def wifi_task():
    # Reconnect WIFI to renew DHCP lease
    while True:
        __nic.active(True)
        __nic.connect(config.WIFI_SSID, config.WIFI_PASSWORD)
        await asyncio.sleep(60 * 60)
        __nic.disconnect()
        __nic.active(False)
        __nic.deinit()


def ble_send(method, **options):
    if not __ble_write_char:
        raise ValueError("not connected")
    options["method"] = options.get("method", method)
    options["messageId"] = options.get(
        "messageId", binascii.hexlify(os.urandom(16)).decode()
    )
    if not method.startswith("BLE"):
        options["deviceId"] = options.get("deviceId", str(config.DEVICE_ID))
        options["timestamp"] = options.get("timestamp", int(time.time()))
    return __ble_write_char.write(json.dumps(options))


def ble_set_output_power_limit(power):
    inverter_max_power = __data.get("properties", {}).get("inverseMaxPower")
    if inverter_max_power is None:
        raise ValueError("inverter max power unknown")
    if power > inverter_max_power:
        raise ValueError("power limit must not exceed inverter max power")
    if power < 100 and power % 30 != 0:
        raise ValueError("if power limit is < 100, it must be a multiple of 30")
    return ble_send("write", properties={"outputLimit": power})


async def ble_task():
    global __ble_write_char, __data, __last_update_ticks_ms
    while True:
        __ble_write_char = None
        try:
            device = aioble.Device(aioble.ADDR_PUBLIC, config.DEVICE_MAC)
            connection = await device.connect()
            connection_start = time.ticks_ms()
            async with connection:
                service = None
                async for s in connection.services():
                    if s.uuid == SERVICE_ID:
                        service = s
                if not service:
                    raise Exception("Service not found")
                notify_char = await service.characteristic(NOTIFY_ID)
                write_char_preliminary = await service.characteristic(WRITE_ID)
                __data.clear()
                get_info_sent = False
                while True:
                    if (
                        not __ble_write_char
                        and time.ticks_diff(time.ticks_ms(), connection_start) > 60_000
                    ):
                        raise asyncio.TimeoutError(
                            "BLESPP not received within 60 seconds"
                        )
                    try:
                        msg = json.loads(await notify_char.notified(timeout_ms=10_000))
                    except asyncio.TimeoutError:
                        continue
                    if msg.get("deviceId") != config.DEVICE_ID:
                        print(f"unexpected message: {msg}")
                        continue
                    __last_update_ticks_ms = time.ticks_ms()
                    if msg.get("method") == "BLESPP":
                        __ble_write_char = write_char_preliminary
                        await ble_send("BLESPP_OK")
                        if get_info_sent:
                            continue
                        await ble_send("getInfo")
                        await ble_send("read", properties=["getAll"])
                        get_info_sent = True
                        continue
                    for key in ["deviceSn", "modules", "firmwares", "offData"]:
                        if key in msg:
                            __data[key] = msg[key]
                    if "data" in msg:
                        __data["data"] = __data.get("data", [])
                        __data["data"].extend(msg["data"])
                    if "properties" in msg:
                        __data["properties"] = __data.get("properties", {})
                        __data["properties"].update(msg["properties"])
                    if "packData" in msg:
                        __data["packData"] = __data.get("packData", [])
                        for pack in msg["packData"]:
                            for i, old_pack in enumerate(__data["packData"]):
                                if old_pack["sn"] == pack["sn"]:
                                    old_pack.update(pack)
                                    break
                            else:
                                __data["packData"].append(pack)
        except MemoryError:
            raise
        except Exception as e:
            sys.print_exception(e)
            await asyncio.sleep(60)


async def get_info_task():
    last_request = None
    while True:
        await asyncio.sleep(60 * 5)
        if not __ble_write_char:
            last_request = None
            continue
        if last_request is not None and (
            __last_update_ticks_ms is None
            or time.ticks_diff(__last_update_ticks_ms, last_request) < 0
        ):
            raise Exception("stale BLE connection")
        last_request = time.ticks_ms()
        try:
            await ble_send("getInfo")
            await ble_send("read", properties=["getAll"])
        except MemoryError:
            raise
        except Exception as e:
            sys.print_exception(e)


async def power_task():
    global __auto_power_info_incoming, __auto_power_info_total
    global __auto_power_info_remaining, __auto_power_info_new_limit
    global __auto_power_info_skip, __auto_power_info_active
    global __auto_power_info_data
    while True:
        await asyncio.sleep(60)
        if not __meter_available:
            continue
        try:
            props = __data.get("properties", {})
            output_power = props.get("outputHomePower")
            output_power_limit = props.get("outputLimit")
            inverter_max_power = props.get("inverseMaxPower")
            should_set_output_power_limit = (
                output_power is not None
                and output_power_limit is not None
                and inverter_max_power is not None
                and __ble_write_char
                and __auto_power_limit
            )
            __auto_power_info_data = {}
            try:
                data = requests.get(config.METER_ENDPOINT).json()
                if not (
                    isinstance(data, dict)
                    and isinstance(
                        data.get(config.METER_POWER_FIELD), (float, int, None)
                    )
                    and isinstance(
                        data.get(config.METER_POWER_DISPLAY_FIELD), (float, int, None)
                    )
                ):
                    raise TypeError(f"invalid meter data: {data!r}")
            except Exception:
                if should_set_output_power_limit and output_power_limit != 0:
                    await ble_set_output_power_limit(0)
                raise
            __auto_power_info_data = data
            if not should_set_output_power_limit:
                __auto_power_info_active = False
                continue
            incoming = data.get(config.METER_POWER_FIELD)
            if incoming is None:
                __auto_power_info_active = False
                continue
            total = incoming + output_power
            remaining = total - output_power_limit
            target = round(
                total - (config.POWER_LOWER_LIMIT + config.POWER_UPPER_LIMIT) / 2
            )
            new_limit = max(0, min(math.floor(inverter_max_power), round(target)))
            if new_limit < 100:
                new_limit = (new_limit // 30) * 30
            skip = (
                (
                    config.POWER_LOWER_LIMIT <= remaining
                    and remaining <= config.POWER_UPPER_LIMIT
                )
                or output_power_limit == new_limit
                or (
                    new_limit > output_power_limit
                    and output_power * 1.2 + 20 <= output_power_limit
                )
            )
            if not skip:
                await ble_set_output_power_limit(new_limit)
            __auto_power_info_incoming = incoming
            __auto_power_info_total = total
            __auto_power_info_remaining = remaining
            __auto_power_info_new_limit = new_limit
            __auto_power_info_skip = skip
            __auto_power_info_active = True
        except MemoryError:
            raise
        except Exception as e:
            sys.print_exception(e)
            __auto_power_info_active = False


app = Microdot()


@app.errorhandler(MemoryError)
async def memory_error(request, exception):
    request.app.shutdown()
    return "Out Of Memory", 500


def normalize_time(value):
    if value == 59940:
        return None
    return value


def normalize_temp(value):
    if value is None:
        return None
    return (value - 2731) / 10


def q(s):
    """Quote HTML"""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


async def html_header_stream(t):
    yield "<!doctype html>"
    yield f'<html lang="{q(t.lang)}">'
    yield '<meta charset="utf-8">'
    yield ('<meta content="width=device-width, initial-scale=1" name="viewport">')
    yield "<title>"
    yield q(t("Solar"))
    yield "</title>"
    yield "<style>"
    yield ":root {"
    yield "color-scheme:light dark;"
    yield "}"
    yield ".error {"
    yield "background:Canvas;"
    yield "color:red;"
    yield "position:sticky;"
    yield "top:0;"
    yield "}"
    yield ":link, :visited {"
    yield "color:LinkText;"
    yield "text-decoration:none;"
    yield "}"
    yield ".inactive {"
    yield "opacity:0.2;"
    yield "}"
    yield "label, select, button {"
    yield "display:block;"
    yield "margin:8px 0;"
    yield "}"
    yield "input[type=number], select, button {"
    yield "min-width:calc(min(15rem,100%));"
    yield "}"
    yield "input[type=radio] {"
    yield "margin-right:0.5em;"
    yield "}"
    yield ".line-through {"
    yield "text-decoration-line:line-through;"
    yield "}"
    yield "</style>"


def html_error(t, message, status=400):
    async def stream(t):
        yield from html_header_stream(t)
        yield '<h1 class="error">'
        yield f'{q(t("Solar"))} - {q(t("Error"))}'
        yield "</h1>"
        yield f"<h2>{q(message)}</h2>"

    return Response(
        body=stream(t),
        status_code=status,
        headers={"Content-Type": "text/html; charset=utf-8"},
    )


@app.before_request
async def check_csrf(request):
    if request.method == "POST" and "csrf" not in request.cookies:
        return html_error("csrf cookie missing")


@app.after_request
async def set_csrf_cookie(request, response):
    response.set_cookie("csrf", "; SameSite=Strict", path="/", http_only=True)


pvBrands = ["Hoymiles", "Enphase", "APsystems", "Anker", "Deye", "BossWerk", "Tsun"]


@app.get("/diagram.svg")
def diagram_svg(request):
    return Response.send_file(
        "/diagram.svgz", content_type="image/svg+xml", compressed=True
    )


@app.get("/")
def index(request):
    async def stream(t):
        def enum(index, *entries):
            if index is None or index < 0 or len(entries) <= index:
                return t.no_value
            return entries[index]

        def kv(
            name,
            value,
            extra=None,
            *,
            setting=None,
            raw_name=False,
            raw_value=False,
            raw_extra=False,
            class_name=None,
        ):
            name = name if raw_name else q(name or t.no_value)
            value = value if raw_value else q(value or t.no_value)
            extra = extra if raw_extra else (q(extra) if extra is not None else None)
            s = f"{name}: {value}"
            if extra:
                s += f" ({extra})"
            if setting:
                s += f' <a href="{q("/settings/"+setting)}">🛠</a>'
            return (
                "<p" + (f' class="{q(class_name)}"' if class_name else "") + f">{s}</p>"
            )

        props = __data.get("properties", {})
        packs = __data.get("packData", [])
        yield from html_header_stream(t)
        if config.REFRESH_WEBPAGE:
            yield '<style onload="' + q(
                "const refresh=()=>setTimeout(()=>"
                + "fetch(location.href)"
                + ".then((response)=>{"
                + "if(!response.ok){"
                + "throw new Error(`Response status: ${response.status}`);"
                + "}"
                + "return response.text();"
                + "})"
                + ".then((html)=>document.documentElement.innerHTML=html)"
                + ".catch((err)=>{"
                + "console.error(err);"
                + "refresh();"
                + 'document.getElementById("error").style'
                + '.removeProperty("display");'
                + f"}}),{config.REFRESH_WEBPAGE * 1000:.0f});"
                + "refresh();"
            ) + '"></style>'
        yield f'<h1>{q(t("Solar"))}</h1>'
        yield '<h2 id="error" class="error"'
        if __ble_write_char:
            yield ' style="display:none"'
        yield f'>{q(t("No connection"))}</h2>'
        yield '<svg xmlns="http://www.w3.org/2000/svg"'
        yield ' width="1024" height="691" viewBox="0 0 1024 691"'
        yield ' style="'
        yield "max-width:calc(min(100%,30rem));"
        yield "height:auto;"
        yield "fill:currentColor;"
        yield '">'
        svg_text_attr = 'font-size="40px" dominant-baseline="middle"'
        yield '<use href="diagram.svg#home"/>'
        yield '<use href="diagram.svg#inverter"/>'
        yield '<use href="diagram.svg#home-inverter-conn"/>'
        yield '<use href="diagram.svg#solar"/>'
        yield '<use href="diagram.svg#inverter-solar-conn"/>'
        value = props.get("solarInputPower")
        if value:
            yield '<use href="diagram.svg#solar-inverter"/>'
            yield '<use href="diagram.svg#solar-sun"/>'
            yield f'<text x="745" y="251" {svg_text_attr}>'
            yield q(t.number(value, "W"))
            yield "</text>"
        elif value == 0:
            yield '<use href="diagram.svg#solar-inverter-x"/>'
        value = props.get("outputHomePower")
        if value:
            yield '<use href="diagram.svg#inverter-home"/>'
            yield f'<text x="611" y="421" {svg_text_attr} text-anchor="end">'
            yield q(t.number(value, "W"))
            yield "</text>"
        elif value == 0:
            yield '<use href="diagram.svg#inverter-home-x"/>'
        if packs:
            yield '<use href="diagram.svg#battery"/>'
            yield '<use href="diagram.svg#inverter-battery-conn"/>'
            value = props.get("packState")
            if value == 0:
                yield '<use href="diagram.svg#battery-inverter-x"/>'
            elif value == 1:
                yield '<use href="diagram.svg#inverter-battery"/>'
                value = props.get("outputPackPower")
                if value:
                    yield f'<text x="745" y="481" {svg_text_attr}>'
                    yield q(t.number(value, "W"))
                    yield "</text>"
            elif value == 2:
                yield '<use href="diagram.svg#battery-inverter"/>'
                value = props.get("packInputPower")
                if value:
                    yield f'<text x="745" y="481" {svg_text_attr}>'
                    yield q(t.number(value, "W"))
                    yield "</text>"
            value = props.get("electricLevel")
            if value is not None:
                yield f'<text x="779" y="611" {svg_text_attr}>'
                yield q(t.number(value, "%"))
                yield "</text>"
            values = [
                normalize_temp(temp)
                for temp in (pack.get("maxTemp") for pack in packs)
                if temp is not None
            ]
            value = max(values) if values else None
            if value:
                yield f'<text x="630" y="611" {svg_text_attr} text-anchor="end">'
                yield q(t.number(value, "°C"))
                yield "</text>"
        if __meter_available:
            yield '<use href="diagram.svg#home-grid-conn"/>'
            yield '<use href="diagram.svg#grid"/>'
            value = __auto_power_info_data.get(config.METER_POWER_DISPLAY_FIELD)
            if value:
                if value < 0:
                    yield '<use href="diagram.svg#home-grid"/>'
                else:
                    yield '<use href="diagram.svg#grid-home"/>'
                yield f'<text x="274" y="421" {svg_text_attr} text-anchor="end">'
                yield q(t.number(abs(value), "W"))
                yield "</text>"
            elif value == 0:
                yield '<use href="diagram.svg#grid-home-x"/>'
        if props.get("pass"):
            yield '<use href="diagram.svg#bypass"/>'
        yield "</svg>"
        yield f'<h2>{q(t("Hub"))}</h2>'
        yield kv(t("Serial number"), __data.get("deviceSn"))
        yield kv(t("Software version"), props.get("masterSoftVersion"))
        yield kv(
            t("Buzzer"),
            enum(props.get("buzzerSwitch"), t("Off"), t("On")),
            setting="buzzer-switch",
        )
        yield kv(
            t("Automatic shutdown"),
            enum(props.get("hubState"), t("Off"), t("On")),
            setting="hub-state",
        )
        yield "<h2>"
        yield q(t("Output"))
        if props.get("pass"):
            yield f' {q(t("bypassed"))}'
        yield "</h2>"
        yield kv(t("Power"), t.number(props.get("outputHomePower"), "W"))
        yield kv(
            t("Bypass"),
            enum(props.get("passMode"), t("Auto"), t("Off"), t("On")),
            setting="pass-mode",
        )
        yield kv(
            t("Reset bypass to auto after one day"),
            enum(props.get("autoRecover"), t("Off"), t("On")),
            setting="auto-recover",
        )
        yield kv(
            t("Maximum inverter power"),
            t.number(props.get("inverseMaxPower"), "W"),
            enum(props.get("pvBrand"), t("Other"), *pvBrands),
            setting="inverse",
        )
        yield kv(
            t("Maximum power"),
            t.number(props.get("outputLimit"), "W"),
            t("auto") if __auto_power_limit else None,
            setting="output-limit",
        )
        if __auto_power_limit:
            yield "<h3"
            if not __auto_power_info_active:
                yield ' class="line-through"'
            yield f'>{q(t("Automatic"))}</h3>'
            yield "<div"
            if not __auto_power_info_active:
                yield ' class="inactive"'
                yield ' aria-hidden="true"'
            yield ">"
            yield kv(
                t("Electricity meter"),
                f'<a href="{q(config.METER_ENDPOINT)}"'
                + f">{q(config.METER_ENDPOINT)}</a>",
                q(config.METER_POWER_FIELD),
                raw_value=True,
            )
            yield kv(t("Power import"), t.number(__auto_power_info_incoming, "W"))
            yield kv(
                t("Total power consumption"), t.number(__auto_power_info_total, "W")
            )
            yield kv(
                t("Remaining at limit"), t.number(__auto_power_info_remaining, "W")
            )
            yield kv(
                t("Target range"),
                t.number_range(
                    config.POWER_LOWER_LIMIT,
                    config.POWER_UPPER_LIMIT,
                    "W",
                ),
            )
            yield kv(
                t("New limit"),
                t.number(__auto_power_info_new_limit, "W"),
                class_name="line-through" if __auto_power_info_skip else None,
            )
            yield "</div>"
        yield f'<h2>{q(t("Solar"))}</h2>'
        yield kv(t("Total power"), t.number(props.get("solarInputPower"), "W"))
        yield kv(t("Panel {}", 1), t.number(props.get("solarPower1"), "W"))
        yield kv(t("Panel {}", 2), t.number(props.get("solarPower2"), "W"))
        yield f'<h2>{q(t("Battery"))}</h2>'
        yield kv(t("Charge level"), t.number(props.get("electricLevel"), "%"))
        yield kv(
            t("Minimum charge level"),
            t.number(props.get("minSoc"), "%", div=10),
            setting="min-soc",
        )
        yield kv(
            t("Maximum charge level"),
            t.number(props.get("socSet"), "%", div=10),
            setting="soc-set",
        )
        value = normalize_time(props.get("remainInputTime"))
        yield kv(
            t("Charging power"),
            t.number(props.get("outputPackPower"), "W"),
            t.minutes(value) if value is not None else None,
        )
        value = normalize_time(props.get("remainOutTime"))
        yield kv(
            t("Discharging power"),
            t.number(props.get("packInputPower"), "W"),
            t.minutes(value) if value is not None else None,
        )
        for i, pack in enumerate(packs):
            yield f'<h3>{q(t("Pack {}", i + 1))}</h3>'
            yield kv(t("Serial number"), pack.get("sn"))
            yield kv(t("Software version"), pack.get("softVersion"))
            yield kv(
                t("Power"),
                t.number(pack.get("power"), "W"),
                enum(
                    pack.get("state"),
                    t("inactive"),
                    t("charging"),
                    t("discharging"),
                ),
            )
            yield kv(t("Charge level"), t.number(pack.get("socLevel"), "%"))
            yield kv(
                t("Maximum temperature"),
                t.number(normalize_temp(pack.get("maxTemp")), "°C"),
            )
            yield kv(t("State of health"), t.number(pack.get("soh"), "%", div=10))

    return Response(
        body=stream(get_translation(request)),
        status_code=200,
        headers={"Content-Type": "text/html; charset=utf-8"},
    )


@app.get("/settings/output-limit")
def output_limit(request):
    async def stream(t):
        props = __data.get("properties", {})
        yield from html_header_stream(t)
        yield f'<h1>{q(t("Solar"))} - {q(t("Settings"))}</h1>'
        yield '<form method="POST" action="/settings/output-limit">'
        yield "<label>"
        yield f'<h2>{q(t("Maximum power"))}</h2>'
        yield '<input type="number" name="limit" required step="1" min="0"'
        value = props.get("inverseMaxPower")
        if value is not None:
            yield f' max="{q(value)}"'
        js = (
            "this.setCustomValidity(this.value < 100 && this.value % 30 ? "
            + repr(t("value must be >= 100 or a multiple of 30"))
            + ' : "")'
        )
        yield f' onChange="{q(js)}"'
        yield f' value="{q(props.get("outputLimit", ""))}"></label>'
        yield '<button type="submit" name="mode" value="manual">'
        yield q(t("Apply"))
        yield "</button>"
        yield f'<button type="reset">{q(t("Reset"))}</button>'
        yield "</form>"
        if __meter_available:
            yield '<form method="POST" action="/settings/output-limit">'
            yield '<button type="submit" name="mode" value="auto">'
            yield q(t("Auto"))
            yield "</button>"
            yield "</form>"

    return Response(
        body=stream(get_translation(request)),
        status_code=200,
        headers={"Content-Type": "text/html; charset=utf-8"},
    )


@app.post("/settings/output-limit")
def output_limit_set(request):
    global __auto_power_limit
    try:
        mode = request.form["mode"]
        if mode != "auto" and mode != "manual":
            raise ValueError("invalid mode")
        if mode == "auto":
            if not __meter_available:
                raise ValueError("meter not available")
            if not __auto_power_limit:
                open("auto-power-limit", "a").close()
                __auto_power_limit = True
            return redirect("/")
        limit = int(request.form["limit"])
        if limit < 0:
            raise ValueError("limit must be >= 0")
        if __auto_power_limit:
            os.remove("auto-power-limit")
            __auto_power_limit = False
        asyncio.create_task(ble_set_output_power_limit(limit))
    except MemoryError:
        raise
    except Exception as e:
        sys.print_exception(e)
        return html_error(e)
    return redirect("/")


@app.get("/settings/min-soc")
def min_soc(request):
    async def stream(t):
        props = __data.get("properties", {})
        yield from html_header_stream(t)
        yield f'<h1>{q(t("Solar"))} - {q(t("Settings"))}</h1>'
        yield '<form method="POST" action="/settings/min-soc">'
        yield "<label>"
        yield f'<h2>{q(t("Minimum charge level"))}</h2>'
        yield '<input type="number" name="value" required min="0" max="50"'
        value = props.get("minSoc")
        if value is not None:
            value = value // 10
        else:
            value = ""
        yield f' value="{q(value)}"></label>'
        yield f'<button type="submit">{q(t("Apply"))}</button>'
        yield f'<button type="reset">{q(t("Reset"))}</button>'
        yield "</form>"

    return Response(
        body=stream(get_translation(request)),
        status_code=200,
        headers={"Content-Type": "text/html; charset=utf-8"},
    )


@app.post("/settings/min-soc")
def min_soc_set(request):
    try:
        value = int(request.form["value"])
        if value < 0 or value > 50:
            raise ValueError("value must be >= 0 and <= 50")
        asyncio.create_task(ble_send("write", properties={"minSoc": value * 10}))
    except MemoryError:
        raise
    except Exception as e:
        sys.print_exception(e)
        return html_error(e)
    return redirect("/")


@app.get("/settings/soc-set")
def soc_set(request):
    async def stream(t):
        props = __data.get("properties", {})
        yield from html_header_stream(t)
        yield f'<h1>{q(t("Solar"))} - {q(t("Settings"))}</h1>'
        yield '<form method="POST" action="/settings/soc-set">'
        yield "<label>"
        yield f'<h2>{q(t("Maximum charge level"))}</h2>'
        yield '<input type="number" name="value" required min="70" max="100"'
        value = props.get("socSet")
        if value is not None:
            value = value // 10
        else:
            value = ""
        yield f' value="{q(value)}"></label>'
        yield f'<button type="submit">{q(t("Apply"))}</button>'
        yield f'<button type="reset">{q(t("Reset"))}</button>'
        yield "</form>"

    return Response(
        body=stream(get_translation(request)),
        status_code=200,
        headers={"Content-Type": "text/html; charset=utf-8"},
    )


@app.post("/settings/soc-set")
def soc_set_set(request):
    try:
        value = int(request.form["value"])
        if value < 70 or value > 100:
            raise ValueError("value must be >= 70 and <= 100")
        asyncio.create_task(ble_send("write", properties={"socSet": value * 10}))
    except MemoryError:
        raise
    except Exception as e:
        sys.print_exception(e)
        return html_error(e)
    return redirect("/")


@app.get("/settings/hub-state")
def hub_state(request):
    async def stream(t):
        props = __data.get("properties", {})
        yield from html_header_stream(t)
        yield f'<h1>{q(t("Solar"))} - {q(t("Settings"))}</h1>'
        yield '<form method="POST" action="/settings/hub-state">'
        yield f'<h2>{q(t("Automatic shutdown"))}</h2>'
        for value, label in [[1, t("On")], [0, t("Off")]]:
            yield '<label><input type="radio" name="value" required'
            if props.get("hubState") == value:
                yield " checked"
            yield f' value="{q(value)}">{q(label)}</label>'
        yield f'<button type="submit">{q(t("Apply"))}</button>'
        yield f'<button type="reset">{q(t("Reset"))}</button>'
        yield "</form>"

    return Response(
        body=stream(get_translation(request)),
        status_code=200,
        headers={"Content-Type": "text/html; charset=utf-8"},
    )


@app.post("/settings/hub-state")
def hub_state_set(request):
    try:
        value = int(request.form["value"])
        if value != 0 and value != 1:
            raise ValueError("value must be 0 or 1")
        asyncio.create_task(ble_send("write", properties={"hubState": value}))
    except MemoryError:
        raise
    except Exception as e:
        sys.print_exception(e)
        return html_error(e)
    return redirect("/")


@app.get("/settings/pass-mode")
def pass_mode(request):
    async def stream(t):
        props = __data.get("properties", {})
        yield from html_header_stream(t)
        yield f'<h1>{q(t("Solar"))} - {q(t("Settings"))}</h1>'
        yield '<form method="POST" action="/settings/pass-mode">'
        yield f'<h2>{q(t("Bypass"))}</h2>'
        for value, label in [[0, t("Auto")], [2, t("On")], [1, t("Off")]]:
            yield '<label><input type="radio" name="value" required'
            if props.get("passMode") == value:
                yield " checked"
            yield f' value="{q(value)}">{q(label)}</label>'
        yield f'<button type="submit">{q(t("Apply"))}</button>'
        yield f'<button type="reset">{q(t("Reset"))}</button>'
        yield "</form>"

    return Response(
        body=stream(get_translation(request)),
        status_code=200,
        headers={"Content-Type": "text/html; charset=utf-8"},
    )


@app.post("/settings/pass-mode")
def pass_mode_set(request):
    try:
        value = int(request.form["value"])
        if value != 0 and value != 1 and value != 2:
            raise ValueError("value must be 0, 1 or 2")
        asyncio.create_task(ble_send("write", properties={"passMode": value}))
    except MemoryError:
        raise
    except Exception as e:
        sys.print_exception(e)
        return html_error(e)
    return redirect("/")


@app.get("/settings/buzzer-switch")
def buzzer_switch(request):
    async def stream(t):
        props = __data.get("properties", {})
        yield from html_header_stream(t)
        yield f'<h1>{q(t("Solar"))} - {q(t("Settings"))}</h1>'
        yield '<form method="POST" action="/settings/buzzer-switch">'
        yield f'<h2>{q(t("Buzzer"))}</h2>'
        for value, label in [[1, t("On")], [0, t("Off")]]:
            yield '<label><input type="radio" name="value" required'
            if props.get("buzzerSwitch") == value:
                yield " checked"
            yield f' value="{q(value)}">{q(label)}</label>'
        yield f'<button type="submit">{q(t("Apply"))}</button>'
        yield f'<button type="reset">{q(t("Reset"))}</button>'
        yield "</form>"

    return Response(
        body=stream(get_translation(request)),
        status_code=200,
        headers={"Content-Type": "text/html; charset=utf-8"},
    )


@app.post("/settings/buzzer-switch")
def buzzer_switch_set(request):
    try:
        value = int(request.form["value"])
        if value != 0 and value != 1:
            raise ValueError("value must be 0 or 1")
        asyncio.create_task(ble_send("write", properties={"buzzerSwitch": value}))
    except MemoryError:
        raise
    except Exception as e:
        sys.print_exception(e)
        return html_error(e)
    return redirect("/")


@app.get("/settings/auto-recover")
def auto_recover(request):
    async def stream(t):
        props = __data.get("properties", {})
        yield from html_header_stream(t)
        yield f'<h1>{q(t("Solar"))} - {q(t("Settings"))}</h1>'
        yield '<form method="POST" action="/settings/auto-recover">'
        yield f'<h2>{q(t("Reset bypass to auto after one day"))}</h2>'
        for value, label in [[1, t("On")], [0, t("Off")]]:
            yield '<label><input type="radio" name="value" required'
            if props.get("autoRecover") == value:
                yield " checked"
            yield f' value="{q(value)}">{q(label)}</label>'
        yield f'<button type="submit">{q(t("Apply"))}</button>'
        yield f'<button type="reset">{q(t("Reset"))}</button>'
        yield "</form>"

    return Response(
        body=stream(get_translation(request)),
        status_code=200,
        headers={"Content-Type": "text/html; charset=utf-8"},
    )


@app.post("/settings/auto-recover")
def auto_recover_set(request):
    try:
        value = int(request.form["value"])
        if value != 0 and value != 1:
            raise ValueError("value must be 0 or 1")
        asyncio.create_task(ble_send("write", properties={"autoRecover": value}))
    except MemoryError:
        raise
    except Exception as e:
        sys.print_exception(e)
        return html_error(e)
    return redirect("/")


@app.get("/settings/inverse")
def inverse(request):
    async def stream(t):
        props = __data.get("properties", {})
        yield from html_header_stream(t)
        yield f'<h1>{q(t("Solar"))} - {q(t("Settings"))}</h1>'
        yield '<form method="POST" action="/settings/inverse">'
        yield "<label>"
        yield f'<h2>{q(t("Maximum inverter power"))}</h2>'
        yield '<input type="number" name="limit" required'
        yield ' step="100" min="100" max="1200"'
        yield f' value="{q(props.get("inverseMaxPower", ""))}"></label>'
        yield "<label>"
        yield f'<h2>{q(t("Inverter manufacturer"))}</h2>'
        yield '<select name="brand" required>'
        if props.get("pvBrand") is None:
            yield f'<option value="" selected>{t.no_value}</option>'
        options = [t("Other")]
        options.extend(pvBrands)
        for value, label in enumerate(options):
            yield f"<option value={q(value)}"
            if value == props.get("pvBrand"):
                yield " selected"
            yield f">{q(label)}</option>"
        yield "</select></label>"
        yield f'<button type="submit">{q(t("Apply"))}</button>'
        yield f'<button type="reset">{q(t("Reset"))}</button>'
        yield "</form>"

    return Response(
        body=stream(get_translation(request)),
        status_code=200,
        headers={"Content-Type": "text/html; charset=utf-8"},
    )


@app.post("/settings/inverse")
def inverse_set(request):
    try:
        limit = int(request.form["limit"])
        if limit < 100 or limit > 1200:
            raise ValueError("limit must be >= 100 and <= 1200")
        if limit % 100:
            raise ValueError("limit must be multiple 100")
        brand = int(request.form["brand"])
        if brand < 0 or brand > len(pvBrands):
            raise ValueError(f"limit must be >= 0 and <= {len(pvBrands)}")
        asyncio.create_task(
            ble_send("write", properties={"pvBrand": brand, "inverseMaxPower": limit})
        )
    except MemoryError:
        raise
    except Exception as e:
        sys.print_exception(e)
        return html_error(e)
    return redirect("/")


@app.get("/data")
def data(request):
    if not __ble_write_char:
        return "No Data", 503
    props = __data.get("properties", {})
    return {
        "batteryLevel": props.get("electricLevel"),
        "batteryChargePower": props.get("outputPackPower"),
        "batteryDischargePower": props.get("packInputPower"),
        "solarPower": props.get("solarInputPower"),
        "outputPower": props.get("outputHomePower"),
        "outputPowerLimit": props.get("outputLimit"),
        "autoOutputPowerLimit": __auto_power_limit,
        "bypass": (bool(props["pass"]) if props.get("pass") is not None else None),
    }


@app.get("/raw-data")
def raw_data(request):
    return __data


__wdt_monitors.extend(
    [
        (asyncio.create_task(ble_task()).done, 0),
        (asyncio.create_task(power_task()).done, 0),
        (asyncio.create_task(get_info_task()).done, 0),
        (asyncio.create_task(watchdog_task()).done, 0),
        (asyncio.create_task(wifi_task()).done, 0),
        (lambda: not __nic.isconnected(), 600_000),
        (lambda: __ble_write_char is None, 600_000),
    ]
)
app.run(port=80)
