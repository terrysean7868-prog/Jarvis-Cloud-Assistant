# run_jarvis.py (fixed for your structure)
import subprocess, os, sys, time, signal
import socket

# Try to import requests, install if missing
try:
    import requests
except ImportError:
    print("⚠️  requests not found. Installing...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import requests

ROOT = os.getcwd()

# since your backend files (app.py etc.) are in the root directory:
BACKEND_DIR = ROOT
FRONTEND_DIR = os.path.join(ROOT, "jarvis-frontend")  # frontend directory name
BACKEND_PORT = int(os.getenv("BACKEND_PORT", "8000"))

def is_port_in_use(port):
    """Check if a port is already in use"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('localhost', port)) == 0

def wait_for_backend(port, timeout=30):
    """Wait for backend to be ready"""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            response = requests.get(f"http://localhost:{port}/health", timeout=1)
            if response.status_code == 200:
                return True
        except:
            time.sleep(1)
    return False

def check_backend_dependencies():
    """Check if backend dependencies are installed"""
    try:
        import uvicorn
        import fastapi
        import pydantic
        return True
    except ImportError as e:
        print(f"❌ Missing backend dependency: {e}")
        print("Please install dependencies: pip install -r requirements.txt")
        return False

def start_backend():
    # Check dependencies before starting
    if not check_backend_dependencies():
        print("❌ Backend dependencies not installed. Cannot start backend.")
        return None
    
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    cmd = [sys.executable, "-m", "uvicorn", "app:app", "--reload", "--host", "0.0.0.0", "--port", str(BACKEND_PORT)]
    print(f"🔄 Starting backend on port {BACKEND_PORT}...")
    try:
        return subprocess.Popen(cmd, cwd=BACKEND_DIR, env=env, shell=True)
    except Exception as e:
        print(f"❌ Failed to start backend: {e}")
        return None

def start_frontend():
    env = os.environ.copy()
    env["BROWSER"] = "none"  # Don't auto-open browser
    print("🔄 Starting frontend...")
    # Use npm start for React apps (react-scripts)
    return subprocess.Popen(["npm", "start"], cwd=FRONTEND_DIR, env=env, shell=True)

if __name__ == "__main__":
    try:
        print("=" * 50)
        print("🚀 Starting JARVIS Cloud Assistant")
        print("=" * 50)
        
        # Check if backend port is already in use
        if is_port_in_use(BACKEND_PORT):
            print(f"⚠️  Warning: Port {BACKEND_PORT} is already in use. Backend might already be running.")
        
        # Start backend
        b = start_backend()
        
        if b is None:
            print("❌ Failed to start backend. Please install dependencies first:")
            print("   pip install -r requirements.txt")
            print("   Or run: install_dependencies.bat")
            input("Press Enter to exit...")
            sys.exit(1)
        
        # Wait for backend to be ready
        print("⏳ Waiting for backend to start...")
        if wait_for_backend(BACKEND_PORT):
            print("✅ Backend is ready!")
        else:
            print("⚠️  Warning: Backend may not be ready yet. Continuing anyway...")
            print("   Check backend logs above for errors.")
        
        # Start frontend
        time.sleep(2)  # Additional delay to ensure backend is fully up
        f = start_frontend()
        
        if f is None:
            print("❌ Failed to start frontend.")
            if b:
                b.terminate()
            input("Press Enter to exit...")
            sys.exit(1)
        
        print("=" * 50)
        print("✅ JARVIS Started Successfully!")
        print(f"   Backend:  http://localhost:{BACKEND_PORT}")
        print("   Frontend: http://localhost:3000")
        print("=" * 50)
        print("📢 Say 'Hey Jarvis' to activate voice commands")
        print("Press Ctrl+C to stop")
        print("=" * 50)
        
        try:
            # Wait for both processes
            while True:
                if b.poll() is not None:
                    print("❌ Backend process ended unexpectedly")
                    break
                if f.poll() is not None:
                    print("❌ Frontend process ended unexpectedly")
                    break
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n🛑 Shutting down JARVIS...")
            try:
                b.terminate()
                f.terminate()
                b.wait(timeout=5)
                f.wait(timeout=5)
            except:
                try:
                    b.kill()
                    f.kill()
                except:
                    pass
            print("✅ JARVIS stopped")
            sys.exit(0)
    except Exception as e:
        import traceback
        print("\n[ERROR] Unexpected exception while starting JARVIS:")
        traceback.print_exc()
        try:
            # give user time to read error when double-clicked
            input("Press Enter to exit...")
        except Exception:
            pass
        sys.exit(1)
