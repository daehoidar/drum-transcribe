# Drum Transcribe

유튜브 드럼 영상 URL에서 자동 채보 결과를 PDF 악보와 MusicXML로 뽑는 로컬 파이프라인.

## 흐름

URL에서 audio.wav, transcription.mid, transcription.musicxml, score.pdf 순으로 생성된다. 각 단계는 멱등적이라 이미 만들어진 파일은 다시 만들지 않는다.

## 설치

```
brew install yt-dlp ffmpeg
brew install --cask musescore

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 사용

```
python transcribe.py "https://youtu.be/<id>"        # 곡 한 개
python transcribe.py --file urls.txt                # urls.txt에 한 줄당 한 URL
python transcribe.py "<url>" --force                # 캐시 무시하고 다시
```

## 옵션

- `--thresholds` 클래스별 민감도. BD,SD,HH,TT,CY+RD 순서. 낮출수록 더 많은 노트를 잡는다. 기본 0.22,0.24,0.32,0.22,0.30
- `--grid` MIDI quantize 해상도. 8, 16, 32 중 선택. 기본 16
- `--bpm` quantize 격자 BPM. 기본 120
- `--no-quantize` quantize 끔. 일부 곡에서 박자가 3/4로 잘못 잡힐 수 있다
- `--force` 기존 출력 무시하고 다시 생성

민감도 튜닝 예시 (킥/스네어 더 잡고 하이햇 덜 잡기):

```
python transcribe.py "<url>" --force --thresholds "0.15,0.17,0.55,0.20,0.30"
```

## 출력

`output/<video_id>/` 안에 네 파일이 생긴다.

- `audio.wav` 44.1kHz mono
- `transcription.mid` ADTOF-pytorch 결과. 16분 격자로 quantize 적용
- `transcription.musicxml` 표준 악보 포맷
- `score.pdf` 시각 검토용

## 검수

자동 채보는 초안이다. transcription.musicxml을 MuseScore 4에서 열어 영상과 같이 재생하며 손으로 다듬는다.
