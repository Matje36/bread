# 1. Gebruik een stabiele, slanke Python base image
FROM python:3.11-slim

# 2. Installeer Systeemafhankelijkheden
# WE VOEGEN HIER libopus0 en libffi-dev AAN TOE
RUN apt-get update && apt-get install -y \
    ffmpeg \
    git \
    libopus0 \  # <--- CRUCIAAL
    libsodium-dev \
    libffi-dev \
    # ... andere libs ...
    && rm -rf /var/lib/apt/lists/*

# 3. Stel de werkmap in
WORKDIR /app

# 4. Kopieer requirements.txt en installeer Python afhankelijkheden
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. Kopieer de rest van je code
COPY . .

# 6. Stel de opstartcommando in
CMD ["python", "main.py"]