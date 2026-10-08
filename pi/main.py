"""
차량 오디오 EQ 시스템 실행 파일 (라즈베리파이)
    실행: python main.py
    종료: Ctrl+C (또는 systemd 서비스 정지)
"""
import signal
import threading

import audio
import mqtt_io
import serial_io


def main():
    shutdown = threading.Event()   # 모든 스레드가 함께 보는 종료 신호
    signal.signal(signal.SIGTERM, lambda *args: shutdown.set())   # 서비스 정지 명령도 정상 종료로 처리

    audio.start(shutdown)
    mqtt_io.start(on_user=audio.handle_user)
    serial_io.start(shutdown, on_encoder=audio.adjust, on_button=mqtt_io.publish_button)
    print("실행 중. Ctrl+C로 종료")

    try:
        while not shutdown.wait(0.5):
            pass
    except KeyboardInterrupt:
        print("\n종료 요청")
    finally:
        shutdown.set()
        audio.stop()        # 저장 안 된 변경 저장 후 재생 정지
        mqtt_io.stop()
        print("종료됨")


if __name__ == "__main__":
    main()
