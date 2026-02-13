import socket
import json
import threading
import time
import traceback
import queue
import dearpygui.dearpygui as dpg

BIND_ADDR_UDP = "0.0.0.0"
JSON_PORT = 9998

SAMPLE_HZ = 60.0
SAMPLE_DT = 1.0 / SAMPLE_HZ

UI_HZ = 30.0
UI_DT = 1.0 / UI_HZ

MAX_POINTS = 999999

start_time = time.time()

scroll_ready = False
scroll_active = True
scroll_time = 0.0

_last_store_t = None
_last_plot_push = 0.0

sample_q = queue.Queue()

history = {
    "t": [],
    "rpm": [],
    "speed_kmh": [],
    "speed_mph": [],
    "boost_psi": [],
    "throttle": [],
    "brake": [],
    "clutch": [],
}

meta = {
    "car": "???",
    "gear": 0,
    "last_time": 0.0,

    "rx_count": 0,
    "pkt_ok": 0,
    "json_fail": 0,
}

STATUS_TEXT_TAG = "status_text"
STATUS_BOX_TAG = "status_box"

SERIES_SPEED_KMH = "series_speed_kmh"
SERIES_SPEED_MPH = "series_speed_mph"
SERIES_RPM = "series_rpm"
SERIES_BOOST = "series_boost"
SERIES_THR = "series_throttle"
SERIES_BRK = "series_brake"
SERIES_CLT = "series_clutch"

PLOT_SPEED = "plot_speed"
PLOT_RPM = "plot_rpm"
PLOT_BOOST = "plot_boost"
PLOT_PEDALS = "plot_pedals"

# viewport/layout cache
_last_vp_w = None
_last_vp_h = None


def on_autoscroll(sender, app_data=None, user_data=None):
    global scroll_active
    scroll_active = bool(dpg.get_value("en_autoscroll"))


def udp_json_listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    except Exception:
        pass

    sock.bind((BIND_ADDR_UDP, JSON_PORT))
    print(f"[JSON] Listening on {BIND_ADDR_UDP}:{JSON_PORT}")

    # Heartbeat so you can confirm the thread stays alive
    last_beat = time.time()
    last_rx = 0

    while True:
        try:
            data, _ = sock.recvfrom(65535)
            meta["rx_count"] += 1

            now = time.time()
            if now - last_beat >= 1.0:
                rx = meta["rx_count"]
                print(f"[UDP] rx_per_s={rx - last_rx} total_rx={rx} ok={meta['pkt_ok']} json_fail={meta['json_fail']}")
                last_rx = rx
                last_beat = now

        except Exception as e:
            print(f"[JSON] recv error: {e}")
            continue

        try:
            txt = data.decode("utf-8", errors="replace")
            obj = json.loads(txt)

            # local time axis (does not depend on sender)
            t_rel = time.time() - start_time

            rpm = float(obj.get("rpm", 0.0))
            speed_kmh = float(obj.get("kmh", 0.0))
            speed_mph = float(obj.get("mph", 0.0))
            boost_psi = float(obj.get("psi", obj.get("boost", 0.0)))

            thr = float(obj.get("throttle", obj.get("thr", 0.0)))
            brk = float(obj.get("brake", obj.get("brk", 0.0)))
            clt = float(obj.get("clutch", obj.get("clt", 0.0)))

            car = str(obj.get("car", "???"))
            gear = int(obj.get("gear", 0))

            meta["car"] = car
            meta["gear"] = gear
            meta["last_time"] = time.time()
            meta["pkt_ok"] += 1

            sample_q.put_nowait((t_rel, rpm, speed_kmh, speed_mph, boost_psi, thr, brk, clt))

        except Exception:
            meta["json_fail"] += 1
            pass


def _store_sample_decimated(sample):
    global _last_store_t, scroll_ready, scroll_time

    t, rpm, speed_kmh, speed_mph, boost_psi, thr, brk, clt = sample

    if _last_store_t is None:
        _last_store_t = t
        do_append = True
    else:
        do_append = (t - _last_store_t) >= SAMPLE_DT

    if do_append:
        _last_store_t = t
        history["t"].append(t)
        history["rpm"].append(rpm)
        history["speed_kmh"].append(speed_kmh)
        history["speed_mph"].append(speed_mph)
        history["boost_psi"].append(boost_psi)
        history["throttle"].append(thr)
        history["brake"].append(brk)
        history["clutch"].append(clt)
    else:
        if history["t"]:
            history["t"][-1] = t
            history["rpm"][-1] = rpm
            history["speed_kmh"][-1] = speed_kmh
            history["speed_mph"][-1] = speed_mph
            history["boost_psi"][-1] = boost_psi
            history["throttle"][-1] = thr
            history["brake"][-1] = brk
            history["clutch"][-1] = clt

    if len(history["t"]) > MAX_POINTS:
        for k in history:
            history[k] = history[k][-MAX_POINTS:]

    elapsed = time.time() - start_time
    if (not scroll_ready) and elapsed >= 30.0:
        scroll_ready = True
    if scroll_ready:
        scroll_time = elapsed


def _drain_queue(max_items=10000):
    n = 0
    while n < max_items:
        try:
            s = sample_q.get_nowait()
        except queue.Empty:
            break
        _store_sample_decimated(s)
        n += 1
    return n


def _apply_time_axis_limits(elapsed_time: float):
    if scroll_active:
        if scroll_ready:
            lo, hi = (scroll_time - 30.0), (scroll_time + 1.0)
        else:
            lo, hi = 0.0, (elapsed_time + 1.0)

        dpg.set_axis_limits("speed_time", lo, hi)
        dpg.set_axis_limits("rpm_time", lo, hi)
        dpg.set_axis_limits("boost_time", lo, hi)
        dpg.set_axis_limits("pedal_time", lo, hi)
    else:
        dpg.set_axis_limits_auto("speed_time")
        dpg.set_axis_limits_auto("rpm_time")
        dpg.set_axis_limits_auto("boost_time")
        dpg.set_axis_limits_auto("pedal_time")


def _prime_layout():
    global _last_vp_w, _last_vp_h

    try:
        vp_w = dpg.get_viewport_client_width()
        vp_h = dpg.get_viewport_client_height()
    except Exception:
        # fallback if client_* not available
        vp_w = dpg.get_viewport_width()
        vp_h = dpg.get_viewport_height()

    if vp_w <= 0 or vp_h <= 0:
        return

    if _last_vp_w == vp_w and _last_vp_h == vp_h:
        return

    _last_vp_w, _last_vp_h = vp_w, vp_h

    # Make primary window fill viewport
    try:
        dpg.configure_item("primary", width=vp_w, height=vp_h)
        dpg.set_item_pos("primary", [0, 0])
    except Exception:
        pass
    
    TOP_UI_EST = 120  # rough but stable


    avail = vp_h - TOP_UI_EST
    if avail < 200:
        plot_h = 120
    else:
        plot_h = int((avail - 12) / 1.9)
        if plot_h < 120:
            plot_h = 120

    try:
        dpg.configure_item(STATUS_BOX_TAG, height=STATUS_BOX_H)
    except Exception:
        pass

    for plot_tag in (PLOT_SPEED, PLOT_RPM, PLOT_BOOST, PLOT_PEDALS):
        try:
            dpg.configure_item(plot_tag, height=plot_h)
        except Exception:
            pass

def update_ui_tick():
    global _last_plot_push

    _prime_layout()

    drained = _drain_queue()

    now = time.time()
    elapsed = now - start_time
    _apply_time_axis_limits(elapsed)

    # push plots at UI_DT
    if (now - _last_plot_push) >= UI_DT:
        _last_plot_push = now

        if history["t"]:
            t = history["t"]
            dpg.set_value(SERIES_SPEED_KMH, [t, history["speed_kmh"]])
            dpg.set_value(SERIES_SPEED_MPH, [t, history["speed_mph"]])
            dpg.set_value(SERIES_RPM, [t, history["rpm"]])
            dpg.set_value(SERIES_BOOST, [t, history["boost_psi"]])
            dpg.set_value(SERIES_THR, [t, history["throttle"]])
            dpg.set_value(SERIES_BRK, [t, history["brake"]])
            dpg.set_value(SERIES_CLT, [t, history["clutch"]])

    # status
    age = time.time() - meta["last_time"] if meta["last_time"] else 9999.0

    if meta["gear"] == 0:
        gear_txt = "R"
    elif meta["gear"] == 1:
        gear_txt = "N"
    else:
        gear_txt = str(meta["gear"] - 1)

    if history["t"]:
        status = (
            f"Car {meta['car']} | Gear {gear_txt} | "
            f"{history['rpm'][-1]:.0f} rpm | {history['speed_kmh'][-1]:.1f} km/h | "
            f"Boost {history['boost_psi'][-1]:.1f} psi | "
            f"{'LIVE' if age < 1.0 else f'{age:.1f}s since last packet'}\n"
        )
    else:
        status = (
            f"Waiting for data on {BIND_ADDR_UDP}:{JSON_PORT}...\n"
        )

    dpg.set_value(STATUS_TEXT_TAG, status)


def build_ui():
    # initial guess; will be overridden by _prime_layout()
    plot_height = 300

    with dpg.window(
        tag="primary",
        label=f"ErinsMod Telemetry | Source: JSON UDP @ {BIND_ADDR_UDP}:{JSON_PORT}",
        width=1200,
        height=700,
        no_scrollbar=True,
        no_scroll_with_mouse=True,
        no_collapse=True,
    ):
        dpg.add_checkbox(label="Auto-Scroll", tag="en_autoscroll", callback=on_autoscroll)
        dpg.set_value("en_autoscroll", scroll_active)
        dpg.add_separator()

        dpg.add_text(default_value="Status:", color=(200, 200, 200, 255))
        dpg.add_input_text(
            default_value="Starting…",
            tag=STATUS_TEXT_TAG,
            multiline=False,
            readonly=True,
            width=-1,
            height=30,
        )
        dpg.set_item_alias(STATUS_TEXT_TAG, STATUS_TEXT_TAG)

        try:
            dpg.set_item_alias(STATUS_TEXT_TAG, STATUS_BOX_TAG)
        except Exception:
            pass

        dpg.add_spacer(height=2)

        with dpg.table(header_row=False, policy=dpg.mvTable_SizingStretchProp, resizable=False):
            dpg.add_table_column()
            dpg.add_table_column()

            with dpg.table_row():
                with dpg.table_cell():
                    with dpg.plot(tag=PLOT_SPEED, label="Speed (km/h & mph)", height=plot_height, width=-1):
                        dpg.add_plot_legend()
                        dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", tag="speed_time")
                        yaxis = dpg.add_plot_axis(dpg.mvYAxis, label="Speed", auto_fit=True)
                        dpg.add_line_series([], [], label="km/h", parent=yaxis, tag=SERIES_SPEED_KMH)
                        dpg.add_line_series([], [], label="mph", parent=yaxis, tag=SERIES_SPEED_MPH)

                with dpg.table_cell():
                    with dpg.plot(tag=PLOT_RPM, label="RPM", height=plot_height, width=-1):
                        dpg.add_plot_legend()
                        dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", tag="rpm_time")
                        yaxis = dpg.add_plot_axis(dpg.mvYAxis, label="RPM", auto_fit=True)
                        dpg.add_line_series([], [], label="rpm", parent=yaxis, tag=SERIES_RPM)

            with dpg.table_row():
                with dpg.table_cell():
                    with dpg.plot(tag=PLOT_BOOST, label="Boost (psi)", height=plot_height, width=-1):
                        dpg.add_plot_legend()
                        dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", tag="boost_time")
                        yaxis = dpg.add_plot_axis(dpg.mvYAxis, label="psi", auto_fit=True)
                        dpg.add_line_series([], [], label="boost psi", parent=yaxis, tag=SERIES_BOOST)

                with dpg.table_cell():
                    with dpg.plot(tag=PLOT_PEDALS, label="Throttle / Brake / Clutch", height=plot_height, width=-1):
                        dpg.add_plot_legend()
                        dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", tag="pedal_time")
                        yaxis = dpg.add_plot_axis(dpg.mvYAxis, label="Value", auto_fit=True)
                        dpg.add_line_series([], [], label="throttle", parent=yaxis, tag=SERIES_THR)
                        dpg.add_line_series([], [], label="brake", parent=yaxis, tag=SERIES_BRK)
                        dpg.add_line_series([], [], label="clutch", parent=yaxis, tag=SERIES_CLT)

    dpg.create_viewport(title="ErinsMod Telemetry", width=1200, height=700, resizable=True)
    dpg.setup_dearpygui()
    dpg.show_viewport()
    dpg.set_primary_window("primary", True)


def main():
    # UDP thread
    t = threading.Thread(target=udp_json_listener, daemon=True)
    t.start()

    dpg.create_context()
    build_ui()

    # Prime layout once after showing viewport
    _prime_layout()

    try:
        while dpg.is_dearpygui_running():
            try:
                update_ui_tick()
            except Exception:
                print("update_ui_tick exception:")
                traceback.print_exc()
            dpg.render_dearpygui_frame()
    finally:
        dpg.destroy_context()


if __name__ == "__main__":
    main()
