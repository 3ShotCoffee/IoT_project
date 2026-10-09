
""""
실행:
    python3 recognize_and_register.py
    python3 recognize_and_register.py --mqtt

인식 화면에서 등록되지 않은 얼굴이 보이면 r을 눌러 등록을 시작합니다.
등록 화면에서는 각도를 바꾸고 Space를 눌러 샘플을 저장합니다.
샘플을 다 모으면 사용하지 않는 ID를 자동 배정합니다. 등록 화면에서 c는 취소,
인식 화면에서 q는 프로그램 종료입니다.
"""

import time
import threading
T_START = time.perf_counter()
print(f"[시간] import: {time.perf_counter() - T_START:.2f}초")
from queue import Queue, Empty
import argparse
from datetime import datetime
from pathlib import Path
import json

import cv2
import numpy as np
import onnxruntime as ort
from insightface.app import FaceAnalysis
from flask import Flask, Response, abort, request


# MJPEG는 추론 루프가 읽는 동일 프레임을 재사용합니다. 별도 카메라를 열지 않습니다.
_mjpeg_condition = threading.Condition()
_mjpeg_jpeg = None
_mjpeg_sequence = 0
_mjpeg_last_encode = 0.0
_mjpeg_app = Flask(__name__)


def update_mjpeg_frame(frame):
    """현재 카메라 프레임을 최대 초당 10장 JPEG로 공유합니다."""
    global _mjpeg_jpeg, _mjpeg_sequence, _mjpeg_last_encode
    now = time.monotonic()
    if now - _mjpeg_last_encode < 0.1:
        return
    _mjpeg_last_encode = now

    resized = cv2.resize(frame, (480, 360))
    ok, encoded = cv2.imencode(
        ".jpg", resized, [cv2.IMWRITE_JPEG_QUALITY, 70]
    )
    if not ok:
        return
    with _mjpeg_condition:
        _mjpeg_jpeg = encoded.tobytes()
        _mjpeg_sequence += 1
        _mjpeg_condition.notify_all()


@_mjpeg_app.route("/video")
def mjpeg_video():
    # Jetson의 LAN IP만으로 공개하지 않고, 요청한 Pi IP와 일치할 때만 허용합니다.
    allowed_ip = _mjpeg_app.config.get("VIDEO_ALLOWED_IP")
    if not allowed_ip or request.remote_addr != allowed_ip:
        abort(403)

    def frames():
        last_sequence = -1
        while True:
            with _mjpeg_condition:
                _mjpeg_condition.wait_for(
                    lambda: _mjpeg_sequence != last_sequence, timeout=10.0
                )
                if _mjpeg_sequence == last_sequence:
                    continue
                jpeg = _mjpeg_jpeg
                last_sequence = _mjpeg_sequence
            if jpeg is None:
                continue
            yield (b"--frame\r\n"
                   b"Content-Type: image/jpeg\r\n"
                   b"Content-Length: " + str(len(jpeg)).encode() +
                   b"\r\n\r\n" + jpeg + b"\r\n")

    return Response(
        frames(), mimetype="multipart/x-mixed-replace; boundary=frame"
    )


def start_mjpeg_server(port, allowed_ip):
    """Pi 한 대만 접속할 수 있는 MJPEG 서버를 백그라운드에서 시작합니다."""
    _mjpeg_app.config["VIDEO_ALLOWED_IP"] = allowed_ip
    server_thread = threading.Thread(
        target=lambda: _mjpeg_app.run(
            host="0.0.0.0", port=port, threaded=True,
            debug=False, use_reloader=False
        ),
        name="mjpeg-server", daemon=True
    )
    server_thread.start()


button_queue = Queue()
def parse_args():
    parser = argparse.ArgumentParser(description="실시간 얼굴 식별 및 사용자 등록")
    parser.add_argument("--db-dir", default="image_data", help="user_<ID>.npz 등록 폴더")
    parser.add_argument("--image-dir", default="image", help="등록 사진 저장 폴더")
    parser.add_argument("--camera", type=int, default=0, help="웹캠 인덱스")
    parser.add_argument("--threshold", type=float, default=0.45,
                        help="코사인 유사도 기준 (임시 시작값, 실제 점수로 조정)")
    parser.add_argument("--stable-frames", type=int, default=20,
                        help="같은 ID가 연속 확인되어야 하는 프레임 수")
    parser.add_argument("--samples", type=int, default=15, help="신규 등록 샘플 수")
    parser.add_argument("--det-size", type=int, default=320, help="검출 입력 크기")
    parser.add_argument( "--no-mqtt", dest="mqtt", action="store_false",help="MQTT 발행 비활성화")
    parser.set_defaults(mqtt=True)
    parser.add_argument("--broker", default="10.10.16.75", help="MQTT 브로커 주소")
    parser.add_argument("--port", type=int, default=1883, help="MQTT 포트")
    parser.add_argument("--face_topic", default="face/user_id", help="ID/name 발행 토픽")
    parser.add_argument("--face_state_topic", default="face/state", help="얼굴 상태 발행 토픽")
    parser.add_argument( "--face_mode_topic",default="face/mode", help="화면 모드 발행 토픽")
    parser.add_argument("--video-port", type=int, default=5000, help="MJPEG 영상 서버 포트")
    parser.add_argument("--video-allowed-ip", default=None,
                        help="영상 접속을 허용할 Pi의 IP (미지정 시 MJPEG 비활성화)")
    return parser.parse_args()


def load_profiles(directory):
    """등록 폴더의 user_<ID>.npz 파일들을 읽어 메모리에 준비합니다."""
    profiles = {}
    for file_path in sorted(Path(directory).glob("user_*.npz")):
        try:
            with np.load(str(file_path), allow_pickle=False) as data:
                user_id = int(np.asarray(data["user_id"]).item())
                embedding = np.asarray(data["embedding"], dtype=np.float32).reshape(-1)
                name = str(np.asarray(data["name"]).item()) if "name" in data else ""
            norm = np.linalg.norm(embedding)
            if norm == 0:
                print("빈 임베딩이라 건너뜁니다: {}".format(file_path))
                continue
            profiles[user_id] = {
                "embedding": embedding / norm,
                "name": name,
                "file": str(file_path),
            }
            print("등록 프로필: ID={}, 이름={!r}".format(user_id, name))
        except (OSError, KeyError, ValueError) as exc:
            print("npz를 읽지 못해 건너뜁니다: {} ({})".format(file_path, exc))
    return profiles


def identify(embedding, profiles, threshold):
    """현재 임베딩을 등록 프로필과 비교하고 ID와 최고 점수를 반환합니다."""
    current = normalize_embedding(embedding)
    if current is None:
        return None, None

    best_id = -1
    best_score = None
    for user_id, profile in profiles.items():
        known = normalize_embedding(profile.get("embedding"))
        if known is None:
            continue
        if current.shape != known.shape:
            continue
        # 단위 길이 벡터끼리의 내적은 코사인 유사도입니다.
        score = float(np.dot(current, known))
        if best_score is None or score > best_score:
            best_id, best_score = user_id, score

    # 저장된 프로필이 없으면 유효한 얼굴이지만 비교할 등록 사용자가 없습니다.
    if best_score is None:
        return (-1, None) if not profiles else (None, None)

    if best_score < threshold:
        return -1, best_score
    return best_id, best_score


def normalize_embedding(embedding):
    """벡터가 비교 가능한 숫자인지 확인하고 길이를 1로 맞춥니다."""
    if embedding is None:
        return None
    try:
        vector = np.asarray(embedding, dtype=np.float32).reshape(-1)
    except (TypeError, ValueError):
        return None
    if vector.size == 0 or not np.isfinite(vector).all():
        return None
    norm = float(np.linalg.norm(vector))
    if not np.isfinite(norm) or norm <= 1e-12:
        return None
    return vector / norm


def register_user(app, cap, args, user_id, user_name, publish_face_state, publish_face_mode, capture_event_publisher):
    """Space로 여러 샘플을 모아 사진과 새 사용자 npz를 저장합니다."""
    image_root = Path(args.image_dir) / "user_{}".format(user_id)
    session = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    photo_dir = image_root / session
    photo_dir.mkdir(parents=True, exist_ok=False)
    publish_face_mode("registration")

    embeddings = []
    stable_since = None
    previous_bbox = None
    last_capture_bbox = None
    waiting_for_move = False
    print("새 사용자 ID {} 등록: 얼굴 각도를 바꾸고 Space를 {}번 누르세요. c: 취소".format(
        user_id, args.samples))
    
    while len(embeddings) < args.samples:
        ok, frame = cap.read()
        if not ok:
            print("웹캠 프레임을 읽지 못했습니다.")
            publish_face_mode("recognize")
            return None

        update_mjpeg_frame(frame)
        photo_frame = frame.copy()
        analysis_failed = False
        try:
            faces = app.get(frame)
        except Exception as exc:
            print("얼굴 분석 실패, 이 프레임은 건너뜁니다: {}".format(exc))
            faces = []
            analysis_failed = True
        face = faces[0] if len(faces) == 1 else None
        ready = False
        status = "Analysis failed - frame ignored" if analysis_failed else "Show exactly one face"
        quality_issue = None
        if face is not None:
            x1, y1, x2, y2 = face.bbox.astype(int)
            quality_issue = face_quality_issue(face, frame.shape)
            ready = quality_issue is None
            status = "Ready - move face slowly" if ready else "Not ready: {}".format(
                quality_issue)
        now = time.monotonic()
        publish_face_state(get_face_state(analysis_failed, face, quality_issue))
        
        auto_capture = False
        current_bbox = None

        if face is not None and ready:
            current_bbox = np.asarray(
                face.bbox, dtype=np.float32
            ).reshape(4).copy()

            if waiting_for_move:
                if (last_capture_bbox is not None
                        and bbox_iou(last_capture_bbox, current_bbox) < 0.80):
                    # 얼굴 위치가 움직였으므로 다음 샘플의 안정 시간 측정을 시작합니다.
                    waiting_for_move = False
                    stable_since = now
                    previous_bbox = current_bbox.copy()
                    status = "Hold still for 3 seconds"
                else:
                    status = "Move face slightly for next sample"

            elif previous_bbox is None:
                stable_since = now
                previous_bbox = current_bbox.copy()
                status = "Hold still for 3 seconds"

            else:
                overlap = bbox_iou(previous_bbox, current_bbox)

                # 박스가 많이 달라졌으면 움직인 것으로 보고 시간을 다시 셉니다.
                if overlap < 0.90:
                    stable_since = now

                previous_bbox = current_bbox.copy()
                stable_time = now - stable_since
                auto_capture = stable_time >= 1.0
                status = "Hold still: {:.1f}/1.0 sec".format(
                    min(stable_time, 1.0)
                )

        else:
            # 얼굴이 없거나 품질 검사를 통과하지 못하면 안정 시간을 다시 셉니다.
            stable_since = None
            previous_bbox = None    
            
            if face is not None:
                x1, y1, x2, y2 = face.bbox.astype(int)  
                cv2.rectangle(
                    frame, (x1, y1), (x2, y2),
                    (0, 220, 0) if ready else (220, 0, 0), 2
                )

        cv2.putText(frame, status, (16, 30), cv2.FONT_HERSHEY_SIMPLEX,
                    0.6, (0, 255, 0), 2)
        cv2.putText(frame, "Samples: {}/{}".format(len(embeddings), args.samples),
                    (16, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        cv2.putText(frame, "Hold still 3 seconds | cancel button 2", (16, 90),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        cv2.imshow("Identify / enroll", frame)

        key = cv2.waitKey(1) & 0xFF
        button_number = None
        try:
            button_number = button_queue.get_nowait()
        except Empty:
            pass
        if button_number == 2:
            print("등록을 취소했습니다. 인식 화면으로 돌아갑니다. 웹캠은 계속 켜져 있습니다.")
            publish_face_mode("recognize")
            return None
        if auto_capture:
            if analysis_failed or not ready:
                print("얼굴 한 명이 선명하게 보일 때만 저장할 수 있습니다.")
                continue
            vector = normalize_embedding(getattr(face, "embedding", None))
            if vector is None:
                print("얼굴 벡터가 유효하지 않아 저장하지 않았습니다.")
                stable_since = None
                previous_bbox = None
                continue

            sample_no = len(embeddings) + 1
            photo_file = photo_dir / "face_{:03d}.jpg".format(sample_no)

            if cv2.imwrite(str(photo_file), photo_frame):
                embeddings.append(vector)
                last_capture_bbox = current_bbox.copy()
                capture_event_publisher(len(embeddings), args.samples)

                # 다음 샘플은 얼굴을 움직인 뒤 다시 3초간 멈추면 저장합니다.
                waiting_for_move = len(embeddings) < args.samples
                stable_since = None
                previous_bbox = None

                print("샘플 {}/{} 자동 저장".format(
                    len(embeddings), args.samples
                ))
            else:
                print("사진 저장 실패: {}".format(photo_file))

    mean_embedding = np.mean(np.stack(embeddings), axis=0)
    mean_embedding /= np.linalg.norm(mean_embedding)
    profile_file = Path(args.db_dir) / "user_{}.npz".format(user_id)
    np.savez_compressed(
        str(profile_file),
        user_id=np.int64(user_id),
        name=np.asarray(user_name),
        embedding=mean_embedding.astype(np.float32),
        sample_count=np.int64(len(embeddings)),
    )
    print("등록 완료: {}".format(profile_file))
    print("사진 저장: {}".format(photo_dir))
    publish_face_mode("recognize")
    return {
        "embedding": mean_embedding.astype(np.float32),
        "name": user_name,
        "file": str(profile_file),
    }


def make_mqtt_client(host, port, button_queue):
    """--mqtt가 있을 때만 브로커에 연결합니다."""
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        raise SystemExit("MQTT에는 paho-mqtt가 필요합니다: python -m pip install paho-mqtt")
    
    def on_message(client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode("utf-8"))
            button_number = int(data["button"])
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            print("잘못된 버튼 메시지:", msg.payload)
            return

        if  button_number in (1, 2):
            button_queue.put(button_number)

    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            print("MQTT 연결됨: {}:{}".format(host, port))
            client.subscribe("button", qos=1)  # 여기서 구독
        else:
            print("MQTT 연결 실패, 결과 코드: {}".format(rc))

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                         client_id="jetson-face-recognizer")
    client.on_connect = on_connect
    client.on_message = on_message 
    try:
        client.connect(host, port, keepalive=30)
    except OSError as exc:
        raise SystemExit("MQTT 브로커 {}:{}에 연결할 수 없습니다: {}".format(
            host, port, exc))
    client.loop_start()
    return client, mqtt


def format_user_name(user_id):
    """사용자 ID를 user_01 형식의 이름으로 바꿉니다."""
    return "user_{:02d}".format(int(user_id))


def publish_user_id(client, mqtt_module, topic, user_id, name):
    payload = json.dumps({
        "user_id": int(user_id),
        "name": None if name is None else str(name),
    })
    result = client.publish(topic, payload, qos=1, retain=False)
    if result.rc == mqtt_module.MQTT_ERR_SUCCESS:
        print("MQTT 발행: topic={!r}, payload={}".format(topic, user_id))
    else:
        print("MQTT 발행 실패/대기: rc={}".format(result.rc))

def has_five_landmarks(face, frame_shape):
    points = getattr(face, "kps", None)
    if points is None:
        return False
    points = np.asarray(points)
    if points.shape != (5, 2):
        return False

    height, width = frame_shape[:2]
    return bool(
        np.isfinite(points).all()
        and (points[:, 0] >= 0).all()
        and (points[:, 0] < width).all()
        and (points[:, 1] >= 0).all()
        and (points[:, 1] < height).all()
    )


def face_quality_issue(face, frame_shape):
    """비교/등록에 사용할 얼굴과 벡터가 충분히 갖춰졌는지 이유를 반환합니다."""
    height, width = frame_shape[:2]
    bbox = getattr(face, "bbox", None)
    if bbox is None:
        return "bounding box missing" # 얼굴 영역을 찾을 수 없음
    try:
        x1, y1, x2, y2 = np.asarray(bbox, dtype=np.float32).reshape(-1)
    except (TypeError, ValueError):
        return "invalid bounding box" 
    if not np.isfinite([x1, y1, x2, y2]).all():
        return "invalid bounding box" # 얼굴 영역 정보를 확인할 수 없음
    if x1 <= 0 or y1 <= 0 or x2 >= width or y2 >= height:
        return "face touches/crosses frame edge" # 얼굴이 화면 가장자리에 닿거나 잘림
    if x2 - x1 < 80 or y2 - y1 < 80: 
        return "face too small"  # 얼굴이 너무 작음
    
    try:
        det_score = float(getattr(face, "det_score", 0.0))
    except (TypeError, ValueError):
        return "invalid detection score" 
    if not np.isfinite(det_score) or det_score < 0.6:
        return "face detection unclear"
    if not has_five_landmarks(face, frame_shape):
        return "five landmarks missing/outside frame"
    if normalize_embedding(getattr(face, "embedding", None)) is None:
        return "embedding missing/invalid"
    return None


def get_face_state(analysis_failed, face, quality_issue):
    if analysis_failed:
        return "unclear"
    if face is None:
        return "no_face"
    if quality_issue is None:
        return "ready"
    if quality_issue == "face too small":
        return "too_small"
    if quality_issue == "face touches/crosses frame edge":
        return "clipped"
    return "unclear"

def make_face_state_publisher(client, mqtt_module, topic):
    last_state = [None]

    def publish_if_changed(state):
        if client is None or state == last_state[0]:
            return

        payload = json.dumps({"state": state})
        result = client.publish(topic, payload, qos=1, retain=True)

        if result.rc == mqtt_module.MQTT_ERR_SUCCESS:
            print("얼굴 상태 발행: topic={!r}, payload={}".format(
                topic, payload))
            last_state[0] = state
        else:
            print("얼굴 상태 발행 실패: rc={}".format(result.rc))

    return publish_if_changed

def make_capture_event_publisher(client, mqtt_module, topic):
    def publish_capture_event(sample, total):
        if client is None:
            return

        payload = json.dumps({
            "state": "photo_captured",
            "sample": int(sample),
            "total": int(total),
        })
        result = client.publish(topic, payload, qos=1, retain=False)

        if result.rc == mqtt_module.MQTT_ERR_SUCCESS:
            print("촬영 이벤트 발행: topic={!r}, payload={}".format(topic, payload))
        else:
            print("촬영 이벤트 발행 실패: rc={}".format(result.rc))

    return publish_capture_event    

def make_face_mode_publisher(client, mqtt_module, topic):
    last_mode = [None]

    def publish_if_changed(mode):
        if client is None or mode == last_mode[0]:
            return

        payload = json.dumps({"mode": mode})
        result = client.publish(topic, payload, qos=1, retain=True)

        if result.rc == mqtt_module.MQTT_ERR_SUCCESS:
            print("화면 모드 발행: topic={!r}, payload={}".format(
                topic, payload))
            last_mode[0] = mode
        else:
            print("화면 모드 발행 실패: rc={}".format(result.rc))

    return publish_if_changed

def bbox_iou(box_a, box_b):
    """두 바운딩 박스가 겹치는 정도를 0~1로 계산합니다."""
    if box_a is None or box_b is None:
        return 0.0
    try:
        a = np.asarray(box_a, dtype=np.float32).reshape(4)
        b = np.asarray(box_b, dtype=np.float32).reshape(4) # np 배열로 바꾸고,
        #소수점 계산이 가능한 숫자 형식으로 변환, 좌표가 숫자 4개로 된 모양인지 맞춤.
        #숫자로 바꿀 수 없거나, 좌표개수가 맞지않거나, nan이나 무한한 수이면, 0.0 리턴
    except (TypeError, ValueError):
        return 0.0
    if not np.isfinite(a).all() or not np.isfinite(b).all():
        return 0.0

    left = max(a[0], b[0])
    top = max(a[1], b[1])
    right = min(a[2], b[2])
    bottom = min(a[3], b[3]) #왼쪽위 좌표,오른쪽 아래 좌표  x,y
    intersection = max(0.0, right - left) * max(0.0, bottom - top) # 겹친영역 계산
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1]) 
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1]) # 두 바운딩 박스 넓이 따로계산
    union = area_a + area_b - intersection # 계산에서 겹치는 부분을 뻄
    return float(intersection / union) if union > 0.0 else 0.0 
    # 겹친 넓이를 전체 넓이로 나눈 값을 리턴, union이 0이면 나눌 수 없으므로 0.0 리턴
    # 따라서 1.0 = 두박스가 완전히 겹침, 0.0 두박스가 겹치지않음.


def main():
    args = parse_args()
    if args.samples < 1 or args.stable_frames < 1:
        raise SystemExit("samples와 stable-frames는 1 이상이어야 합니다.")
    if not -1.0 <= args.threshold <= 1.0:
        raise SystemExit("threshold는 -1과 1 사이여야 합니다.")

    db_dir = Path(args.db_dir)
    db_dir.mkdir(parents=True, exist_ok=True)
    image_dir = Path(args.image_dir)
    image_dir.mkdir(parents=True, exist_ok=True)
    profiles = load_profiles(db_dir)
    available = ort.get_available_providers()
    if "CUDAExecutionProvider" not in available:
        raise SystemExit("CUDAExecutionProvider를 사용할 수 없습니다: {}".format(available))
    t = time.perf_counter()
    app = FaceAnalysis(name="buffalo_sc", providers=["CUDAExecutionProvider"])
    print(f"[시간] FaceAnalysis 생성: {time.perf_counter() - t:.2f}초")
    t = time.perf_counter()
    app.prepare(ctx_id=0, det_size=(args.det_size, args.det_size))
    print(f"[시간] app.prepare: {time.perf_counter() - t:.2f}초")
    
    # 모델 미리 돌리기: GPU 준비 시간을 시작 단계에서 끝냄
    t = time.perf_counter()
    app.get(np.zeros((args.det_size, args.det_size, 3), dtype=np.uint8))   # 검출 모델
    rec = app.models.get("recognition")
    if rec is not None:
        rec.get_feat(np.zeros((112, 112, 3), dtype=np.uint8))              # 인식 모델
    print(f"[시간] 모델 준비(warm-up): {time.perf_counter() - t:.2f}초")

    # 화면 창 미리 만들기 (반복문의 imshow와 같은 이름)
    t = time.perf_counter()
    cv2.namedWindow("Identify / enroll")
    cv2.waitKey(1)
    print(f"[시간] 화면 창 준비: {time.perf_counter() - t:.2f}초")

    mqtt_client = None
    mqtt_module = None
    if args.mqtt:
        mqtt_client, mqtt_module = make_mqtt_client(args.broker, args.port, button_queue)

    publish_face_state = make_face_state_publisher(
    mqtt_client, mqtt_module, args.face_state_topic)
    publish_face_mode = make_face_mode_publisher(
    mqtt_client, mqtt_module, args.face_mode_topic)   
    publish_capture_event = make_capture_event_publisher(
    mqtt_client, mqtt_module, args.face_state_topic)    
    publish_face_mode("recognize")

    t = time.perf_counter()
    cap = cv2.VideoCapture(args.camera)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    print(f"[시간] 웹캠 열기: {time.perf_counter() - t:.2f}초")
    if not cap.isOpened():
        if mqtt_client is not None:
            mqtt_client.loop_stop()
            mqtt_client.disconnect()
        raise SystemExit("웹캠을 열 수 없습니다. --camera 값을 확인하세요.")

    if args.video_allowed_ip:
        start_mjpeg_server(args.video_port, args.video_allowed_ip)
        print("Pi 전용 MJPEG 영상: http://<Jetson-IP>:{}/video (허용 IP: {})".format(
            args.video_port, args.video_allowed_ip))
    else:
        print("MJPEG 비활성화: Pi 접속 IP를 --video-allowed-ip로 지정하세요.")

    candidate_id = None
    candidate_count = 0
    confirmed_id = None
    confirmed_bbox = None
    read_sum = get_sum = 0.0
    frame_count = 0
    loop_start = time.perf_counter()
    analysis_error_reported = False
    print("실시간 식별 시작. Unknown일 때 r: 등록, q: 종료")
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                print("웹캠 프레임을 읽지 못했습니다.")
                break

            update_mjpeg_frame(frame)
            t = time.perf_counter()
            analysis_failed = False
            try:
                faces = app.get(frame)
                analysis_error_reported = False
            except Exception as exc:
                # 얼굴 분석 자체가 실패한 프레임은 Unknown으로 세지 않습니다.
                faces = []
                analysis_failed = True
                if not analysis_error_reported:
                    print("얼굴 분석 실패, 해당 프레임은 무시합니다: {}".format(exc))
                    analysis_error_reported = True
            get_sum += time.perf_counter() - t

            frame_count += 1
            if frame_count == 30:
                elapsed = time.perf_counter() - loop_start
                print(f"[시간] FPS {30 / elapsed:.1f} | "
                      f"read 평균 {read_sum / 30 * 1000:.0f}ms | "
                      f"app.get 평균 {get_sum / 30 * 1000:.0f}ms")
                read_sum = get_sum = 0.0
                frame_count = 0
                loop_start = time.perf_counter()

            face = max(
                faces,
                key=lambda item: (item.bbox[2] - item.bbox[0]) *
                                 (item.bbox[3] - item.bbox[1]),
            ) if faces else None # 많은 얼굴중 가장 큰 얼굴 하나만 선정
 
            best_score = None
            quality_issue = None
            temporarily_uncertain = False
            current_bbox = None
            display_bbox = None
            if face is not None:
                try:
                    current_bbox = np.asarray(
                        face.bbox, dtype=np.float32).reshape(4).copy()
                    display_bbox = current_bbox
                except (TypeError, ValueError):
                    current_bbox = None

            if analysis_failed:
                this_candidate = None
                status = "Analysis failed - frame ignored"
            elif face is None:
                this_candidate = None
                status = "No face"
            else:
                quality_issue = face_quality_issue(face, frame.shape)
                if quality_issue is not None:
                    this_candidate = None
                    status = "Ignored: {}".format(quality_issue)
                else:
                    this_candidate, best_score = identify(
                        getattr(face, "embedding", None), profiles, args.threshold)
                    if this_candidate is None:
                        status = "Cannot compare - frame ignored"
                        quality_issue = "embedding incompatible with profiles"
                    elif this_candidate == -1:
                        status = ("Unknown | R: enroll" if best_score is None else
                                  "Unknown score={:.3f} | R: enroll".format(best_score))
                    else:
                        status = "ID {} score={:.3f}".format(this_candidate, best_score)

                # 직전 확정 ID의 상자와 현재 상자가 충분히 겹치면 -1을 보류합니다.
                # 겹치지않으면 -1 -> 0
                if (this_candidate == -1
                        and confirmed_id is not None
                        and confirmed_id >= 0
                        and current_bbox is not None
                        and bbox_iou(confirmed_bbox, current_bbox) >= 0.5):
                        # 겹치는 정도가 0.5 이상일 경우에 -1 보류 
                    temporarily_uncertain = True
                    this_candidate = None
                    display_bbox = confirmed_bbox.copy()
                    status = "Temporarily uncertain - keeping ID {}".format(confirmed_id)
                elif (this_candidate is not None and this_candidate >= 0
                      and confirmed_id is not None and confirmed_id >= 0):
                    if this_candidate == confirmed_id and current_bbox is not None:
                        # 같은 ID가 정상 인식되면 기준 박스를 현재 위치로 갱신합니다.
                        confirmed_bbox = current_bbox.copy()
                    elif this_candidate != confirmed_id:
                        # 다른 ID가 감지되면 이전 ID의 박스를 더 이상 사용하지 않습니다.
                        confirmed_bbox = None
                        


                if display_bbox is not None:
                    x1, y1, x2, y2 = display_bbox.astype(int)
                    if temporarily_uncertain or this_candidate is None:
                        color = (0, 200, 255)  # 잠시 확인 어려움 또는 분석 불충분
                    else:
                        color = (0, 220, 0) if this_candidate != -1 else (0, 0, 255)
                    cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)

            if this_candidate is None:
                # 확인 보류/분석 불충분은 Unknown 연속 횟수에 포함하지 않습니다.
                candidate_id = None
                candidate_count = 0
            elif this_candidate == candidate_id:
                candidate_count += 1
            else:
                candidate_id = this_candidate
                candidate_count = 1

            # 같은 결과가 충분히 이어졌을 때만 확정합니다. Unknown은 MQTT로 보내지 않습니다.
            if (this_candidate is not None
                    and candidate_count >= args.stable_frames
                    and candidate_id != confirmed_id):
                confirmed_id = candidate_id
                if confirmed_id == -1:
                    confirmed_bbox = None
                    print("등록된 사용자가 아닙니다. 등록하려면 인식 화면에서 r을 누르세요.")
                else:
                    if current_bbox is not None:
                        confirmed_bbox = current_bbox.copy()
                    print("확정 사용자 ID: {}".format(confirmed_id))
                    if mqtt_client is not None:
                        published_name = (
                            profiles.get(confirmed_id, {}).get("name")
                            or format_user_name(confirmed_id)
                        )
                        publish_user_id(
                            mqtt_client, mqtt_module, args.face_topic,
                            confirmed_id, published_name)

            cv2.putText(frame, status, (16, 30), cv2.FONT_HERSHEY_SIMPLEX,
                        0.6, (0, 255, 0), 2)
            confirmed_label = (
                "ID {} (temporary)".format(confirmed_id) if temporarily_uncertain
                else "-" if confirmed_id is None else confirmed_id
            )
            cv2.putText(frame, "Stable: {}/{}  Confirmed: {}".format(
                min(candidate_count, args.stable_frames), args.stable_frames,
                confirmed_label),
                (16, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
            cv2.imshow("Identify / enroll", frame)

            key = cv2.waitKey(1) & 0xFF

            button_number = None
            try:
                button_number = button_queue.get_nowait()
            except Empty:
                pass

            if button_number == 1:
                key = ord("r")

            if key == ord("q"):
                break


            # 등록은 품질 검사를 통과한 Unknown 얼굴에 대해서만 시작합니다.
            if key == ord("r"):
                if temporarily_uncertain:
                    print("이전 ID를 임시 유지 중이므로 신규 등록을 시작하지 않습니다.")
                    continue
                if analysis_failed:
                    print("얼굴 분석에 실패해 등록할 수 없습니다. 다시 시도하세요.")
                    continue
                if face is None:
                    print("화면에 얼굴이 없어 등록할 수 없습니다.")
                    continue
                if quality_issue is not None:
                    print("얼굴 상태가 불충분해 등록하지 않습니다: {}".format(quality_issue))
                    continue
                if this_candidate is None:
                    print("얼굴 벡터를 비교할 수 없어 등록하지 않습니다.")
                    continue
                if this_candidate != -1:
                    print("이미 등록된 사용자입니다. 새 사용자로 등록하지 않습니다.")
                    continue
                if candidate_count < args.stable_frames:
                    print("Unknown 판정을 확인 중입니다. 잠시 같은 얼굴을 유지한 뒤 다시 누르세요.")
                    continue
                existing_names = {str(p.get("name", "")) for p in profiles.values()}
                new_user_id = max(profiles.keys(), default=0) + 1
                new_user_name = format_user_name(new_user_id)
                while ((db_dir / "user_{}.npz".format(new_user_id)).exists()
                       or new_user_name in existing_names):
                    new_user_id += 1
                    new_user_name = format_user_name(new_user_id)
                print("새 사용자에게 ID {} / 이름 {}을 배정합니다.".format(
                    new_user_id, new_user_name))
                profile = register_user(app, cap, args, new_user_id, new_user_name, publish_face_state, publish_face_mode, publish_capture_event)
                if profile is not None:
                    profiles[new_user_id] = profile
                    candidate_id = None
                    candidate_count = 0
                    confirmed_id = new_user_id
                    if current_bbox is not None:
                        confirmed_bbox = current_bbox.copy()
                    print("ID {} ({}) 등록됨. 인식 화면으로 돌아갑니다.".format(
                        new_user_id, new_user_name))
                    if mqtt_client is not None:
                        publish_user_id(
                            mqtt_client, mqtt_module, args.face_topic,
                            new_user_id, new_user_name)
                else:
                    # 취소하거나 등록이 끝나지 않았으면 이전 Unknown 횟수를 초기화합니다.
                    candidate_id = None
                    candidate_count = 0
                    print("등록이 완료되지 않았습니다. 인식 화면으로 돌아갑니다.")
    finally:
        cap.release()
        cv2.destroyAllWindows()
        if mqtt_client is not None:
            mqtt_client.loop_stop()
            mqtt_client.disconnect()


if __name__ == "__main__":
    main()
