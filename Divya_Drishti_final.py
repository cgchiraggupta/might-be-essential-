#!/usr/bin/env python3
"""
DIVYA DRISHTI - Phase 1 Final Integration
Hardware: RPi Zero, Dual VL53L5CX ToF (separate I2C buses),
          Camera, INMP441 Mic, MAX98357A Amp + Speaker,
          Dual Vibration Motors (Left/Right directional)
"""

import RPi.GPIO as GPIO
import cv2
import time
import threading
import subprocess
import os
import json
import queue
import random
import string
import sys
import hmac
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import smbus2
import speech_recognition as sr
import requests
from picamera2 import Picamera2

try:
    from vl53l5cx_ctypes import VL53L5CX
    TOF_LIB_AVAILABLE = True
except ImportError:
    TOF_LIB_AVAILABLE = False

# ══════════════════════════════════════════
#  PIN CONFIGURATION
# ══════════════════════════════════════════
MOTOR_LEFT  = 12   # Pin 32
MOTOR_RIGHT = 13   # Pin 33

GPIO.setmode(GPIO.BCM)
GPIO.setup(MOTOR_LEFT,  GPIO.OUT)
GPIO.setup(MOTOR_RIGHT, GPIO.OUT)
GPIO.output(MOTOR_LEFT,  GPIO.LOW)
GPIO.output(MOTOR_RIGHT, GPIO.LOW)

# ══════════════════════════════════════════
#  THRESHOLDS
# ══════════════════════════════════════════
TOF_URGENT_MM   = 500
TOF_WARNING_MM  = 1000
TOF_CAUTION_MM  = 2000

CV_HIGH = 0.020
CV_MED  = 0.010

# ══════════════════════════════════════════
#  SHARED STATE
# ══════════════════════════════════════════
state = {
    "running":         True,
    "paused":          False,
    "last_scene":      "starting up",
    "frame_count":     0,
    "tof_ok":          False,
    "last_alert_side": None,
    "last_alert_zone": None,
    "tof_left_ok":     False,
    "tof_right_ok":    False,
    "camera_ok":       False,
    "mic_ok":          True,
    "device_id":       None,
    "pairing_code":    None,
}

state_lock = threading.Lock()
motor_lock = threading.Lock()

# ══════════════════════════════════════════
#  SUPABASE DEVICE SYNC
#  The service credential is Pi-local only. Never copy this file or
#  ~/.divyadrishti/config.env into the companion app repository.
# ══════════════════════════════════════════
CONFIG_DIR = Path.home() / ".divyadrishti"
CONFIG_FILE = CONFIG_DIR / "config.env"
DEVICE_FILE = CONFIG_DIR / "device.json"
sync_queue = queue.Queue(maxsize=200)
LOCAL_LINK_PORT = 8765

def utc_now():
    return datetime.now(timezone.utc).isoformat()

def load_sync_config():
    values = {}
    try:
        for line in CONFIG_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    except FileNotFoundError:
        print(f"[SYNC] No config at {CONFIG_FILE}; cloud sync disabled.")
    return values

SYNC_CONFIG = load_sync_config()
SUPABASE_URL = SYNC_CONFIG.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = SYNC_CONFIG.get("SUPABASE_SERVICE_KEY", "")

def sync_enabled():
    return bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)

def supabase_headers(prefer="return=minimal"):
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": prefer,
    }

def random_pairing_code():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(random.choice(alphabet) for _ in range(6))

def speak_pairing_code(code):
    spaced = ". ".join(code)
    speak(f"Your pairing code is {spaced}. I repeat. {spaced}", blocking=True)

def register_device_if_needed():
    """Register once and keep the server-assigned device ID on the Pi."""
    if not sync_enabled():
        return None

    try:
        saved = json.loads(DEVICE_FILE.read_text())
        if saved.get("device_id") and saved.get("pairing_code"):
            with state_lock:
                state["device_id"] = saved["device_id"]
                state["pairing_code"] = saved["pairing_code"]
            print(f"[SYNC] Using registered device {saved['device_id']}")
            return saved
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    for _ in range(5):
        pairing_code = random_pairing_code()
        payload = {
            "name": "Divya Drishti",
            "pairing_code": pairing_code,
            "last_seen_at": utc_now(),
        }
        try:
            response = requests.post(
                f"{SUPABASE_URL}/rest/v1/devices",
                headers=supabase_headers("return=representation"),
                json=payload,
                timeout=6,
            )
            if response.ok:
                row = response.json()[0]
                saved = {"device_id": row["id"], "pairing_code": pairing_code}
                DEVICE_FILE.write_text(json.dumps(saved) + "\n")
                os.chmod(DEVICE_FILE, 0o600)
                with state_lock:
                    state["device_id"] = saved["device_id"]
                    state["pairing_code"] = pairing_code
                print(f"[SYNC] Registered device with pairing code {pairing_code}")
                speak_pairing_code(pairing_code)
                return saved
            print(f"[SYNC] Registration rejected ({response.status_code})")
        except requests.RequestException as error:
            print(f"[SYNC] Registration failed: {error}")
            break
    return None

def enqueue_sync(method, endpoint, payload, prefer="return=minimal"):
    if not sync_enabled():
        return
    try:
        sync_queue.put_nowait((method, endpoint, payload, prefer))
    except queue.Full:
        print("[SYNC] Queue full; dropping oldest non-critical update.")

def queue_status(current_alert, mode):
    with state_lock:
        device_id = state["device_id"]
        payload = {
            "device_id": device_id,
            "battery_pct": None,  # No battery sensor is wired in Phase 1.
            "tof_left_ok": state["tof_left_ok"],
            "tof_right_ok": state["tof_right_ok"],
            "camera_ok": state["camera_ok"],
            "mic_ok": state["mic_ok"],
            "mode": mode,
            "current_alert": current_alert,
            "updated_at": utc_now(),
        }
    if not device_id:
        return
    enqueue_sync("POST", "device_status?on_conflict=device_id", payload,
                 "resolution=merge-duplicates,return=minimal")
    enqueue_sync("PATCH", f"devices?id=eq.{device_id}", {"last_seen_at": utc_now()})

def queue_event(event_type, detail):
    with state_lock:
        device_id = state["device_id"]
    if device_id:
        enqueue_sync("POST", "device_events", {
            "device_id": device_id,
            "event_type": event_type,
            "detail": detail,
        })

# ══════════════════════════════════════════
#  LOCAL COMPANION LINK (same Wi-Fi)
#  The phone can read nearby status even if cloud connectivity is slow. Commands
#  require the pairing code already held by the paired companion app.
# ══════════════════════════════════════════
def local_status_snapshot():
    with state_lock:
        return {
            "device_id": state["device_id"],
            "available": True,
            "paused": state["paused"],
            "last_scene": state["last_scene"],
            "tof_left_ok": state["tof_left_ok"],
            "tof_right_ok": state["tof_right_ok"],
            "camera_ok": state["camera_ok"],
            "mic_ok": state["mic_ok"],
            "updated_at": utc_now(),
        }

def local_pairing_code_matches(headers):
    with state_lock:
        expected = state["pairing_code"] or ""
    received = headers.get("X-Divya-Pairing-Code", "").strip().upper()
    return bool(expected and received and hmac.compare_digest(expected.upper(), received))

class LocalLinkServer(ThreadingHTTPServer):
    allow_reuse_address = True

class LocalLinkHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[LOCAL] {self.address_string()} - {fmt % args}")

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "https://localhost")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Divya-Pairing-Code")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_json(204, {})

    def do_GET(self):
        if self.path == "/v1/health":
            self.send_json(200, {"name": "Divya Drishti", "available": True})
            return
        if self.path != "/v1/status":
            self.send_json(404, {"error": "Not found"})
            return
        if not local_pairing_code_matches(self.headers):
            self.send_json(401, {"error": "Pairing required"})
            return
        self.send_json(200, local_status_snapshot())

    def do_POST(self):
        if self.path != "/v1/command":
            self.send_json(404, {"error": "Not found"})
            return
        if not local_pairing_code_matches(self.headers):
            self.send_json(401, {"error": "Pairing required"})
            return
        try:
            size = min(int(self.headers.get("Content-Length", "0")), 2048)
            payload = json.loads(self.rfile.read(size) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self.send_json(400, {"error": "Invalid request"})
            return
        command = str(payload.get("command", "")).lower()
        if command not in ("pause", "resume"):
            self.send_json(400, {"error": "Unsupported command"})
            return
        with state_lock:
            state["paused"] = command == "pause"
        queue_event("voice_command", {"command": command, "source": "companion_app"})
        self.send_json(200, local_status_snapshot())

def start_local_link(stop_event):
    try:
        server = LocalLinkServer(("0.0.0.0", LOCAL_LINK_PORT), LocalLinkHandler)
    except OSError as error:
        print(f"[LOCAL] Could not start companion link: {error}")
        return None

    def serve():
        print(f"[LOCAL] Companion link ready on port {LOCAL_LINK_PORT}")
        server.timeout = 0.5
        while not stop_event.is_set():
            server.handle_request()
        server.server_close()

    threading.Thread(target=serve, daemon=True).start()
    return server

def sync_worker(stop_event):
    """Cloud writes stay off the sensing and voice threads."""
    while not stop_event.is_set() or not sync_queue.empty():
        try:
            method, endpoint, payload, prefer = sync_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            response = requests.request(
                method,
                f"{SUPABASE_URL}/rest/v1/{endpoint}",
                headers=supabase_headers(prefer),
                json=payload,
                timeout=4,
            )
            if not response.ok:
                print(f"[SYNC] {method} {endpoint} failed ({response.status_code})")
        except requests.RequestException as error:
            print(f"[SYNC] {method} {endpoint} failed: {error}")
        finally:
            sync_queue.task_done()

# ══════════════════════════════════════════
#  AUDIO
# ══════════════════════════════════════════
def speak(text, blocking=False):
    print(f"[AUDIO] {text}")
    env = {**os.environ, "AUDIODEV": "plughw:0,0"}
    args = ["espeak", "-s", "140", "-v", "en", text]
    kwargs = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    if blocking:
        subprocess.call(args, **kwargs)
    else:
        subprocess.Popen(args, **kwargs)

# ══════════════════════════════════════════
#  VIBRATION (dual motor, directional)
# ══════════════════════════════════════════
def vibrate(side, duration=0.3, intensity=100):
    with motor_lock:
        pwm_l = GPIO.PWM(MOTOR_LEFT,  1000)
        pwm_r = GPIO.PWM(MOTOR_RIGHT, 1000)

        if side in ("left",  "both"): pwm_l.start(intensity)
        if side in ("right", "both"): pwm_r.start(intensity)

        time.sleep(duration)
        pwm_l.stop()
        pwm_r.stop()
        GPIO.output(MOTOR_LEFT,  GPIO.LOW)
        GPIO.output(MOTOR_RIGHT, GPIO.LOW)

def vibrate_pattern(side, pattern):
    if pattern == "rapid":
        # Urgent is still one distinct warning, not a continuous buzz.
        vibrate(side, 0.35, 100)
    elif pattern == "double":
        vibrate(side, 0.2, 100)
        time.sleep(0.1)
        vibrate(side, 0.2, 100)
    elif pattern == "single":
        vibrate(side, 0.3, 70)
    elif pattern == "pulse":
        vibrate(side, 0.5, 80)

# ══════════════════════════════════════════
#  TOF SENSORS (dual, separate I2C buses)
# ══════════════════════════════════════════
def init_tof_sensors():
    """
    Sensor 1 (LEFT)  -> bus 3 (GPIO23/24, Pin 16/18)
    Sensor 2 (RIGHT) -> bus 4 (GPIO17/27, Pin 11/13)
    Independent buses, both at default address 0x29 - no conflict.
    """
    if not TOF_LIB_AVAILABLE:
        print("[TOF] Library not installed, skipping ToF sensors.")
        return None, None, None, None

    try:
        print("[TOF] Opening bus 3 (sensor LEFT)...")
        bus3 = smbus2.SMBus(3)

        tof1 = None
        for attempt in range(3):
            try:
                tof1 = VL53L5CX(i2c_dev=bus3)
                print(f"[TOF] Sensor LEFT OK on bus 3 (attempt {attempt+1})")
                break
            except Exception as e:
                print(f"[TOF] Sensor LEFT attempt {attempt+1} failed: {e}")
                time.sleep(1.0)

        if tof1 is None:
            raise RuntimeError("Sensor LEFT failed after 3 attempts")

        print("[TOF] Opening bus 4 (sensor RIGHT)...")
        bus4 = smbus2.SMBus(4)

        tof2 = None
        for attempt in range(3):
            try:
                tof2 = VL53L5CX(i2c_dev=bus4)
                print(f"[TOF] Sensor RIGHT OK on bus 4 (attempt {attempt+1})")
                break
            except Exception as e:
                print(f"[TOF] Sensor RIGHT attempt {attempt+1} failed: {e}")
                time.sleep(1.0)

        if tof2 is None:
            raise RuntimeError("Sensor RIGHT failed after 3 attempts")

        tof1.set_resolution(4 * 4)
        tof2.set_resolution(4 * 4)
        tof1.start_ranging()
        tof2.start_ranging()

        with state_lock:
            state["tof_ok"] = True
            state["tof_left_ok"] = True
            state["tof_right_ok"] = True

        print("[TOF] Dual sensors active.")
        return tof1, tof2, bus3, bus4

    except Exception as e:
        print(f"[TOF] Not available ({e}). Falling back to camera-only detection.")
        with state_lock:
            state["tof_ok"] = False
            state["tof_left_ok"] = False
            state["tof_right_ok"] = False
        return None, None, None, None

def read_tof(tof1, tof2):
    """Returns (d_left, d_right) in mm, filtering invalid 0mm zones."""
    if tof1 is None or tof2 is None:
        return None, None
    d_left, d_right = None, None
    try:
        if tof1.data_ready():
            zones = list(tof1.get_data().distance_mm[0])
            valid = [z for z in zones[:16] if z > 0]
            d_left = min(valid) if valid else None
        if tof2.data_ready():
            zones = list(tof2.get_data().distance_mm[0])
            valid = [z for z in zones[:16] if z > 0]
            d_right = min(valid) if valid else None
    except Exception:
        pass
    return d_left, d_right

# ══════════════════════════════════════════
#  CAMERA + CV
# ══════════════════════════════════════════
def init_camera():
    print("[CAMERA] Initializing...")
    try:
        picam2 = Picamera2()
        picam2.configure(
            picam2.create_still_configuration(main={"size": (320, 240)})
        )
        picam2.start()
        time.sleep(2)
        with state_lock:
            state["camera_ok"] = True
        print("[CAMERA] Ready.")
        return picam2
    except Exception as error:
        # Known hardware TODO: Picamera2 IndexError here means libcamera found
        # no camera. Keep ToF/audio/vibration running until ribbon/config is fixed.
        with state_lock:
            state["camera_ok"] = False
        print(f"[CAMERA] Unavailable ({error}); continuing without camera.")
        return None

def analyze_frame(frame):
    gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    left   = gray[:, :w//3]
    center = gray[:, w//3:2*w//3]
    right  = gray[:, 2*w//3:]
    bottom = gray[2*h//3:, :]

    def density(zone, lo=50, hi=150):
        return cv2.Canny(zone, lo, hi).mean() / 255.0

    return {
        "left":   density(left),
        "center": density(center),
        "right":  density(right),
        "floor":  density(bottom, 30, 100),
    }

# ══════════════════════════════════════════
#  OBSTACLE DECISION LOGIC (ToF primary, CV secondary)
# ══════════════════════════════════════════
def decide_alert(tof_left, tof_right, cv_densities):
    """Prefer ToF (accurate, real distance) over CV (heuristic) when available."""

    # ── ToF path ──
    if tof_left is not None or tof_right is not None:
        vals = [d for d in [tof_left, tof_right] if d is not None]
        closest = min(vals) if vals else None

        if tof_left and tof_right:
            dist_cm = closest // 10
            if closest < TOF_URGENT_MM:
                return ("both", "rapid", f"obstacle directly ahead, {dist_cm} centimeters")
            elif closest < TOF_WARNING_MM:
                return ("both", "double", f"obstacle ahead, {dist_cm} centimeters")
            elif closest < TOF_CAUTION_MM:
                return ("both", "single", None)
        elif tof_left and tof_left < TOF_WARNING_MM:
            dist_cm = tof_left // 10
            return ("left", "double", f"obstacle on left, {dist_cm} centimeters")
        elif tof_right and tof_right < TOF_WARNING_MM:
            dist_cm = tof_right // 10
            return ("right", "double", f"obstacle on right, {dist_cm} centimeters")

        return (None, None, "path clear")


    # ── CV fallback path ──
    d = cv_densities
    if d["center"] > CV_HIGH:
        return ("both", "rapid", "obstacle directly ahead")
    if d["center"] > CV_MED:
        return ("both", "double", "obstacle ahead")
    if d["left"] > CV_HIGH:
        return ("left", "double", "obstacle on left")
    if d["right"] > CV_HIGH:
        return ("right", "double", "obstacle on right")
    if d["floor"] > CV_HIGH:
        return ("both", "rapid", "uneven ground ahead")
    return (None, None, "path clear")

def event_type_for(side, message):
    message = (message or "").lower()
    if "path clear" in message:
        return "path_clear"
    if "uneven" in message:
        return "uneven_ground"
    if side == "left":
        return "obstacle_left"
    if side == "right":
        return "obstacle_right"
    return "obstacle_ahead"

# ══════════════════════════════════════════
#  DETECTION LOOP (background thread)
# ══════════════════════════════════════════
def detection_loop(picam2, tof1, tof2, stop_event):
    print("[DETECT] Detection loop started.")
    last_alert = 0
    last_alert_distance = None
    last_alert_side = None
    last_alert_pattern = None
    obstacle_active = False
    clear_since = None
    last_status_sync = 0

    while not stop_event.is_set():
        with state_lock:
            if state["paused"]:
                time.sleep(0.5)
                continue

        try:
            if picam2 is not None:
                frame = picam2.capture_array()
                cv_densities = analyze_frame(frame)
            else:
                # ToF remains the primary source while camera hardware is offline.
                cv_densities = {"left": 0, "center": 0, "right": 0, "floor": 0}
            tof_left, tof_right = read_tof(tof1, tof2)

            side, pattern, message = decide_alert(tof_left, tof_right, cv_densities)
            print(f"[DEBUG] tof_left={tof_left} tof_right={tof_right} -> side={side} pattern={pattern} msg={message}")



            with state_lock:
                state["frame_count"] += 1
                fc = state["frame_count"]
                state["last_scene"] = message or "path clear"

            if fc % 10 == 0:
                tof_str = f"TOF L={tof_left} R={tof_right}" if tof1 else "TOF n/a"
                print(f"[DETECT] Frame {fc}: {tof_str} | "
                      f"CV L={cv_densities['left']:.3f} "
                      f"C={cv_densities['center']:.3f} "
                      f"R={cv_densities['right']:.3f} | {message}")

            now = time.time()
            event_type = event_type_for(side, message)
            mode = "tof" if tof1 is not None else "camera_fallback"
            if now - last_status_sync >= 3.0:
                queue_status(event_type, mode)
                last_status_sync = now
            if side is not None:
                # Guidance is event-driven: alert once, then stay quiet until
                # the situation becomes materially more dangerous.
                clear_since = None
                distance_mm = min(d for d in (tof_left, tof_right) if d is not None) \
                    if tof_left is not None or tof_right is not None else None
                severity = {"single": 1, "double": 2, "rapid": 3}.get(pattern, 0)
                previous_severity = {"single": 1, "double": 2, "rapid": 3}.get(last_alert_pattern, 0)
                direction_changed = obstacle_active and side != last_alert_side
                escalated = obstacle_active and severity > previous_severity
                moved_closer = (
                    obstacle_active and distance_mm is not None and
                    last_alert_distance is not None and
                    distance_mm <= last_alert_distance - 300
                )
                urgent_reminder = (
                    obstacle_active and pattern == "rapid" and
                    (now - last_alert) >= 30.0
                )
                should_alert = not obstacle_active or direction_changed or escalated or moved_closer or urgent_reminder

                if should_alert:
                    if message:
                        threading.Thread(target=speak, args=(message,)).start()
                    threading.Thread(target=vibrate_pattern, args=(side, pattern)).start()
                    queue_event(event_type, {
                        "distance_mm": distance_mm,
                        "direction": "ahead" if side == "both" else side,
                        "message": message,
                    })
                    last_alert = now
                    last_alert_distance = distance_mm
                    last_alert_side = side
                    last_alert_pattern = pattern
                    obstacle_active = True
                    with state_lock:
                        state["last_alert_side"] = side
                        state["last_alert_zone"] = pattern
            else:
                # Require a stable clear reading before resetting. One noisy
                # sensor frame must not cause a repeat alert for the same person.
                if obstacle_active:
                    clear_since = clear_since or now
                    if now - clear_since >= 2.0:
                        queue_event("path_clear", {"message": "Path clear"})
                        speak("Path clear")
                        obstacle_active = False
                        last_alert_distance = None
                        last_alert_side = None
                        last_alert_pattern = None
                        with state_lock:
                            state["last_alert_side"] = None
                            state["last_alert_zone"] = None


        except Exception as e:
            print(f"[DETECT] Error: {e}")

        time.sleep(0.3)

# ══════════════════════════════════════════
#  VOICE COMMAND LOOP (main thread)
# ══════════════════════════════════════════
def voice_loop(picam2, stop_event):
    print("[MIC] Voice loop started.")
    print("[MIC] Commands: 'what is ahead', 'pause', 'resume', "
          "'photo', 'status', 'pairing code', 'help', 'stop'")

    r = sr.Recognizer()
    r.energy_threshold = 300
    r.dynamic_energy_threshold = True

    while not stop_event.is_set():
        try:
            with sr.Microphone() as source:
                r.adjust_for_ambient_noise(source, duration=0.5)
                audio = r.listen(source, timeout=4, phrase_time_limit=5)

            text = r.recognize_google(audio).lower()
            print(f"[MIC] Heard: '{text}'")

            if "what" in text or "ahead" in text or "around" in text:
                with state_lock:
                    scene = state["last_scene"]
                speak(f"Currently: {scene}", blocking=True)
                vibrate("both", 0.2, 60)
                queue_event("voice_command", {
                    "command": text,
                    "response": f"Currently: {scene}",
                })

            elif "pause" in text:
                with state_lock:
                    state["paused"] = True
                speak("Detection paused", blocking=True)

            elif "resume" in text or "start" in text:
                with state_lock:
                    state["paused"] = False
                speak("Detection resumed", blocking=True)

            elif "photo" in text or "capture" in text or "picture" in text:
                if picam2 is None:
                    speak("Camera is unavailable", blocking=True)
                else:
                    fname = f"photo_{int(time.time())}.jpg"
                    frame = picam2.capture_array()
                    frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                    cv2.imwrite(fname, frame_bgr)
                    speak("Photo saved", blocking=True)
                    print(f"[CAMERA] Saved {fname}")

            elif "status" in text or "check" in text:
                with state_lock:
                    fc = state["frame_count"]
                    scene = state["last_scene"]
                    paused = state["paused"]
                    tof_ok = state["tof_ok"]
                mode = "ToF sensors active" if tof_ok else "camera-only mode"
                status = "paused" if paused else "running"
                speak(
                    f"System {status}, {mode}. "
                    f"{fc} frames processed. {scene}",
                    blocking=True
                )
                queue_event("voice_command", {
                    "command": text,
                    "response": f"System {status}, {mode}. {scene}",
                })

            elif "pairing code" in text or text.strip() == "code":
                with state_lock:
                    code = state["pairing_code"]
                if code:
                    speak_pairing_code(code)
                else:
                    speak("A pairing code is not available", blocking=True)

            elif "help" in text or "commands" in text:
                speak(
                    "Commands: what is ahead, pause, resume, "
                    "photo, status, stop",
                    blocking=True
                )

            elif "stop" in text or "exit" in text or "shutdown" in text:
                speak("Shutting down Divya Drishti", blocking=True)
                vibrate_pattern("both", "double")
                stop_event.set()

            else:
                speak("Command not recognized")

        except sr.WaitTimeoutError:
            pass
        except sr.UnknownValueError:
            pass
        except sr.RequestError as e:
            print(f"[MIC] API error: {e}")
        except Exception as e:
            print(f"[MIC] Error: {e}")

# ══════════════════════════════════════════
#  STARTUP SEQUENCE
# ══════════════════════════════════════════
def startup_sequence():
    print("=" * 50)
    print("  DIVYA DRISHTI - PHASE 1 FULL SYSTEM")
    print("=" * 50)
    speak("Divya Drishti starting", blocking=False)
    time.sleep(0.3)
    vibrate("left",  0.2, 100)
    time.sleep(0.15)
    vibrate("right", 0.2, 100)
    time.sleep(0.15)
    vibrate("both",  0.4, 100)
    time.sleep(0.5)

# ══════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════
def main():
    startup_sequence()

    registered = register_device_if_needed()
    if sync_enabled() and not registered:
        speak("Cloud sync is unavailable. Continuing offline.")

    tof1, tof2, bus3, bus4 = init_tof_sensors()
    if tof1 is None:
        speak("ToF sensors unavailable. Using camera detection only.")
    else:
        speak("ToF sensors online.")

    picam2 = init_camera()
    if picam2 is not None:
        speak("Camera ready.")

    stop_event = threading.Event()
    start_local_link(stop_event)
    if sync_enabled():
        threading.Thread(target=sync_worker, args=(stop_event,), daemon=True).start()

    det_thread = threading.Thread(
        target=detection_loop,
        args=(picam2, tof1, tof2, stop_event)
    )
    det_thread.daemon = True
    det_thread.start()

    speak("All systems active. Listening for commands.")
    vibrate("both", 0.3, 70)

    try:
        voice_loop(picam2, stop_event)
    except KeyboardInterrupt:
        print("\n[SYSTEM] Ctrl+C received, shutting down...")
        stop_event.set()

    # cleanup
    if tof1:
        try:
            tof1.stop_ranging()
            tof2.stop_ranging()
            bus3.close()
            bus4.close()
        except Exception:
            pass
    if picam2 is not None:
        picam2.stop()
    GPIO.cleanup()

    print("=" * 50)
    print("  SHUTDOWN COMPLETE")
    print("=" * 50)

if __name__ == "__main__":
    if "--link-only" in sys.argv:
        try:
            saved = json.loads(DEVICE_FILE.read_text())
            with state_lock:
                state["device_id"] = saved.get("device_id")
                state["pairing_code"] = saved.get("pairing_code")
        except (FileNotFoundError, json.JSONDecodeError):
            print("[LOCAL] Device is not registered yet; link-only mode cannot start.")
            sys.exit(1)
        link_stop_event = threading.Event()
        start_local_link(link_stop_event)
        print("[LOCAL] Link-only mode active. Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            link_stop_event.set()
    else:
        main()





