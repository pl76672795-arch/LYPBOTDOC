FROM python:3.12-slim

# Instalar dependencias del sistema y Chromium para renderizado PDF (Puppeteer/Playwright futuro)
RUN apt-get update && apt-get install -y \
    chromium \
    fonts-liberation \
    libgconf-2-4 \
    libnss3 \
    libxss1 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copiar dependencias de Python e instalarlas
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el código fuente de LYP DOC BOT
COPY . .

# Exponer el puerto configurado por Railway/Render
ENV PORT=8080
EXPOSE 8080

# Comando de inicio
CMD ["python", "main.py"]
