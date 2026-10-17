FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    GRADIO_SERVER_NAME=0.0.0.0 \
    GRADIO_SERVER_PORT=7860 \
    MARIMO_HOST=0.0.0.0 \
    MARIMO_PORT=2718 \
    MARIMO_PUBLIC_URL=http://localhost:2718

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        graphviz \
        ffmpeg \
        texlive \
        texlive-latex-extra \
        libcairo2-dev \
        libpango1.0-dev \
        pkg-config \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
# Hosts such as Hugging Face Spaces run the container as a non-root user (uid 1000):
# make the folders the app writes to writable by any user.
# It also needs a writable HOME for Marimo/Manim config and caches.
RUN useradd -m -u 1000 user \
    && mkdir -p designs output/diagrams output/testdata output/animations \
    && chmod -R a+rwX designs output /home/user
ENV HOME=/home/user

EXPOSE 7860
EXPOSE 2718

CMD ["python", "app.py"]
