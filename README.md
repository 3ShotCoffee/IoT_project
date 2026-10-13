# IoT_project

## 라즈베리파이 환경 설정
1. OS 패키지: `sudo apt install git libportaudio2 libopenblas0 ffmpeg mariadb-server mosquitto mosquitto-clients`
2. 가상환경: `python3 -m venv ~/eq-env` → `source ~/eq-env/bin/activate` → `pip install -r requirements.txt`
3. 오디오: `/boot/firmware/config.txt`에 `dtoverlay=max98357a` 추가, `dtparam=audio=on` 주석 처리, 재부팅
4. 시리얼 권한: `sudo usermod -aG dialout pi`
5. DB: 계정 만들기(SQL) → `use audio`
6. `config.py` 생성 후 비밀번호 입력

   # 사용자 맞춤 스마트 EQ 시스템

얼굴 인식으로 사용자를 구분하고, Raspberry Pi가 해당 사용자에게 저장된
EQ 프리셋을 적용하는 시스템입니다.

## 시스템 구성

- **Jetson**: 카메라 영상에서 얼굴을 검출·인식하고 사용자 ID와 얼굴 상태를 전달
- **Raspberry Pi**: 전달받은 사용자 ID에 맞춰 EQ 프리셋 적용
- **통신**: MQTT로 상태와 ID를 주고받고, 필요하면 MJPEG로 카메라 영상을 전달

## Vision 기능

### 얼굴 검출 및 사용자 식별

InsightFace의 `buffalo_sc` 모델로 얼굴 임베딩을 만들고,
등록된 사용자 임베딩과 코사인 유사도를 비교합니다.
유사도가 기준값보다 낮으면 Unknown으로 판정합니다.

### 여러 얼굴 중 대상 선택

화면에 얼굴이 여러 명 잡히면 바운딩 박스 면적이 가장 큰 얼굴 하나를
주 인식 대상으로 선택합니다. 가까운 사용자를 우선 처리하기 위한 정책입니다.

### 연속 프레임 확인

한 프레임의 결과만으로 ID를 바로 바꾸지 않습니다.
같은 ID가 설정된 프레임 수만큼 연속 확인되면 사용자를 확정해 Pi로 전달합니다.
이 과정을 통해 순간적인 인식 변화로 EQ가 자주 바뀌는 현상을 줄입니다.

### IoU를 이용한 일시적 Unknown 완화

확정된 사용자가 잠깐 Unknown으로 판정되더라도 현재 얼굴 박스와
직전 확정 박스의 IoU가 기준 이상이면 기존 ID를 유지합니다.
잠깐의 인식 흔들림 때문에 EQ가 바뀌는 현상을 완화하기 위한 보조 처리입니다.


### 신규 사용자 등록

등록 과정에서 여러 얼굴 샘플을 모아 임베딩 프로필을 저장합니다.
이미 등록된 사용자로 판정되면 중복 촬영을 막습니다.

## MQTT 메시지

| 토픽 | 용도 | 예시 |
|---|---|---|
| `face/user_id` | 확정된 사용자 ID 전달 | `{"user_id": 128, "name": "사용자 이름"}` |
| `face/state` | 얼굴 검출 및 등록 상태 전달 | `{"state": "no_face"}` |
| `face/mode` | 인식/등록 모드 전달 | `{"mode": "recognize"}` |

얼굴이 없는 `no_face` 상태와, 얼굴은 있지만 등록되지 않은 `Unknown`은
서로 다른 상태로 처리합니다.

## 사용 기술

- Python
- OpenCV
- InsightFace (`buffalo_sc`)
- ONNX Runtime CUDA Execution Provider
- NumPy
- MQTT (Paho MQTT)
- Flask / MJPEG (영상 모니터링 기능 사용 시)

## 환경 및 설정

Jetson에서 CUDA를 사용할 수 있는 ONNX Runtime 환경과 프로젝트의
Python 의존성을 준비해야 합니다. Pi와 Jetson은 같은 MQTT 브로커에
접속할 수 있어야 합니다.

주요 실행 옵션은 다음과 같습니다.

- `--camera`: 카메라 인덱스
- `--threshold`: 얼굴 유사도 판정 기준
- `--stable-frames`: ID 확정에 필요한 연속 프레임 수
- `--samples`: 사용자 등록 샘플 수
- `--no-mqtt`: MQTT 비활성화
- `--video-allowed-ip`: MJPEG 접속을 허용할 Pi 주소

## 실행

```bash
python3 recognize_and_register_improved.py
