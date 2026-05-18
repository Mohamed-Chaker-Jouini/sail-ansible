import os
import subprocess
import sys

# Configuration
VENV_DIR = ".venv"
REQ_FILE = "requirements-test.txt"

# Resolve paths dynamically based on the operating system
if os.name == "nt":  # Windows
    python_bin = os.path.join(VENV_DIR, "Scripts", "python.exe")
    pip_bin = os.path.join(VENV_DIR, "Scripts", "pip.exe")
    pytest_bin = os.path.join(VENV_DIR, "Scripts", "pytest.exe")
else:  # Linux / macOS
    python_bin = os.path.join(VENV_DIR, "bin", "python")
    pip_bin = os.path.join(VENV_DIR, "bin", "pip")
    pytest_bin = os.path.join(VENV_DIR, "bin", "pytest")

def main():
    # 1. Create the virtual environment if it doesn't exist
    if not os.path.exists(VENV_DIR):
        print(f"Creating virtual environment in '{VENV_DIR}'...")
        subprocess.run([sys.executable, "-m", "venv", VENV_DIR], check=True)
    else:
        print(f"Using existing virtual environment in '{VENV_DIR}'.")

    # 2. Install dependencies
    print(f"\nInstalling requirements from '{REQ_FILE}'...")
    subprocess.run([pip_bin, "install", "-r", REQ_FILE], check=True)

    # 3. Run pytest
    print("\nRunning pytest...")
    # sys.argv[1:] passes any extra arguments given to this script straight to pytest
    result = subprocess.run([pytest_bin] + sys.argv[1:])
    
    # Exit with the same code pytest returned (useful for CI/CD pipelines)
    sys.exit(result.returncode)

if __name__ == "__main__":
    main()