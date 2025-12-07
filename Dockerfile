# 1. Gebruik een stabiele, slanke Python base image
FROM python:3.11-slim

# 2. Installeer Systeemafhankelijkheden (FFmpeg is HIER)
# apt-get update en installeer ffmpeg en git (nodig voor yt-dlp)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    git \
    && rm -rf /var/lib/apt/lists/*

# 3. Stel de werkmap in
WORKDIR /app

# 4. Kopieer requirements.txt en installeer Python afhankelijkheden
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. Kopieer de rest van je code (inclusief main.py)
COPY . .

# 6. Stel de opstartcommando in
CMD ["python", "main.py"]