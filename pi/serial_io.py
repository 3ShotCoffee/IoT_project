"""
시리얼 관리: STM32가 USB 시리얼로 보내는 메시지 읽기
    "B1:+1"  엔코더 1번을 한 칸 시계 방향으로 (B1=저음, B2=중음, B3=고음, B4=볼륨)
    "S1:1"   버튼 1번 눌림

다른 파일에서 쓰는 함수:
    start(shutdown, on_encoder, on_button)
        on_encoder(target, steps): 엔코더가 돌아갔을 때 호출 (target: "low"/"mid"/"high"/"vol")
        on_button(num): 버튼이 눌렸을 때 호출
"""
import re
import threading

import serial    # pip install pyserial

# ---------------- 설정값 ----------------
SERIAL_PORT = "/dev/ttyACM0"   # Nucleo를 USB로 연결하면 보통 이 이름 (ls /dev/ttyACM* 로 확인)
SERIAL_BAUD = 115200           # STM32 USART2 설정과 같아야 함
ENCODER_MAP = {1: "low", 2: "mid", 3: "high", 4: "vol"}   # 엔코더 번호 -> 조절 대상

ENCODER_PATTERN = re.compile(r"B(\d+):([+-]?\d+)")   # "B1:+1" 또는 "B1:+1 (cnt=4)"
BUTTON_PATTERN = re.compile(r"S(\d+):1")             # "S1:1"


def _handle_line(line, on_encoder, on_button):
    m = ENCODER_PATTERN.match(line)
    if m:
        target = ENCODER_MAP.get(int(m.group(1)))
        if target is not None:
            on_encoder(target, int(m.group(2)))
        return
    m = BUTTON_PATTERN.match(line)
    if m:
        on_button(int(m.group(1)))


def _listen(shutdown, on_encoder, on_button):
    """한 줄씩 읽어서 처리, 연결이 끊기면 1초 뒤 재연결"""
    while not shutdown.is_set():
        try:
            with serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=0.5) as ser:
                print(f"STM32 연결됨: {SERIAL_PORT}")
                while not shutdown.is_set():
                    line = ser.readline().decode(errors="ignore").strip()
                    if not line:
                        continue                      # 0.5초 동안 메시지 없음
                    try:
                        _handle_line(line, on_encoder, on_button)
                    except Exception as e:
                        # 처리 중 예상 못 한 오류가 나도 시리얼 스레드가 죽지 않도록
                        print(f"시리얼 메시지 처리 오류: {line!r} ({e})")
        except serial.SerialException as e:
            print(f"STM32 연결 안 됨 ({e}). 1초 뒤 다시 시도")
            shutdown.wait(1)


def start(shutdown, on_encoder, on_button):
    threading.Thread(target=_listen, args=(shutdown, on_encoder, on_button),
                     daemon=True).start()
