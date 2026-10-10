"""
라즈베리파이 LCD 대시보드 (오디오 프로그램 main.py와 별도로 실행)
    - EQ, 볼륨, 현재 사용자: MQTT eq/state (main.py가 보냄)
    - 카메라 영상: 젯슨 MJPEG (http://젯슨IP:5000/video)
    - 등록 모드, 얼굴 상태, 인식된 사용자: MQTT face/mode, face/state, face/user_id (젯슨이 보냄)
    젯슨 쪽이 아직 없어도 EQ 부분은 동작하고, 영상 자리에는 "카메라 연결 대기"가 표시됨

설치:  sudo apt install python3-pygame python3-paho-mqtt fonts-nanum
실행:  /usr/bin/python3 dashboard.py        (ESC로 종료)
"""
import io
import json
import os
import re
import threading
import time
import urllib.request

# LCD 화면(Wayland) 지정: SSH로 실행해도 LCD에 뜨도록
# setdefault는 이미 값이 있으면(LCD 터미널에서 직접 실행한 경우) 그대로 둠
os.environ.setdefault("WAYLAND_DISPLAY", "wayland-0")
os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
os.nice(10)     # 실행 우선순위 낮추기 (오디오 프로그램이 먼저 처리되도록). nice -n 10과 같은 효과

import pygame
from pygame import gfxdraw          # 가장자리를 부드럽게 그리는 원 그리기용
import paho.mqtt.client as mqtt

# ---------------- 설정값 ----------------
MQTT_HOST = "localhost"                     # 브로커가 이 라즈베리파이에 있음
VIDEO_URL = "http://10.10.16.109:5000/video"       # 젯슨 MJPEG 주소 (영상이 없으면 None)
#VIDEO_URL = None       # 젯슨 MJPEG 주소 (영상이 없으면 None)
FONT_PATH = "/usr/share/fonts/truetype/nanum/NanumGothic.ttf"        # 한글 글꼴
FONT_BOLD_PATH = "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf"  # 한글 굵은 글꼴
FPS = 15                                    # 화면 갱신 횟수 (1초당)
VIDEO_W, VIDEO_H = 480, 600                 # 카메라 영상 영역 크기 (화면이 이보다 작으면 같은 비율로 줄임)

GAIN_MIN, GAIN_MAX = -12.0, 12.0            # audio.py와 같은 범위
VOL_MIN, VOL_MAX = -60.0, 0.0
BANDS = [("low", "저음"), ("mid", "중음"), ("high", "고음")]

# 등록 모드일 때 얼굴 상태별 안내 문구 (젯슨이 보내는 face/state 값 -> 화면 문구)
# 문장 끝의 ". " 기준으로 줄을 나눠서 표시함
FACE_MESSAGES = {
    "ready":     "얼굴이 잘 인식되고 있어요. 지금 상태를 유지해주세요.",
    "no_face":   "얼굴이 보이지 않아요. 화면 안으로 들어와 주세요.",
    "too_small": "얼굴이 너무 작아요. 화면에 가까이 와 주세요.",
    "clipped":   "얼굴을 화면 가운데에 맞춰 주세요.",
    "unclear":   "화면이 흐려 얼굴을 인식할 수 없어요.",
    "already_registered": "이미 등록된 사용자입니다.",
}
REGISTER_TITLE = "얼굴 등록중..."            # 등록 모드일 때 영상 위 빈 공간에 표시
NO_VIDEO_RATIO = 3 / 4                      # 영상이 없을 때 자리 표시 영역의 세로/가로 비율 (4:3)

# 색 (R, G, B) - 오래된 하이파이 오디오 앞판 느낌의 베이지 계열
BG = (188, 204, 255)        # 배경: 베이지 앞판
PANEL = (226, 214, 194)     # 영상 자리, 슬라이더 판: 한 단계 진한 베이지
SHADOW = (205, 190, 166)    # 손잡이 그림자
SLOT = (70, 56, 45)         # 슬라이더 홈: 짙은 갈색
TICK = (176, 160, 138)      # 눈금
KNOB = (43, 91, 249)        # 손잡이
TEXT = (46, 38, 31)         # 글자: 짙은 갈색
SUB = (140, 125, 106)       # 보조 글자: 회갈색
ACCENT = (111, 145, 255)    # 강조
VOL_KNOB = (94, 52, 186)    # 볼륨 손잡이: 짙은 보라
VOL_ACCENT = (176, 146, 255)  # 볼륨 강조: 연보라
WARN = (164, 74, 63)        # 음소거, 주의: 벽돌색
MSG_BG = (74, 59, 46)       # 영상 아래 안내 문구 띠: 짙은 갈색
MSG_OK = (190, 214, 170)    # 안내 띠 위 정상 문구: 밝은 초록
MSG_WARN = (240, 190, 150)  # 안내 띠 위 주의 문구: 밝은 테라코타

# ---------------- 다른 스레드가 채우는 최신 값 ----------------
lock = threading.Lock()
latest = {
    "eq": None,           # eq/state 메시지 (dict)
    "mode": None,         # face/mode 메시지 (dict)
    "face": None,         # face/state 값 (문자열, 예: "ready")
    "user_name": None,    # face/user_id의 이름
    "jpg": None,          # 가장 최근 영상 한 장 (JPEG 바이트)
    "jpg_new": False,     # 새 영상이 왔는지
    "video_ok": False,    # 영상 연결 상태
}


# ---------------- MQTT 수신 (별도 스레드) ----------------
def on_connect(client, userdata, flags, *args):
    # paho-mqtt 1.x, 2.x 모두에서 동작하도록 나머지 인자는 *args로 받음
    client.subscribe([("eq/state", 1), ("face/mode", 1), ("face/state", 1), ("face/user_id", 1)])


def on_message(client, userdata, msg):
    try:
        text = msg.payload.decode()
        with lock:
            if msg.topic == "eq/state":
                latest["eq"] = json.loads(text)
            elif msg.topic == "face/mode":
                latest["mode"] = json.loads(text)             # {"mode": "registration" 또는 "recognize"}
            elif msg.topic == "face/state":
                face_state = json.loads(text).get("state")
                if face_state and face_state != "photo_captured":   # 촬영 완료 메시지는 무시
                    latest["face"] = face_state
            elif msg.topic == "face/user_id":
                latest["user_name"] = json.loads(text).get("name")
    except (ValueError, UnicodeDecodeError) as e:
        print(f"MQTT 메시지 형식 오류: {msg.topic} {msg.payload!r} ({e})")


def start_mqtt():
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)   # paho-mqtt 2.x
    except AttributeError:
        client = mqtt.Client()                                   # paho-mqtt 1.x
    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=5)
    client.connect_async(MQTT_HOST, 1883)
    client.loop_start()
    return client


# ---------------- MJPEG 영상 수신 (별도 스레드) ----------------
def video_loop():
    """젯슨 영상을 계속 받아 '가장 최근 한 장'만 보관. 끊기면 2초 뒤 재연결"""
    while True:
        try:
            with urllib.request.urlopen(VIDEO_URL, timeout=5) as stream:
                while True:
                    length = None
                    while True:                                # 머리글에서 사진 크기 찾기
                        line = stream.readline()
                        if not line:
                            raise ConnectionError("영상 연결 끊김")
                        line = line.strip()
                        if line.lower().startswith(b"content-length:"):
                            length = int(line.split(b":")[1])
                        elif line == b"" and length is not None:
                            break
                    jpg = stream.read(length)
                    with lock:
                        latest["jpg"] = jpg                    # 이전 사진은 버리고 최신 한 장만
                        latest["jpg_new"] = True
                        latest["video_ok"] = True
        except Exception as e:
            with lock:
                latest["video_ok"] = False
            print(f"영상 연결 안 됨 ({e}). 2초 뒤 다시 시도")
            time.sleep(2)


# ---------------- 화면 그리기 ----------------
def load_font(size, path=FONT_PATH):
    try:
        return pygame.font.Font(path, size)
    except (FileNotFoundError, OSError):
        return pygame.font.Font(None, size)     # 한글 글꼴이 없으면 기본 글꼴 (한글이 네모로 보임)


def draw_text(screen, font, text, color, pos, center=False):
    surf = font.render(text, True, color)
    rect = surf.get_rect(center=pos) if center else surf.get_rect(topleft=pos)
    screen.blit(surf, rect)


def to_percent(value, vmin, vmax):
    """vmin~vmax 범위의 값을 0~100% 정수로 바꿈"""
    ratio = (min(max(value, vmin), vmax) - vmin) / (vmax - vmin)
    return round(ratio * 100)


def aa_circle(screen, color, center, r):
    """가장자리가 부드러운 꽉 찬 원"""
    x, y = int(center[0]), int(center[1])
    gfxdraw.filled_circle(screen, x, y, r, color)
    gfxdraw.aacircle(screen, x, y, r, color)


def draw_slider(screen, fonts, rect, label, value, vmin, vmax, value_text,
                ticks, zero=None, active=True, knob_color=KNOB, accent_color=ACCENT):
    """세로 슬라이더 하나: 짙은 홈 위에서 손잡이가 값에 따라 위아래로 움직임
    ticks: 눈금을 그릴 값 목록, zero: 채움 시작 기준값 (EQ는 0, 볼륨은 None=맨 아래)
    active=False면 손잡이와 채움을 흐리게 (음소거용)"""
    x, y, w, h = rect
    cx = x + w // 2
    vh = fonts["v"].get_height()
    lh = fonts["s"].get_height()
    rail_top, rail_bottom = y + vh + 22, y + h - lh - 20
    knob_r = max(11, min(w // 5, 18))

    def to_y(v):
        ratio = (min(max(v, vmin), vmax) - vmin) / (vmax - vmin)
        return int(rail_bottom - ratio * (rail_bottom - rail_top))

    # 눈금: 기준값은 길게
    for t in ticks:
        ty = to_y(t)
        length = 12 if t == zero else 7
        pygame.draw.line(screen, TICK, (cx - 10 - length, ty), (cx - 10, ty), 2)
        pygame.draw.line(screen, TICK, (cx + 10, ty), (cx + 10 + length, ty), 2)

    # 홈
    pygame.draw.rect(screen, SLOT, (cx - 3, rail_top - 4, 6, rail_bottom - rail_top + 8), border_radius=3)

    # 기준값(EQ는 0dB, 볼륨은 맨 아래)에서 손잡이까지 색으로 채움
    knob_y = to_y(value)
    base_y = to_y(zero) if zero is not None else rail_bottom + 4
    fill_color = accent_color if active else SUB
    top, bottom = min(base_y, knob_y), max(base_y, knob_y)
    if bottom - top > 0:
        pygame.draw.rect(screen, fill_color, (cx - 3, top, 6, bottom - top), border_radius=3)

    # 손잡이: 그림자 -> 테두리 -> 상아색 몸체 -> 가운데 점
    aa_circle(screen, SHADOW, (cx, knob_y + 3), knob_r)
    aa_circle(screen, fill_color, (cx, knob_y), knob_r)
    aa_circle(screen, knob_color, (cx, knob_y), knob_r - 3)
    aa_circle(screen, fill_color, (cx, knob_y), max(2, knob_r // 4))

    draw_text(screen, fonts["v"], value_text, TEXT if active else WARN, (cx, y + vh // 2), center=True)
    draw_text(screen, fonts["s"], label, SUB, (cx, y + h - lh // 2), center=True)


def draw_message_band(screen, fonts, rect, message, color):
    """영상 아래쪽에 반투명 띠를 깔고 안내 문구를 표시. 문장마다 줄을 나눔"""
    lines = re.split(r"(?<=\.)\s+", message.strip())
    font = fonts["msg"]
    if any(font.size(line)[0] > rect.w - 24 for line in lines):
        font = fonts["s"]                       # 영상 폭보다 길면 작은 글꼴로
    line_h = font.get_linesize()
    band_h = line_h * len(lines) + 20
    band = pygame.Surface((rect.w, band_h), pygame.SRCALPHA)
    band.fill((*MSG_BG, 215))                   # 215/255 불투명도: 뒤 영상이 살짝 비침
    screen.blit(band, (rect.x, rect.bottom - band_h))
    for i, line in enumerate(lines):
        y = rect.bottom - band_h + 10 + line_h * i + line_h // 2
        draw_text(screen, font, line, color, (rect.centerx, y), center=True)


def draw(screen, fonts, video_surface, state):
    W, H = screen.get_size()
    screen.fill(BG)
    pad = int(H * 0.04)

    # 오른쪽 슬라이더 판의 세로 중심 (영상 중심을 여기에 맞춤)
    panel_top = pad + fonts["s"].get_height() + fonts["l"].get_height() + 22
    panel_center_y = (panel_top + H - pad) // 2

    # ---- 왼쪽: 카메라 영상 (최대 VIDEO_W x VIDEO_H, 왼쪽에 pad만큼 여백) ----
    box_scale = min(1.0, H / VIDEO_H, (W * 0.6) / VIDEO_W)             # 화면이 작으면 비율 유지하며 줄임
    vw, vh = int(VIDEO_W * box_scale), int(VIDEO_H * box_scale)
    has_video = video_surface is not None and state["video_ok"]
    if has_video:
        iw, ih = video_surface.get_size()
        scale = min(vw / iw, vh / ih)                                    # 영상 전체가 보이도록 맞춤
        dw, dh = int(iw * scale), int(ih * scale)
    else:
        dw, dh = vw, int(vw * NO_VIDEO_RATIO)
    vid_rect = pygame.Rect(pad, 0, dw, dh)
    vid_rect.centery = panel_center_y                                     # 영상 중심 = 슬라이더 판 중심
    vid_rect.top = max(0, vid_rect.top)
    vid_rect.bottom = min(H, vid_rect.bottom)

    if has_video:
        img = pygame.transform.smoothscale(video_surface, (dw, dh))
        screen.blit(img, vid_rect)
    else:
        pygame.draw.rect(screen, PANEL, vid_rect)
        draw_text(screen, fonts["m"], "카메라 연결 대기", SUB, vid_rect.center, center=True)

    # ---- 안내 문구 ----
    eq = state["eq"]
    if (state["mode"] or {}).get("mode") == "registration":
        # 등록 모드: 영상 위 빈 공간에 제목, 영상 아래쪽에 얼굴 상태 안내
        draw_text(screen, fonts["t"], REGISTER_TITLE, TEXT, (vid_rect.centerx, vid_rect.top // 2), center=True)
        face = state["face"] if state["face"] in FACE_MESSAGES else "no_face"
        draw_message_band(screen, fonts, vid_rect, FACE_MESSAGES[face],
                          MSG_OK if face == "ready" else MSG_WARN)
    #else:
        # 인식 모드: 인식된 사용자에게 인사
        #greet_name = state["user_name"] or (eq or {}).get("name")
        #if greet_name:
        #    draw_message_band(screen, fonts, vid_rect, f"안녕하세요 {greet_name}님", MSG_OK)

    # ---- 오른쪽: 사용자, EQ, 볼륨 ----
    px = vid_rect.right + pad
    pw = W - px - pad
    name = (eq or {}).get("name")
    draw_text(screen, fonts["s"], "현재 사용자", SUB, (px, pad))
    draw_text(screen, fonts["l"], name if name else "인식 전", TEXT if name else SUB,
              (px, pad + fonts["s"].get_height() + 4))

    if eq is None:
        draw_text(screen, fonts["s"], "EQ 정보 대기 중", SUB, (px, H // 2))
        return

    # 슬라이더 판: EQ 3개 + 구분선 + 볼륨 1개 (panel_top은 위에서 계산)
    panel = pygame.Rect(px, panel_top, pw, H - panel_top - pad)
    pygame.draw.rect(screen, PANEL, panel, border_radius=14)
    inner = panel.inflate(-16, -28)
    col_w = inner.w // (len(BANDS) + 1)

    eq_ticks = [-12, -6, 0, 6, 12]
    for i, (key, label) in enumerate(BANDS):
        value = float(eq.get(key, 0.0))
        draw_slider(screen, fonts, (inner.x + i * col_w, inner.y, col_w, inner.h), label,
                    value, GAIN_MIN, GAIN_MAX, "0 dB" if round(value) == 0 else f"{value:+.0f} dB",
                    eq_ticks, zero=0)

    # EQ와 볼륨 사이 구분선
    div_x = inner.x + len(BANDS) * col_w
    pygame.draw.line(screen, TICK, (div_x, inner.y + 10), (div_x, inner.bottom - 10), 1)

    vol = float(eq.get("vol", VOL_MIN))
    muted = bool(eq.get("muted"))
    draw_slider(screen, fonts, (div_x, inner.y, col_w, inner.h), "볼륨",
                vol, VOL_MIN, VOL_MAX, "음소거" if muted else f"{to_percent(vol, VOL_MIN, VOL_MAX)}%",
                [-60, -45, -30, -15, 0], zero=None, active=not muted,
                knob_color=VOL_KNOB, accent_color=VOL_ACCENT)


# ---------------- 메인 ----------------
def main():
    # pygame.init()은 소리 기능까지 켜서 MAX98357A를 차지할 수 있음 -> 화면과 글꼴만 켬
    pygame.display.init()
    pygame.font.init()
    screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)   # LCD 해상도에 맞춰 전체 화면
    pygame.mouse.set_visible(False)
    H = screen.get_height()
    try:
        open(FONT_PATH).close()
    except OSError:
        print("한글 글꼴이 없어 기본 글꼴 사용 (한글이 네모로 보임): sudo apt install fonts-nanum")
    fonts = {"s": load_font(max(14, H // 28)), "m": load_font(max(18, H // 20)),
             "v": load_font(max(16, H // 22), FONT_BOLD_PATH),    # 슬라이더 값 숫자
             "l": load_font(max(24, H // 12), FONT_BOLD_PATH),    # 사용자 이름
             "t": load_font(max(20, H // 16), FONT_BOLD_PATH),    # "얼굴 등록중..." 제목
             "msg": load_font(max(16, H // 24))}                   # 영상 아래 안내 문구

    client = start_mqtt()
    if VIDEO_URL:
        threading.Thread(target=video_loop, daemon=True).start()

    clock = pygame.time.Clock()
    video_surface = None
    try:
        while True:
            for event in pygame.event.get():
                if event.type == pygame.QUIT or (event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE):
                    return

            with lock:
                state = dict(latest)            # 이 순간의 값 복사 (그리는 동안 다른 스레드가 바꿔도 안전)
                latest["jpg_new"] = False
            if state["jpg_new"] and state["jpg"]:
                try:
                    video_surface = pygame.image.load(io.BytesIO(state["jpg"]), "frame.jpg")
                except pygame.error:
                    pass                        # 깨진 사진 한 장은 건너뜀

            draw(screen, fonts, video_surface, state)
            pygame.display.flip()
            clock.tick(FPS)                     # 1초에 FPS번만 그리기 (CPU 사용 제한)
    finally:
        client.loop_stop()
        pygame.quit()


if __name__ == "__main__":
    main()
