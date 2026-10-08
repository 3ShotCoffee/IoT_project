"""
MQTT 관리
    받기: face/user_id  {"user_id": 1, "name": "user_01"}  (젯슨 -> 파이)
    보내기: button      {"button": 1}                      (파이 -> 젯슨)

다른 파일에서 쓰는 함수:
    start(on_user)          연결 시작. on_user(user_id, name): 사용자 메시지를 받았을 때 호출
    publish_button(num)     버튼 눌림 보내기
    stop()                  연결 정리
"""
import json

import paho.mqtt.client as mqtt     # pip install paho-mqtt

# ---------------- 설정값 ----------------
MQTT_HOST = "localhost"        # 브로커(Mosquitto)가 이 라즈베리파이에 있으면 localhost
MQTT_PORT = 1883
USER_TOPIC = "face/user_id"    # 젯슨 -> 파이: 사용자 정보
BUTTON_TOPIC = "button"        # 파이 -> 젯슨: 버튼 눌림

_client = None
_on_user = None


def _on_connect(client, userdata, flags, reason_code, properties):
    # 연결될 때마다(재연결 포함) 다시 구독해야 메시지를 계속 받음
    if reason_code == 0:
        print(f"MQTT 연결됨: {MQTT_HOST}:{MQTT_PORT}, 구독 토픽: {USER_TOPIC}")
        client.subscribe(USER_TOPIC, qos=1)
    else:
        print(f"MQTT 연결 실패: {reason_code}")


def _on_message(client, userdata, msg):
    try:
        data = json.loads(msg.payload.decode())
        user_id = int(data["user_id"])
        name = str(data["name"])
    except (ValueError, KeyError, TypeError) as e:
        print(f"MQTT 메시지 형식 오류, 무시함: {msg.payload!r} ({e})")
        return
    try:
        _on_user(user_id, name)
    except Exception as e:
        # 처리 중 오류가 나도 MQTT 수신 스레드가 멈추지 않도록
        print(f"사용자 처리 오류: {e}")


def start(on_user):
    global _client, _on_user
    _on_user = on_user
    _client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    _client.on_connect = _on_connect
    _client.on_message = _on_message
    _client.reconnect_delay_set(min_delay=1, max_delay=5)   # 끊기면 1~5초 간격으로 재연결
    _client.connect_async(MQTT_HOST, MQTT_PORT)             # 브로커가 아직 안 켜져 있어도 멈추지 않음
    _client.loop_start()                                    # 송수신을 별도 스레드에서 실행


def publish_button(num):
    """버튼 눌림을 젯슨에 전달. 연결이 끊겨 있으면 늦게 전달되지 않도록 버림"""
    if _client is None or not _client.is_connected():
        print(f"버튼 {num} 눌림 -> MQTT 연결 안 됨, 전달하지 않음")
        return
    msg = json.dumps({"button": num})
    _client.publish(BUTTON_TOPIC, msg, qos=1, retain=False)   # 일회성 사건이라 retain 안 함
    print(f"버튼 {num} 눌림 -> MQTT {BUTTON_TOPIC} 전송: {msg}")


def stop():
    if _client is not None:
        _client.loop_stop()
        _client.disconnect()
