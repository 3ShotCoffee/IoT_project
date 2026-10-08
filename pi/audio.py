"""
오디오 관리: 음악 반복 재생, 실시간 EQ, 사용자별 EQ 불러오기·저장

다른 파일에서 쓰는 함수:
    start(shutdown)            재생 시작
    stop()                     저장 안 된 변경 저장 후 재생 정지
    adjust(target, steps)      노브 조절 (target: "low"/"mid"/"high"/"vol")
    handle_user(user_id, name) 사용자 변경 (DB에서 EQ 불러와 적용)
    toggle_mute()              음소거 켜기/끄기
"""
import threading
import time

import numpy as np
import sounddevice as sd
from scipy.io import wavfile
from scipy.signal import sosfilt

from db import load_eq, save_eq     # 같은 폴더의 db.py

# ---------------- 설정값 ----------------
SONG = "song.wav"
DEVICE = "MAX98357A"     # 출력 장치 이름 (aplay -l 에 나온 이름의 일부면 됨)
BLOCK = 1024             # 한 번에 처리할 샘플 수
GAIN_MIN, GAIN_MAX = -12.0, 12.0    # EQ 밴드 조절 범위 (dB)
VOL_START_DB = -20.0                # 시작 볼륨 (dB)
VOL_MIN_DB, VOL_MAX_DB = -60.0, 0.0  # 볼륨 조절 범위 (dB)
STEP_DB = 1.0            # 노브 한 칸당 바뀌는 양 (dB)
SAVE_DELAY = 1           # 조절이 멈춘 뒤 몇 초 후에 DB에 저장할지

# DB에 없는 사용자에게 적용할 기본 EQ
DEFAULT_EQ = {"low": 0.0, "mid": 0.0, "high": 0.0, "vol": VOL_START_DB}

# 밴드 설정: (이름, 필터 종류, 기준 주파수 Hz, Q)
BANDS = [
    ("low",  "lowshelf",  200,  0.707),
    ("mid",  "peak",      1000, 1.0),
    ("high", "highshelf", 4000, 0.707),
]


# ---------------- 필터 계수 계산 (RBJ Audio EQ Cookbook 공식) ----------------
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


def db_to_linear(db):
    """dB 값을 소리 크기 배율로 바꿈 (0dB=1.0배, -6dB=약 0.5배, -20dB=0.1배)"""
    return 10 ** (db / 20)


# ---------------- 상태 (start()에서 채움, 이 파일 안에서만 사용) ----------------
_song = None                 # 곡 전체 (샘플 수, 채널 수), -1.0~1.0
_sr = None                   # 샘플레이트
_pos = 0                     # 지금까지 재생한 위치
_gains = {name: 0.0 for name, _, _, _ in BANDS}
_sos = None                  # 현재 필터 계수
_zi = None                   # 필터 상태 (블록 사이에 이어받음)

_vol_db = VOL_START_DB       # 사용자가 정한 볼륨 (dB)
_muted = False
_target_vol = db_to_linear(VOL_START_DB)   # 콜백이 따라갈 목표 배율
_current_vol = _target_vol                 # 콜백이 지금 쓰고 있는 배율

_user_id = None              # 지금 적용 중인 사용자 번호
_user_name = None
_pending_save = False        # 저장 안 된 변경이 있는지
_last_change = 0.0           # 마지막으로 조절한 시각 (초)

_state_lock = threading.Lock()   # EQ, 볼륨 값을 여러 스레드가 동시에 바꾸지 않도록
_save_lock = threading.Lock()    # 저장과 사용자 변경이 동시에 일어나지 않도록
_stream = None


# ---------------- 오디오 장치가 다음 블록을 달라고 할 때마다 호출 ----------------
def _callback(outdata, frames, time_info, status):
    global _pos, _zi, _current_vol
    if status:
        print("오디오 경고:", status)

    # 곡 끝에 도달하면 남은 부분 + 처음 부분을 이어 붙여서 반복 재생
    chunk = _song[_pos:_pos + frames]
    if chunk.shape[0] < frames:
        _pos = frames - chunk.shape[0]
        chunk = np.concatenate([chunk, _song[:_pos]])
    else:
        _pos += frames

    # 이전 블록의 필터 상태를 이어받아 EQ 적용 (블록 경계에서 소리가 튀지 않음)
    y, _zi = sosfilt(_sos, chunk, axis=0, zi=_zi)

    # 볼륨이 바뀌었으면 이번 블록 안에서 서서히 바꿈 ("툭" 소리 방지)
    new_vol = _target_vol
    ramp = np.linspace(_current_vol, new_vol, frames, dtype=np.float32)[:, None]
    _current_vol = new_vol

    outdata[:] = np.clip(y * ramp, -1.0, 1.0).astype(np.float32)


# ---------------- EQ, 볼륨 조절 ----------------
def _update_target_vol():
    """state_lock 안에서 호출"""
    global _target_vol
    _target_vol = 0.0 if _muted else db_to_linear(_vol_db)


def adjust(target, steps):
    """노브 조절: target 밴드(또는 볼륨)를 steps칸만큼 바꿈"""
    global _sos, _vol_db, _pending_save, _last_change
    delta = steps * STEP_DB
    with _state_lock:
        if target == "vol":
            _vol_db = float(np.clip(_vol_db + delta, VOL_MIN_DB, VOL_MAX_DB))
            _update_target_vol()
            text = f"volume: {_vol_db:+.1f} dB" + (" (음소거 중)" if _muted else "")
        elif target in _gains:
            _gains[target] = float(np.clip(_gains[target] + delta, GAIN_MIN, GAIN_MAX))
            _sos = build_sos(_gains, _sr)     # 새 계수를 다 만든 뒤 한 번에 교체
            text = f"{target}: {_gains[target]:+.1f} dB"
        else:
            return
        _pending_save = True                  # 직접 조절한 값만 저장 대상
        _last_change = time.monotonic()
    print(text)


def toggle_mute():
    global _muted
    with _state_lock:
        _muted = not _muted
        _update_target_vol()
        text = "음소거" if _muted else f"음소거 해제 (volume: {_vol_db:+.1f} dB)"
    print(text)


def _apply_eq(eq):
    """EQ 전체를 정해진 값으로 한 번에 설정 (사용자가 바뀔 때 사용)"""
    global _sos, _vol_db
    with _state_lock:
        for name in _gains:
            _gains[name] = float(np.clip(eq.get(name, 0.0), GAIN_MIN, GAIN_MAX))
        _sos = build_sos(_gains, _sr)
        _vol_db = float(np.clip(eq.get("vol", VOL_START_DB), VOL_MIN_DB, VOL_MAX_DB))
        _update_target_vol()
        eq_text = ", ".join(f"{k} {v:+.1f}" for k, v in _gains.items())
        vol = _vol_db
    print(f"  EQ 적용: {eq_text}, volume {vol:+.1f} dB")


# ---------------- DB 저장 ----------------
def _save_current():
    """현재 사용자의 EQ를 DB에 저장 (save_lock 안에서 호출)"""
    global _pending_save, _last_change
    with _state_lock:
        if not _pending_save:
            return
        _pending_save = False
        user_id, name = _user_id, _user_name
        eq = {**_gains, "vol": _vol_db}       # 저장할 값을 이 순간 기준으로 복사

    if user_id is None:
        print("  인식된 사용자가 없어서 저장하지 않음")
        return
    try:
        save_eq(user_id, eq, name)
        print(f"  저장됨: 사용자 {user_id} ({name})")
    except Exception as e:                    # DB 접속 실패 등
        print(f"  저장 실패 ({e}). {SAVE_DELAY}초 뒤 다시 시도")
        with _state_lock:
            _pending_save = True
            _last_change = time.monotonic()


def _saver_loop(shutdown):
    """마지막 조절 후 SAVE_DELAY초가 지나면 저장 (별도 스레드)"""
    while not shutdown.wait(0.2):
        if _pending_save and time.monotonic() - _last_change >= SAVE_DELAY:
            with _save_lock:
                _save_current()


# ---------------- 사용자 변경 ----------------
def handle_user(user_id, name):
    """같은 사용자면 무시, 다르면 DB에서 불러와 적용, DB에 없으면 기본 EQ"""
    global _user_id, _user_name, _pending_save
    with _save_lock:
        if user_id == _user_id:
            return

        # 이전 사용자가 조절하고 아직 저장 안 된 값이 있으면 먼저 저장
        _save_current()

        print(f"사용자 변경: {_user_id} -> {user_id} ({name})")
        try:
            eq = load_eq(user_id)
        except Exception as e:                # DB 접속 실패 등
            print(f"  DB 조회 실패 ({e}). 기본 EQ 적용")
            eq = None
        if eq is None:
            print("  DB에 없는 사용자 -> 기본 EQ")
            eq = DEFAULT_EQ

        _apply_eq(eq)
        with _state_lock:
            _user_id, _user_name = user_id, name
            _pending_save = False             # 이전 사용자 저장이 실패했어도 새 사용자에게 섞이지 않도록


# ---------------- 시작, 정지 ----------------
def start(shutdown):
    """곡을 읽고 재생 시작, 저장 스레드 시작"""
    global _song, _sr, _sos, _zi, _stream

    sr, data = wavfile.read(SONG)
    if data.dtype == np.int16:
        song = data.astype(np.float32) / 32768.0
    elif data.dtype == np.int32:
        song = data.astype(np.float32) / 2147483648.0
    else:
        song = data.astype(np.float32)
    if song.ndim == 1:                        # 모노 파일이면 (샘플 수, 1) 모양으로
        song = song[:, None]
    channels = song.shape[1]
    print(f"곡 정보: {sr}Hz, {channels}채널, {song.shape[0] / sr:.1f}초")

    _song, _sr = song, sr
    _sos = build_sos(_gains, _sr)
    _zi = np.zeros((len(BANDS), 2, channels))

    threading.Thread(target=_saver_loop, args=(shutdown,), daemon=True).start()

    _stream = sd.OutputStream(device=DEVICE, samplerate=sr, channels=channels,
                              blocksize=BLOCK, dtype="float32", callback=_callback)
    _stream.start()
    print(f"재생 시작 (볼륨 {_vol_db:+.1f} dB)")


def stop():
    """저장 안 된 변경을 저장하고 재생 정지"""
    with _save_lock:
        _save_current()
    if _stream is not None:
        _stream.stop()
        _stream.close()
