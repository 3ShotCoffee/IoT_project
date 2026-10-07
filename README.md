# IoT_project

## 라즈베리파이 환경 설정
1. OS 패키지: `sudo apt install git libportaudio2 libopenblas0 ffmpeg mariadb-server mosquitto mosquitto-clients`
2. 가상환경: `python3 -m venv ~/eq-env` → `source ~/eq-env/bin/activate` → `pip install -r requirements.txt`
3. 오디오: `/boot/firmware/config.txt`에 `dtoverlay=max98357a` 추가, `dtparam=audio=on` 주석 처리, 재부팅
4. 시리얼 권한: `sudo usermod -aG dialout pi`
5. DB: 계정 만들기(SQL) → `use audio`
6. `config.py` 생성 후 비밀번호 입력