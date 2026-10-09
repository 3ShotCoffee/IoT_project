"""
라즈베리파이 LCD 대시보드 (오디오 프로그램 main.py와 별도로 실행)
    - EQ, 볼륨, 현재 사용자: MQTT eq/state (main.py가 보냄)
    - 카메라 영상: 젯슨 MJPEG (http://젯슨IP:5000/video)
    - 등록 모드, 얼굴 상태: MQTT face/mode, face/state (젯슨이 보냄)
    젯슨 쪽이 아직 없어도 EQ 부분은 동작하고, 영상 자리에는 "카메라 연결 대기"가 표시됨

설치:  sudo apt install python3-pygame python3-paho-mqtt fonts-nanum
실행:  /usr/bin/python3 dashboard.py        (ESC로 종료)
"""
import io
import json
import threading
import time
import urllib.request

import pygame
import paho.mqtt.client as mqtt

# ---------------- 설정값 ----------------
MQTT_HOST = "localhost"                     # 브로커가 이 라즈베리파이에 있음
VIDEO_URL = "http://젯슨IP:5000/video"       # 젯슨 MJPEG 주소 (영상이 없으면 None)
FONT_PATH = "/usr/share/fonts/truetype/nanum/NanumGothic.ttf"   # 한글 글꼴
FPS = 15                                    # 화면 갱신 횟수 (1초당)
RESULT_SHOW_SEC = 3                         # "등록 완료" 같은 결과 문구를 보여줄 시간

GAIN_MIN, GAIN_MAX = -12.0, 12.0            # audio.py와 같은 범위
VOL_MIN, VOL_MAX = -60.0, 0.0
BANDS = [("low", "저음"), ("mid", "중음"), ("high", "고음")]

# 등록 모드일 때 얼굴 상태별 안내 문구 (젯슨이 보내는 코드 -> 화면 문구)
FACE_MESSAGES = {
    "ok":      "좋아요! 버튼을 눌러 촬영하세요",
    "small":   "조금 더 가까이 와 주세요",
    "edge":    "얼굴을 화면 가운데에 맞춰 주세요",
    "unclear": "얼굴이 잘 보이지 않아요",
}
RESULT_MESSAGES = {"done": "사용자 등록이 완료됐어요", "cancelled": "등록을 취소했어요",
                   "failed": "등록에 실패했어요. 다시 시도해 주세요"}

# 색 (R, G, B) - 베이지 계열
BG = (243, 235, 221)        # 배경: 밝은 베이지
PANEL = (228, 216, 196)     # 영상 자리, 막대 바탕: 진한 베이지
TEXT = (62, 51, 40)         # 글자: 짙은 갈색
SUB = (138, 123, 104)       # 보조 글자: 회갈색
ACCENT = (122, 154, 107)    # EQ, 볼륨 막대와 정상 안내: 차분한 초록
WARN = (192, 112, 63)       # 주의 안내, 음소거: 테라코타
MSG_BG = (74, 59, 46)       # 영상 아래 안내 문구 띠: 짙은 갈색
MSG_OK = (190, 214, 170)    # 안내 띠 위 정상 문구: 밝은 초록
MSG_WARN = (240, 190, 150)  # 안내 띠 위 주의 문구: 밝은 테라코타

# ---------------- 다른 스레드가 채우는 최신 값 ----------------
lock = threading.Lock()
latest = {
    "eq": None,           # eq/state 메시지 (dict)
    "mode": None,         # face/mode 메시지 (dict)
    "face": None,         # face/state 코드 (문자열)
    "jpg": None,          # 가장 최근 영상 한 장 (JPEG 바이트)
    "jpg_new": False,     # 새 영상이 왔는지
    "video_ok": False,    # 영상 연결 상태
    "result_until": 0.0,  # 결과 문구를 언제까지 보여줄지
}


# ---------------- MQTT 수신 (별도 스레드) ----------------
def on_connect(client, userdata, flags, *args):
    # paho-mqtt 1.x, 2.x 모두에서 동작하도록 나머지 인자는 *args로 받음
    client.subscribe([("eq/state", 1), ("face/mode", 1), ("face/state", 1)])


def on_message(client, userdata, msg):
    try:
        text = msg.payload.decode()
        with lock:
            if msg.topic == "eq/state":
                latest["eq"] = json.loads(text)
            elif msg.topic == "face/mode":
                mode = json.loads(text)
                latest["mode"] = mode
                if mode.get("result"):                         # 등록이 막 끝났으면 결과 문구 표시 시작
                    latest["result_until"] = time.monotonic() + RESULT_SHOW_SEC
            elif msg.topic == "face/state":
                latest["face"] = text.strip().strip('"')       # "ok" 또는 ok 둘 다 허용
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
def load_font(size):
    try:
        return pygame.font.Font(FONT_PATH, size)
    except (FileNotFoundError, OSError):
        return pygame.font.Font(None, size)     # 한글 글꼴이 없으면 기본 글꼴 (한글이 네모로 보임)


def draw_text(screen, font, text, color, pos, center=False):
    surf = font.render(text, True, color)
    rect = surf.get_rect(center=pos) if center else surf.get_rect(topleft=pos)
    screen.blit(surf, rect)


def draw_eq_bar(screen, fonts, rect, label, value):
    """세로 막대 하나: 가운데가 0dB, 위로 올리면 위로, 내리면 아래로 채움"""
    x, y, w, h = rect
    bar_top, bar_bottom = y + fonts["s"].get_height() + 8, y + h - fonts["s"].get_height() - 8
    bar_x, bar_w = x + w // 2 - 14, 28
    mid = (bar_top + bar_bottom) // 2
    pygame.draw.rect(screen, PANEL, (bar_x, bar_top, bar_w, bar_bottom - bar_top), border_radius=6)
    ratio = max(-1.0, min(1.0, value / GAIN_MAX))
    fill = int(abs(ratio) * (bar_bottom - bar_top) / 2)
    if fill > 0:
        top = mid - fill if ratio > 0 else mid
        pygame.draw.rect(screen, ACCENT, (bar_x, top, bar_w, fill), border_radius=4)
    pygame.draw.line(screen, SUB, (bar_x - 6, mid), (bar_x + bar_w + 6, mid), 2)
    draw_text(screen, fonts["s"], f"{value:+.0f} dB", TEXT, (x + w // 2, y + fonts["s"].get_height() // 2), center=True)
    draw_text(screen, fonts["s"], label, SUB, (x + w // 2, y + h - fonts["s"].get_height() // 2), center=True)


def draw(screen, fonts, video_surface, state):
    W, H = screen.get_size()
    screen.fill(BG)
    pad = int(H * 0.04)

    # ---- 왼쪽: 카메라 영상 ----
    vid_rect = pygame.Rect(pad, pad, int(W * 0.58) - pad, H - 2 * pad)
    pygame.draw.rect(screen, PANEL, vid_rect, border_radius=12)
    if video_surface is not None and state["video_ok"]:
        iw, ih = video_surface.get_size()
        scale = min(vid_rect.w / iw, vid_rect.h / ih)                   # 비율 유지하며 맞추기
        img = pygame.transform.scale(video_surface, (int(iw * scale), int(ih * scale)))
        screen.blit(img, img.get_rect(center=vid_rect.center))
    else:
        draw_text(screen, fonts["m"], "카메라 연결 대기", SUB, vid_rect.center, center=True)

    # 등록 모드일 때만 영상 아래쪽에 안내 문구
    mode = state["mode"] or {}
    message, color = None, MSG_OK
    if time.monotonic() < state["result_until"] and mode.get("result"):
        message, color = RESULT_MESSAGES.get(mode["result"], ""), MSG_OK
    elif mode.get("mode") == "register":
        message = FACE_MESSAGES.get(state["face"], "카메라를 봐 주세요")
        color = MSG_OK if state["face"] == "ok" else MSG_WARN
        if mode.get("progress") is not None and mode.get("total"):
            message += f"  ({mode['progress']}/{mode['total']})"
    if message:
        box = pygame.Rect(vid_rect.x, vid_rect.bottom - fonts["m"].get_height() - 24,
                          vid_rect.w, fonts["m"].get_height() + 24)
        pygame.draw.rect(screen, MSG_BG, box, border_bottom_left_radius=12, border_bottom_right_radius=12)
        draw_text(screen, fonts["m"], message, color, box.center, center=True)

    # ---- 오른쪽: 사용자, EQ, 볼륨 ----
    px = vid_rect.right + pad
    pw = W - px - pad
    eq = state["eq"]
    name = (eq or {}).get("name")
    draw_text(screen, fonts["s"], "현재 사용자", SUB, (px, pad))
    draw_text(screen, fonts["l"], name if name else "인식 전", TEXT, (px, pad + fonts["s"].get_height() + 4))

    if eq is None:
        draw_text(screen, fonts["s"], "EQ 정보 대기 중", SUB, (px, H // 2))
        return

    bars_top = pad + fonts["s"].get_height() + fonts["l"].get_height() + 24
    vol_h = fonts["s"].get_height() * 2 + 30
    bars_h = H - bars_top - vol_h - 2 * pad
    col_w = pw // len(BANDS)
    for i, (key, label) in enumerate(BANDS):
        draw_eq_bar(screen, fonts, (px + i * col_w, bars_top, col_w, bars_h), label, float(eq.get(key, 0.0)))

    # 볼륨: 가로 막대
    vy = H - pad - vol_h
    vol = float(eq.get("vol", VOL_MIN))
    muted = bool(eq.get("muted"))
    vol_text = "음소거" if muted else f"볼륨 {vol:+.0f} dB"
    draw_text(screen, fonts["s"], vol_text, WARN if muted else TEXT, (px, vy))
    bar = pygame.Rect(px, vy + fonts["s"].get_height() + 10, pw, 20)
    pygame.draw.rect(screen, PANEL, bar, border_radius=10)
    ratio = 0.0 if muted else (vol - VOL_MIN) / (VOL_MAX - VOL_MIN)
    if ratio > 0:
        pygame.draw.rect(screen, ACCENT, (bar.x, bar.y, int(bar.w * ratio), bar.h), border_radius=10)


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
             "l": load_font(max(24, H // 12))}

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
