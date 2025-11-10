# Installation Guide for Jarvis Cloud Assistant

## Installation

1. Create and activate a virtual environment:
```bash
python -m venv venv
.\venv\Scripts\activate  # Windows
source venv/bin/activate  # Linux/Mac
```

2. Install dependencies:
   
   a. For minimal installation (core features only):
   ```bash
   # Install only the core dependencies (uncomment the optional sections in requirements.txt)
   pip install -r requirements.txt
   ```
   
   b. For full installation:
   ```bash
   # Install all dependencies including voice and advanced AI features
   pip install -r requirements.txt
   ```

## Optional Features

### Voice Recognition (Windows)

To enable voice recognition features, you'll need:

1. Install Microsoft Visual C++ Build Tools:
   - Download from: https://visualstudio.microsoft.com/visual-cpp-build-tools/
   - In the installer, select "Desktop Development with C++"
   - Make sure "Windows 10/11 SDK" and "MSVC v143" are selected

2. Install voice dependencies:
```bash
pip install -r requirements-extras.txt
```

### Voice Recognition (Linux)

1. Install system dependencies:
```bash
sudo apt-get update
sudo apt-get install python3-dev portaudio19-dev libportaudio2 libportaudiocpp0
sudo apt-get install ffmpeg libav-tools
```

2. Install voice dependencies:
```bash
pip install -r requirements-extras.txt
```

### Running Without Voice Features

If you don't need voice recognition, you can run Jarvis with text-only mode by setting:
```
ENABLE_VOICE=0
```
in your `.env` file.

## Troubleshooting

### webrtcvad Installation Issues

If you encounter issues installing `webrtcvad`:

1. Ensure you have Microsoft Visual C++ Build Tools installed
2. Try installing pre-built wheels:
```bash
pip install --only-binary :all: webrtcvad
```

### Common Issues

1. Memory errors with TensorFlow:
   - Set `TF_FORCE_GPU_ALLOW_GROWTH=true` in your environment
   - Or disable GPU: `CUDA_VISIBLE_DEVICES=-1`

2. Audio device issues:
   - Check your system's default audio input/output devices
   - On Linux, ensure user is in the audio group: `sudo usermod -a -G audio $USER`

## Development Setup

For development, install all dependencies:
```bash
pip install -r requirements-core.txt -r requirements-extras.txt
```

## Environment Variables

Copy `.env.template` to `.env` and fill in your values:
```bash
cp .env.template .env
```