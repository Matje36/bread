# 1. Use a stable, slim Python base image
FROM python:3.11-slim

# 2. Install System Dependencies
# This section installs FFmpeg and the required libraries for voice encoding (libopus0) 
# and PyNaCl (libsodium-dev, libffi-dev) which are essential for Discord voice functionality.
RUN apt-get update && apt-get install -y \
    ffmpeg \
    git \
    libopus0 \
    libsodium-dev \
    libffi-dev \
    # Cleanup unnecessary files to keep the image size small
    && rm -rf /var/lib/apt/lists/*

# 3. Set the working directory for the application
WORKDIR /app

# 4. Copy requirements.txt and install Python dependencies
COPY requirements.txt .
# Use --no-cache-dir to prevent large cache files
RUN pip install --no-cache-dir -r requirements.txt

# 5. Copy the rest of your code (including main.py and cookies.txt)
# Ensure your cookies.txt file is included here!
COPY . .

# 6. Set the startup command for the bot
CMD ["python", "main.py"]