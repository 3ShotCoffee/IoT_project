"""
STM32 엔코더 + 젯슨 얼굴 인식 연동 실시간 EQ 재생 (scipy 버전, 라즈베리파이 + MAX98357A용)
- STM32가 USB 시리얼로 보내는 "B1:+1" 메시지를 받아 EQ와 볼륨을 조절
  (B1=저음, B2=중음, B3=고음, B4=볼륨 / 한 칸 = STEP_DB 만큼)
- 젯슨이 MQTT(face/user_id)로 보내는 {"user_id": 1, "name": "user_01"}을 받아
  사용자가 바뀌면 DB에서 그 사람의 EQ를 불러와 적용 (DB에 없으면 기본 EQ)
- 노브(또는 키보드)로 EQ를 바꾸고 SAVE_DELAY초 동안 추가 조절이 없으면
  현재 사용자의 EQ를 DB에 저장 (DB에 없던 사용자는 새로 추가됨)
- 키보드 입력도 그대로 사용 가능 (테스트용)
- 재생 중 터미널에 "low 3", "mid -2", "high 6" 처럼 입력하면 해당 밴드가 바뀜
- "vol -3", "vol 3" 처럼 입력하면 전체 볼륨이 dB 단위로 바뀜, "mute"로 음소거/해제

준비:
    sudo apt install ffmpeg
    ffmpeg -i song.mp3 -ar 48000 song.wav      # MP3를 48000Hz WAV로 변환
"""
import json
import re
import threading
import time

import serial                       # pip install pyserial
import paho.mqtt.client as mqtt     # pip install paho-mqtt

import numpy as np
import sounddevice as sd
from scipy.io import wavfile
from scipy.signal import sosfilt

from db import load_eq, save_eq     # 같은 폴더의 db.py

# ---------------- 설정값 ----------------
SONG = "song.wav"
DEVICE = "MAX98357A"     # 출력 장치 이름 (aplay -l 에 나온 이름의 일부면 됨)
BLOCK = 1024             # 한 번에 처리할 샘플 수
GAIN_MIN, GAIN_MAX = -12.0, 12.0   # EQ 밴드 조절 범위 (dB)
VOL_START_DB = -20.0                # 시작 볼륨 (dB). -6dB = 소리 크기 약 절반 (스피커 보호용)
VOL_MIN_DB, VOL_MAX_DB = -60.0, 0.0  # 볼륨 조절 범위 (dB). 0dB = 원래 크기 (그 이상은 올리지 않음)

# ---------------- STM32 시리얼 설정 ----------------
SERIAL_PORT = "/dev/ttyACM0"   # Nucleo를 USB로 연결하면 보통 이 이름 (ls /dev/ttyACM* 로 확인)
SERIAL_BAUD = 115200           # STM32 USART2 설정과 같아야 함
STEP_DB = 1.0                  # 엔코더 한 칸당 바뀌는 양 (dB)
ENCODER_MAP = {1: "low", 2: "mid", 3: "high", 4: "vol"}   # 엔코더 번호 -> 조절 대상

# ---------------- MQTT 설정 (젯슨 -> 파이) ----------------
MQTT_HOST = "localhost"        # 브로커(Mosquitto)가 이 라즈베리파이에 있으면 localhost
MQTT_PORT = 1883
MQTT_TOPIC = "face/user_id"    # 젯슨이 사용자 정보를 올리는 토픽

# 조절이 멈춘 뒤 몇 초 후에 저장할지 (노브를 돌리는 도중에 매번 저장하지 않도록)
SAVE_DELAY = 1

# DB에 없는 사용자에게 적용할 기본 EQ
DEFAULT_EQ = {"low": 0.0, "mid": 0.0, "high": 0.0, "vol": VOL_START_DB}

# 밴드 설정: (이름, 필터 종류, 기준 주파수 Hz, Q)
# 엔코더 4개를 모두 EQ로 쓰려면 여기에 한 줄 추가하면 됨
BANDS = [
    ("low",  "lowshelf",  200,  0.707),
    ("mid",  "peak",      1000, 1.0),
    ("high", "highshelf", 4000, 0.707),
]


# ---------------- 1) 필터 계수 계산 (RBJ Audio EQ Cookbook 공식) ----------------
def biquad(kind, f0, q, gain_db, fs):
    """필터 하나의 계수를 sosfilt 형식 [b0, b1, b2, 1, a1, a2]로 돌려줌"""
    A = 10 ** (gain_db / 40)
    w0 = 2 * np.pi * f0 / fs
    cosw, sinw = np.cos(w0), np.sin(w0)
    alpha = sinw / (2 * q)
    sqA = np.sqrt(A)

    if kind == "peak":
        b0, b1, b2 = 1 + alpha * A, -2 * cosw, 1 - alpha * A
        a0, a1, a2 = 1 + alpha / A, -2 * cosw, 1 - alpha / A
    elif kind == "lowshelf":
        b0 = A * ((A + 1) - (A - 1) * cosw + 2 * sqA * alpha)
        b1 = 2 * A * ((A - 1) - (A + 1) * cosw)
        b2 = A * ((A + 1) - (A - 1) * cosw - 2 * sqA * alpha)
        a0 = (A + 1) + (A - 1) * cosw + 2 * sqA * alpha
        a1 = -2 * ((A - 1) + (A + 1) * cosw)
        a2 = (A + 1) + (A - 1) * cosw - 2 * sqA * alpha
    elif kind == "highshelf":
        b0 = A * ((A + 1) + (A - 1) * cosw + 2 * sqA * alpha)
        b1 = -2 * A * ((A - 1) + (A + 1) * cosw)
        b2 = A * ((A + 1) + (A - 1) * cosw - 2 * sqA * alpha)
        a0 = (A + 1) - (A - 1) * cosw + 2 * sqA * alpha
        a1 = 2 * ((A - 1) - (A + 1) * cosw)
        a2 = (A + 1) - (A - 1) * cosw - 2 * sqA * alpha
    else:
        raise ValueError(kind)

    return np.array([b0, b1, b2, a0, a1, a2]) / a0


def build_sos(gains, fs):
    """모든 밴드의 계수를 한 배열로 묶음 (밴드 수 x 6)"""
    return np.array([biquad(kind, f0, q, gains[name], fs)
                     for name, kind, f0, q in BANDS])


# ---------------- 2) WAV 읽기 ----------------
sr, data = wavfile.read(SONG)
if data.dtype == np.int16:
    audio = data.astype(np.float32) / 32768.0
elif data.dtype == np.int32:
    audio = data.astype(np.float32) / 2147483648.0
else:
    audio = data.astype(np.float32)
if audio.ndim == 1:                      # 모노 파일이면 (샘플 수, 1) 모양으로
    audio = audio[:, None]
channels = audio.shape[1]                # sounddevice와 같은 (샘플 수, 채널 수) 모양
print(f"곡 정보: {sr}Hz, {channels}채널, {audio.shape[0] / sr:.1f}초")

# ---------------- 3) EQ 상태 ----------------
gains = {name: 0.0 for name, _, _, _ in BANDS}
sos = build_sos(gains, sr)                         # 현재 필터 계수
zi = np.zeros((len(BANDS), 2, channels))           # 필터 상태 (블록 사이에 이어받음)
pos = 0
finished = threading.Event()

# ---------------- 볼륨 상태 ----------------
vol_db = VOL_START_DB          # 사용자가 정한 볼륨 (dB)
muted = False


def db_to_linear(db):
    """dB 값을 소리 크기 배율로 바꿈 (0dB=1.0배, -6dB=약 0.5배, -20dB=0.1배)"""
    return 10 ** (db / 20)


target_vol = db_to_linear(vol_db)   # 콜백이 따라갈 목표 배율
current_vol = target_vol            # 콜백이 지금 쓰고 있는 배율

# ---------------- 사용자 상태 ----------------
current_user_id = None         # 지금 적용 중인 사용자 번호 (처음엔 아무도 없음)
current_user_name = None       # 지금 적용 중인 사용자 이름 (DB에 새로 추가할 때 사용)

# ---------------- 저장 대기 상태 ----------------
pending_save = False           # 저장 안 된 변경이 있는지
last_change_time = 0.0         # 마지막으로 EQ를 바꾼 시각 (초)


# ---------------- 4) 오디오 장치가 다음 블록을 달라고 할 때마다 호출 ----------------
def callback(outdata, frames, time_info, status):
    global pos, zi, current_vol
    if status:
        print("오디오 경고:", status)

    chunk = audio[pos:pos + frames]
    pos += frames
    n = chunk.shape[0]
    outdata.fill(0)
    if n == 0:
        raise sd.CallbackStop

    # zi로 이전 블록의 상태를 넘기고, 이번 블록이 끝난 상태를 다시 받아둠
    # -> 블록 경계에서 소리가 튀지 않음
    y, zi = sosfilt(sos, chunk, axis=0, zi=zi)

    # 볼륨이 바뀌었으면 이번 블록 안에서 이전 값 -> 새 값으로 서서히 바꿈
    # (한 번에 바꾸면 "툭" 소리가 날 수 있음)
    new_vol = target_vol
    ramp = np.linspace(current_vol, new_vol, n, dtype=np.float32)[:, None]
    current_vol = new_vol

    outdata[:n] = np.clip(y * ramp, -1.0, 1.0).astype(np.float32)
    if n < frames:
        raise sd.CallbackStop


state_lock = threading.Lock()   # 키보드, 시리얼, MQTT 세 곳에서 동시에 값을 바꾸지 않도록
save_lock = threading.Lock()    # 저장과 사용자 변경이 동시에 일어나지 않도록


def _mark_changed():
    """노브·키보드로 값이 바뀌었음을 기록 (state_lock 안에서 호출)"""
    global pending_save, last_change_time
    pending_save = True
    last_change_time = time.monotonic()


def set_gain(band, delta):
    with state_lock:
        _set_gain(band, delta)


def _set_gain(band, delta):
    global sos
    gains[band] = float(np.clip(gains[band] + delta, GAIN_MIN, GAIN_MAX))
    # 새 계수를 다 만든 뒤 한 번에 교체 (콜백이 반쯤 바뀐 값을 읽지 않도록)
    # 필터 상태(zi)는 그대로 두어 소리가 끊기지 않게 함
    sos = build_sos(gains, sr)
    _mark_changed()
    print(f"{band}: {gains[band]:+.1f} dB")


def update_target_vol():
    global target_vol
    target_vol = 0.0 if muted else db_to_linear(vol_db)


def set_volume(delta):
    with state_lock:
        _set_volume(delta)


def _set_volume(delta):
    global vol_db
    vol_db = float(np.clip(vol_db + delta, VOL_MIN_DB, VOL_MAX_DB))
    update_target_vol()
    _mark_changed()
    print(f"volume: {vol_db:+.1f} dB" + (" (음소거 중)" if muted else ""))


def toggle_mute():
    global muted
    muted = not muted
    update_target_vol()
    print("음소거" if muted else f"음소거 해제 (volume: {vol_db:+.1f} dB)")


def apply_eq(eq):
    """EQ 전체를 정해진 값으로 한 번에 바꿈 (사용자가 바뀔 때 사용)
    set_gain/set_volume은 '현재 값 + 변화량'이고, 이 함수는 '이 값으로 설정'"""
    global sos, vol_db
    with state_lock:
        for name in gains:
            gains[name] = float(np.clip(eq.get(name, 0.0), GAIN_MIN, GAIN_MAX))
        sos = build_sos(gains, sr)            # 밴드를 다 바꾼 뒤 계수는 한 번만 계산
        vol_db = float(np.clip(eq.get("vol", VOL_START_DB), VOL_MIN_DB, VOL_MAX_DB))
        update_target_vol()
    eq_text = ", ".join(f"{k} {v:+.1f}" for k, v in gains.items())
    print(f"  EQ 적용: {eq_text}, volume {vol_db:+.1f} dB")


# ---------------- DB 저장 ----------------
def _save_current():
    """현재 사용자의 EQ를 DB에 저장 (save_lock 안에서 호출)"""
    global pending_save, last_change_time
    with state_lock:
        if not pending_save:
            return
        pending_save = False
        user_id, name = current_user_id, current_user_name
        eq = {**gains, "vol": vol_db}             # 저장할 값을 이 순간 기준으로 복사

    if user_id is None:
        print("  인식된 사용자가 없어서 저장하지 않음")
        return
    try:
        save_eq(user_id, eq, name)
        print(f"  저장됨: 사용자 {user_id} ({name})")
    except Exception as e:                        # DB 접속 실패 등
        print(f"  저장 실패 ({e}). {SAVE_DELAY}초 뒤 다시 시도")
        with state_lock:
            pending_save = True
            last_change_time = time.monotonic()


def saver_loop():
    """별도 스레드에서 계속 실행: 마지막 조절 후 SAVE_DELAY초가 지나면 저장"""
    while not finished.wait(0.2):                 # 0.2초마다 확인, 재생이 끝나면 멈춤
        if pending_save and time.monotonic() - last_change_time >= SAVE_DELAY:
            with save_lock:
                _save_current()


# ---------------- STM32 시리얼 수신 ----------------
MSG_PATTERN = re.compile(r"B(\d+):([+-]?\d+)")   # "B1:+1" 또는 "B1:+1 (cnt=4)" 모두 인식


def handle_encoder(num, steps):
    target = ENCODER_MAP.get(num)
    if target is None:
        return
    if target == "vol":
        set_volume(steps * STEP_DB)
    else:
        set_gain(target, steps * STEP_DB)


def serial_listener():
    """별도 스레드에서 계속 실행: 시리얼 한 줄씩 읽어서 처리, 연결이 끊기면 1초 뒤 재연결"""
    while not finished.is_set():
        try:
            with serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=0.5) as ser:
                print(f"STM32 연결됨: {SERIAL_PORT}")
                while not finished.is_set():
                    line = ser.readline().decode(errors="ignore").strip()
                    if not line:
                        continue                      # 0.5초 동안 메시지 없음
                    m = MSG_PATTERN.match(line)
                    if m:
                        handle_encoder(int(m.group(1)), int(m.group(2)))
        except serial.SerialException as e:
            print(f"STM32 연결 안 됨 ({e}). 1초 뒤 다시 시도")
            time.sleep(1)


# ---------------- 젯슨 MQTT 수신 ----------------
def handle_user(user_id, name):
    """사용자 메시지 처리: 같으면 무시, 다르면 DB에서 불러와 적용, DB에 없으면 기본 EQ"""
    with save_lock:
        _handle_user(user_id, name)


def _handle_user(user_id, name):
    global current_user_id, current_user_name
    if user_id == current_user_id:
        return                                    # 같은 사람이면 아무것도 안 함

    # 이전 사용자가 조절하고 아직 저장 안 된 값이 있으면, 바꾸기 전에 이전 사용자에게 먼저 저장
    _save_current()

    print(f"사용자 변경: {current_user_id} -> {user_id} ({name})")
    try:
        eq = load_eq(user_id)
    except Exception as e:                        # DB 접속 실패 등
        print(f"  DB 조회 실패 ({e}). 기본 EQ 적용")
        eq = None

    if eq is None:
        print("  DB에 없는 사용자 -> 기본 EQ")
        eq = DEFAULT_EQ

    apply_eq(eq)
    with state_lock:
        current_user_id, current_user_name = user_id, name


def on_connect(client, userdata, flags, reason_code, properties):
    # 연결될 때마다(재연결 포함) 다시 구독해야 메시지를 계속 받음
    if reason_code == 0:
        print(f"MQTT 연결됨: {MQTT_HOST}:{MQTT_PORT}, 구독 토픽: {MQTT_TOPIC}")
        client.subscribe(MQTT_TOPIC, qos=1)
    else:
        print(f"MQTT 연결 실패: {reason_code}")


def on_message(client, userdata, msg):
    try:
        data = json.loads(msg.payload.decode())
        user_id = int(data["user_id"])
        name = str(data["name"])
    except (ValueError, KeyError, TypeError) as e:
        print(f"MQTT 메시지 형식 오류, 무시함: {msg.payload!r} ({e})")
        return
    handle_user(user_id, name)


mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message
mqtt_client.reconnect_delay_set(min_delay=1, max_delay=5)   # 끊기면 1~5초 간격으로 재연결
mqtt_client.connect_async(MQTT_HOST, MQTT_PORT)             # 브로커가 아직 안 켜져 있어도 멈추지 않음
mqtt_client.loop_start()                                    # MQTT 수신을 별도 스레드에서 실행

threading.Thread(target=serial_listener, daemon=True).start()
threading.Thread(target=saver_loop, daemon=True).start()


stream = sd.OutputStream(
    device=DEVICE, samplerate=sr, channels=channels, blocksize=BLOCK,
    dtype="float32", callback=callback, finished_callback=finished.set,
)

try:
    with stream:
        names = "/".join(gains)
        print("재생 시작. 예: 'low 6', 'mid -3', 'high 3', 'vol -3', 'mute' / 'q'로 종료")
        print(f"시작 볼륨: {vol_db:+.1f} dB")
        while not finished.is_set():
            cmd = input().split()
            if not cmd:
                continue
            if cmd[0] == "q":
                break
            if cmd[0] == "mute":
                toggle_mute()
                continue
            if len(cmd) == 2 and cmd[0] == "vol":
                try:
                    set_volume(float(cmd[1]))
                except ValueError:
                    print("숫자를 입력하세요. 예: vol -3")
                continue
            if len(cmd) == 2 and cmd[0] in gains:
                try:
                    set_gain(cmd[0], float(cmd[1]))
                except ValueError:
                    print("숫자를 입력하세요. 예: low 6")
            else:
                print(f"형식: {names} 숫자 / vol 숫자 / mute")
finally:
    finished.set()              # 다른 스레드들에게 종료 알림
    with save_lock:
        _save_current()         # 종료 직전에 저장 안 된 변경이 있으면 저장
    mqtt_client.loop_stop()     # 종료할 때 MQTT 스레드 정리
    mqtt_client.disconnect()