# 1. Gebruik een stabiele, slanke Python base image
FROM python:3.11-slim

# 2. Installeer ALLE Systeemafhankelijkheden
RUN apt-get update && apt-get install -y \
    ffmpeg \
    git \
    libopus0 \        # <--- CRUCIAAL: Nodig voor Opus-codering (Discord)
    libsodium-dev \   # <--- CRUCIAAL: Nodig voor PyNaCl/Voice Security
    libffi-dev \      # <--- Nodig voor verschillende Python-bibliotheken
    && rm -rf /var/lib/apt/lists/*

# 3. Stel de werkmap in
WORKDIR /app

# 4. Kopieer requirements.txt en installeer Python afhankelijkheden
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. Kopieer de rest van je code (inclusief main.py en cookies.txt)
COPY . .

# 6. Stel de opstartcommando in
CMD ["python", "main.py"]